"""Tests for the Silverblaze config flow."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers.selector import DateSelector, SelectSelector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.silverblaze import config_flow
from custom_components.silverblaze.api import (
    CannotConnect,
    EndpointNotAllowed,
    HttpResponse,
    InvalidAuth,
    LoginFlowChanged,
    PageContext,
    SessionExpired,
    SilverblazeClient,
    SilverblazeHttp,
    UnexpectedResponse,
)
from custom_components.silverblaze.api.models import Series, ServicePoint
from custom_components.silverblaze.config_flow import _format_date
from custom_components.silverblaze.const import (
    CONF_IMPORT_FROM,
    CONF_UPDATE_INTERVAL,
    CONF_UTILITY,
    DOMAIN,
)

from .conftest import load_fixture_text
from .test_idahopower_login import (
    ENC_VALUE,
    LOGIN_POST,
    SB,
    TOOLS,
    _happy_chain,
    _html,
    _login_form_chain,
    _post_to_oidc,
    _redirect,
)

OPEN_SESSION = "custom_components.silverblaze.config_flow.open_session"
META_URL_RE = re.compile(rf"^{re.escape(SB)}/api/DataDownload/Meta(\?.*)?$")
USERNAME = "user@example.com"
PASSWORD = "sentinel-password-9f3a"
CTX = PageContext(client_id=1, tab_key="t", parameters_b64="cA==")

COMPONENT = Path(__file__).parent.parent / "custom_components" / "silverblaze"


@pytest.fixture(autouse=True)
def _no_setup(hass: HomeAssistant):
    # The manifest depends on the recorder; mark it loaded so flow init does not
    # try to start a real one (enable_custom_integrations makes recorder_mock unusable here).
    hass.config.components.add("recorder")
    with patch("custom_components.silverblaze.async_setup_entry", return_value=True):
        yield


async def _to_credentials(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_UTILITY: "idahopower"}
    )


class _FixtureHttp:
    """Answers every request with the text of one fixture (no network)."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def request(self, method: str, url: str, **kwargs) -> HttpResponse:
        return HttpResponse(status=200, url=url, text=self._text)


async def _meta_points(name: str = "meta_one_sp.json") -> tuple[ServicePoint, ...]:
    http = _FixtureHttp(load_fixture_text(f"silverblaze/{name}"))
    return await SilverblazeClient(http).get_meta(CTX)  # type: ignore[arg-type]


def _patch_meta(**kwargs):
    return patch.object(config_flow.SilverblazeClient, "get_meta", **kwargs)


async def _to_history(hass: HomeAssistant, points: tuple[ServicePoint, ...] | None = None):
    """Drive user -> credentials -> the history step; return that step's result."""
    if points is None:
        points = await _meta_points()
    result = await _to_credentials(hass)
    with patch(OPEN_SESSION, return_value=CTX), _patch_meta(return_value=points):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )


async def test_user_step_lists_only_idaho_power(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    selector = result["data_schema"].schema[CONF_UTILITY]
    options = selector.config["options"]
    assert [(o["value"], o["label"]) for o in options] == [("idahopower", "Idaho Power")]


async def test_credentials_password_is_masked(hass: HomeAssistant) -> None:
    result = await _to_credentials(hass)
    assert result["step_id"] == "credentials"
    schema = result["data_schema"].schema
    pw = next(v for k, v in schema.items() if k == CONF_PASSWORD)
    assert pw.config["type"] == "password"


async def test_success(hass: HomeAssistant) -> None:
    points = await _meta_points()
    result = await _to_credentials(hass)
    with patch(OPEN_SESSION, return_value=CTX) as mock_open, _patch_meta(return_value=points):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {config_flow.CONF_HISTORY: "all"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Idaho Power"
    assert result["data"] == {
        CONF_UTILITY: "idahopower",
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: PASSWORD,
    }
    assert result["result"].options == {}
    assert mock_open.await_count == 1
    assert mock_open.await_args.args[2:] == (USERNAME, PASSWORD)


async def test_session_closed_after_validation(hass: HomeAssistant) -> None:
    result = await _to_credentials(hass)
    closed = []
    connectors = []
    real = config_flow.async_create_clientsession

    def make(*args, **kwargs):
        session = real(*args, **kwargs)
        closed.append(session)
        connectors.append(session.connector)
        return session

    with (
        patch("custom_components.silverblaze.config_flow.async_create_clientsession", make),
        patch(OPEN_SESSION, side_effect=InvalidAuth),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert len(closed) == 1
    assert closed[0].closed  # detach() switches the session to the closed state
    assert connectors[0] is not None
    assert not connectors[0].closed  # the shared HA connector must survive detach()


@pytest.mark.parametrize(
    ("exc", "key"),
    [
        (InvalidAuth(), "invalid_auth"),
        (CannotConnect(), "cannot_connect"),
        (UnexpectedResponse(), "cannot_connect"),
        (SessionExpired(), "cannot_connect"),
        (LoginFlowChanged(), "login_flow_changed"),
        (RuntimeError("boom"), "unknown"),
        (EndpointNotAllowed("nope"), "unknown"),
    ],
)
async def test_errors_then_recovery(hass: HomeAssistant, exc: Exception, key: str) -> None:
    result = await _to_credentials(hass)
    with patch(OPEN_SESSION, side_effect=exc):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": key}
    assert hass.config_entries.async_entries(DOMAIN) == []

    points = await _meta_points()
    with patch(OPEN_SESSION, return_value=CTX), _patch_meta(return_value=points):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {config_flow.CONF_HISTORY: "all"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_flow_changed_is_not_invalid_auth(hass: HomeAssistant) -> None:
    result = await _to_credentials(hass)
    with patch(OPEN_SESSION, side_effect=LoginFlowChanged("form missing")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["errors"]["base"] != "invalid_auth"
    assert result["errors"]["base"] == "login_flow_changed"


async def test_unknown_logs_trace_without_secrets(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    result = await _to_credentials(hass)
    caplog.set_level(logging.DEBUG)
    with patch(OPEN_SESSION, side_effect=RuntimeError("boom")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["errors"] == {"base": "unknown"}
    records = [r for r in caplog.records if "Unexpected error validating" in r.getMessage()]
    assert records
    assert records[0].exc_info is not None
    assert "RuntimeError" in (records[0].exc_text or caplog.text)
    assert PASSWORD not in caplog.text
    for r in caplog.records:
        assert PASSWORD not in (r.exc_text or "")


async def test_duplicate_aborts_case_insensitive(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"idahopower_{USERNAME}",
        data={CONF_UTILITY: "idahopower", CONF_USERNAME: USERNAME, CONF_PASSWORD: "x"},
    ).add_to_hass(hass)
    result = await _to_credentials(hass)
    with patch(OPEN_SESSION, return_value=CTX) as mock_open:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "User@example.com", CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_open.assert_not_called()


def test_translations_literal_and_identical() -> None:
    strings = (COMPONENT / "strings.json").read_text(encoding="utf-8")
    en = (COMPONENT / "translations" / "en.json").read_text(encoding="utf-8")
    assert "[%key:" not in strings
    assert json.loads(strings) == json.loads(en)
    errors = json.loads(en)["config"]["error"]
    assert errors["invalid_auth"] == "Invalid username or password."
    assert errors["cannot_connect"] == "Failed to connect."
    assert errors["unknown"] == "Unexpected error."
    assert errors["login_flow_changed"] == (
        "Your utility's login flow changed in a way this version does not understand. "
        "Check for an integration update."
    )
    assert json.loads(en)["config"]["abort"]["already_configured"] == (
        "Account is already configured."
    )


GPP_URL = re.compile(rf"^{re.escape(SB)}/api/Page/GetPageParameters(\?.*)?$")


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def fast_http():
    """Skip the real pacing/backoff sleeps without touching production defaults."""
    with patch.object(config_flow, "SilverblazeHttp", partial(SilverblazeHttp, sleep=_no_sleep)):
        yield


def _real_chain(m, *, page_parameters_exception: Exception | None = None) -> None:
    _happy_chain(m)
    m.get(META_URL_RE, status=200, body=load_fixture_text("silverblaze/meta_one_sp.json"))
    _html(m, re.compile(rf"^{re.escape(SB)}/Page\?.*$"), "no_form.html")
    if page_parameters_exception is not None:
        m.add(GPP_URL, method="GET", exception=page_parameters_exception)
    else:
        m.add(
            GPP_URL,
            method="GET",
            status=200,
            body=load_fixture_text("silverblaze/get_page_parameters.json"),
        )


async def test_end_to_end_real_validation_chain(
    hass: HomeAssistant, mock_aioresponse, fast_http
) -> None:
    result = await _to_credentials(hass)
    created = []
    real = config_flow.async_create_clientsession

    def make(*args, **kwargs):
        session = real(*args, **kwargs)
        created.append(session)
        return session

    _real_chain(mock_aioresponse)
    with patch.object(config_flow, "async_create_clientsession", make):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    assert result["description_placeholders"]["history_start"] == "January 1, 2025"
    gpp = [
        k[1]
        for k in mock_aioresponse.requests
        if k[0] == "GET" and k[1].path == "/api/Page/GetPageParameters"
    ]
    assert len(gpp) == 1
    assert gpp[0].query["enc"] == ENC_VALUE
    meta = [k for k in mock_aioresponse.requests if k[1].path == "/api/DataDownload/Meta"]
    assert len(meta) == 1
    assert len(created) == 1  # Meta used the same single session
    assert created[0].closed
    with patch.object(config_flow, "async_create_clientsession", make):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {config_flow.CONF_HISTORY: "all"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Idaho Power"
    assert len(created) == 1


async def test_end_to_end_single_bounce_creates_entry(
    hass: HomeAssistant, mock_aioresponse, fast_http
) -> None:
    result = await _to_credentials(hass)
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(
        mock_aioresponse,
        f"{TOOLS}/MyAccount/signin-oidc",
        f"{TOOLS}/MyAccount/Account/Login?ReturnUrl=SYNTHETIC",
        method="post",
    )
    _real_chain(mock_aioresponse)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {config_flow.CONF_HISTORY: "all"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_end_to_end_double_bounce_is_cannot_connect(
    hass: HomeAssistant, mock_aioresponse, fast_http
) -> None:
    result = await _to_credentials(hass)
    for _ in range(2):
        _login_form_chain(mock_aioresponse)
        _post_to_oidc(mock_aioresponse)
        _redirect(
            mock_aioresponse,
            f"{TOOLS}/MyAccount/signin-oidc",
            f"{TOOLS}/MyAccount/Account/Login?ReturnUrl=SYNTHETIC",
            method="post",
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_end_to_end_rejected_login_400_is_invalid_auth(
    hass: HomeAssistant, mock_aioresponse, fast_http
) -> None:
    result = await _to_credentials(hass)
    _login_form_chain(mock_aioresponse)
    _html(mock_aioresponse, LOGIN_POST, "login_rejected_400.html", status=400, method="post")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_unknown_error_in_real_chain_leaks_no_secrets(
    hass: HomeAssistant,
    mock_aioresponse,
    fast_http,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel_password = "Sentinel-Pw-7c1e44d0"
    result = await _to_credentials(hass)
    caplog.set_level(logging.DEBUG)
    _real_chain(
        mock_aioresponse,
        page_parameters_exception=ValueError("unexpected portal failure"),
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: sentinel_password}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
    assert any("Unexpected error validating" in r.getMessage() for r in caplog.records)
    for secret in (sentinel_password, "SYNTHETIC_ENC"):
        assert secret not in caplog.text
        for r in caplog.records:
            assert secret not in (r.exc_text or "")


# --- setup: history step ---

HISTORY = config_flow.CONF_HISTORY


def _series(interval: str, oldest: str) -> Series:
    return Series(
        interval=interval,
        kind="usage",
        uom="kwh",
        commodity_code="1",
        oldest_utc=datetime.fromisoformat(oldest),
        newest_utc=datetime(2025, 12, 1, tzinfo=UTC),
        oldest_raw=oldest,
        newest_raw="2025-12-01T00:00:00Z",
    )


def _point(sp_id: str, *series: Series) -> ServicePoint:
    return ServicePoint(id=sp_id, timezone_label="MST", series=tuple(series))


async def test_history_step_form(hass: HomeAssistant) -> None:
    result = await _to_history(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    selector = result["data_schema"].schema[HISTORY]
    assert isinstance(selector, SelectSelector)
    assert selector.config["options"] == ["all", "from_date"]
    assert selector.config["translation_key"] == "history_choice"
    assert selector.config["mode"] == "list"
    assert _field(result, HISTORY).default() == "all"
    assert result["description_placeholders"] == {
        "utility_name": "Idaho Power",
        "history_start": "January 1, 2025",
    }


async def test_history_all_creates_entry_without_options(hass: HomeAssistant) -> None:
    result = await _to_history(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {HISTORY: "all"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_UTILITY: "idahopower",
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: PASSWORD,
    }
    assert result["options"] == {}


async def test_history_from_date_asks_for_start_date(hass: HomeAssistant) -> None:
    result = await _to_history(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {HISTORY: "from_date"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "start_date"
    assert [str(k) for k in result["data_schema"].schema] == [CONF_IMPORT_FROM]
    assert isinstance(result["data_schema"].schema[CONF_IMPORT_FROM], DateSelector)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_FROM: "2025-06-01"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_IMPORT_FROM: "2025-06-01"}


async def test_history_start_is_earliest_across_points_in_utility_zone(
    hass: HomeAssistant,
) -> None:
    assert hass.config.time_zone == "US/Pacific"  # the same instant is Dec 31 here
    points = (
        _point("SP0000000001", _series("60min", "2025-03-01T07:00:00+00:00")),
        _point("SP0000000002", _series("60min", "2025-01-01T07:00:00+00:00")),
        _point("SP0000000003", _series("1day", "2024-01-01T07:00:00+00:00")),
    )
    result = await _to_history(hass, points)
    assert result["step_id"] == "history"
    assert result["description_placeholders"]["history_start"] == "January 1, 2025"


@pytest.mark.parametrize(
    "meta",
    [
        {"side_effect": CannotConnect()},
        {"side_effect": UnexpectedResponse()},
        {"side_effect": SessionExpired()},
        {"side_effect": LoginFlowChanged()},
        {"return_value": ()},
        {"return_value": (_point("SP0000000001", _series("1day", "2025-01-01T07:00:00+00:00")),)},
    ],
)
async def test_history_unknown_does_not_block_setup(hass: HomeAssistant, meta: dict) -> None:
    result = await _to_credentials(hass)
    with patch(OPEN_SESSION, return_value=CTX), _patch_meta(**meta):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history_unknown"
    assert result["description_placeholders"] == {"utility_name": "Idaho Power"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {HISTORY: "from_date"}
    )
    assert result["step_id"] == "start_date"


async def test_history_unknown_all_creates_entry(hass: HomeAssistant) -> None:
    result = await _to_credentials(hass)
    with patch(OPEN_SESSION, return_value=CTX), _patch_meta(side_effect=CannotConnect()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["step_id"] == "history_unknown"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {HISTORY: "all"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {}


async def test_unknown_time_zone_skips_history_lookup(hass: HomeAssistant) -> None:
    """Without a usable utility zone the start is unknown: no lookup, same step as a failed one."""

    async def unknown_zone(_name):
        return None

    result = await _to_credentials(hass)
    with (
        patch(OPEN_SESSION, return_value=CTX),
        patch.object(config_flow.dt_util, "async_get_time_zone", unknown_zone),
        _patch_meta() as get_meta,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    get_meta.assert_not_called()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history_unknown"


async def test_history_start_timeout_does_not_block_setup(hass: HomeAssistant) -> None:
    result = await _to_credentials(hass)
    created = []
    real = config_flow.async_create_clientsession

    def make(*args, **kwargs):
        session = real(*args, **kwargs)
        created.append(session)
        return session

    never = asyncio.Event()

    async def hang(*_args, **_kwargs):
        await never.wait()

    with (
        patch.object(config_flow, "async_create_clientsession", make),
        patch.object(config_flow, "HISTORY_START_TIMEOUT", 0.05),
        patch(OPEN_SESSION, return_value=CTX),
        _patch_meta(side_effect=hang),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["step_id"] == "history_unknown"
    assert len(created) == 1
    assert created[0].closed


async def test_meta_endpoint_not_allowed_is_unknown_error(hass: HomeAssistant) -> None:
    result = await _to_credentials(hass)
    with patch(OPEN_SESSION, return_value=CTX), _patch_meta(side_effect=EndpointNotAllowed("no")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": "unknown"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_future_start_date_uses_utility_time_zone(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Denver is 00:30 on Mar 11; US/Pacific (the configured zone) is still Mar 10.
    freezer.move_to("2026-03-11T06:30:00+00:00")
    assert hass.config.time_zone == "US/Pacific"
    result = await _to_history(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {HISTORY: "from_date"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_FROM: "2026-03-12"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "start_date"
    assert result["errors"] == {CONF_IMPORT_FROM: "date_in_future"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_FROM: "2026-03-11"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_IMPORT_FROM: "2026-03-11"}


async def test_start_date_before_history_start_is_accepted(hass: HomeAssistant) -> None:
    result = await _to_history(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {HISTORY: "from_date"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_IMPORT_FROM: "2000-01-01"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_IMPORT_FROM: "2000-01-01"}


async def test_duplicate_added_during_setup_aborts(hass: HomeAssistant) -> None:
    result = await _to_history(hass)
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"idahopower_{USERNAME}",
        data={CONF_UTILITY: "idahopower", CONF_USERNAME: USERNAME, CONF_PASSWORD: "x"},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {HISTORY: "all"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_abandoned_setup_does_not_block_a_new_one(hass: HomeAssistant) -> None:
    """A setup left open (e.g. page refreshed mid-dialog) must not block starting again."""
    stale = await _to_history(hass)
    assert stale["step_id"] == "history"

    fresh = await _to_history(hass)
    assert fresh["type"] is FlowResultType.FORM
    assert fresh["step_id"] == "history"

    result = await hass.config_entries.flow.async_configure(fresh["flow_id"], {HISTORY: "all"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    # Finishing one setup closes the other open setup for the same account.
    assert all(f["flow_id"] != stale["flow_id"] for f in hass.config_entries.flow.async_progress())
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    with pytest.raises(UnknownFlow):
        await hass.config_entries.flow.async_configure(stale["flow_id"], {HISTORY: "all"})
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


def test_format_date() -> None:
    assert _format_date(date(2025, 1, 1)) == "January 1, 2025"
    assert _format_date(date(2025, 12, 31)) == "December 31, 2025"


# --- options flow ---


def _entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Idaho Power",
        unique_id=f"idahopower_{USERNAME}",
        data={CONF_UTILITY: "idahopower", CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        options=options or {},
    )


async def _loaded_entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = _entry(options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _field(result, key: str):
    return next(k for k in result["data_schema"].schema if k == key)


async def _open_settings(hass: HomeAssistant, entry: MockConfigEntry):
    """Open the options menu and choose Change settings."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )


async def test_options_form_defaults(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["settings"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    assert _field(result, CONF_UPDATE_INTERVAL).default() == 12
    assert _field(result, CONF_IMPORT_FROM).default is vol.UNDEFINED
    selector = result["data_schema"].schema[CONF_UPDATE_INTERVAL]
    assert selector.config["mode"] == "box"


async def test_options_form_prefilled_from_entry(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass, {CONF_IMPORT_FROM: "2025-01-01", CONF_UPDATE_INTERVAL: 8})
    result = await _open_settings(hass, entry)
    assert _field(result, CONF_IMPORT_FROM).description == {"suggested_value": "2025-01-01"}
    assert _field(result, CONF_UPDATE_INTERVAL).description == {"suggested_value": 8}


async def test_options_save_reloads_entry(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass)
    with (
        patch("custom_components.silverblaze.async_setup_entry", return_value=True) as setup,
        patch("custom_components.silverblaze.async_unload_entry", return_value=True) as unload,
    ):
        result = await _open_settings(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_IMPORT_FROM: "2025-03-01", CONF_UPDATE_INTERVAL: 10}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_IMPORT_FROM: "2025-03-01", CONF_UPDATE_INTERVAL: 10}
    assert isinstance(entry.options[CONF_UPDATE_INTERVAL], int)
    assert len(unload.mock_calls) == 1
    assert len(setup.mock_calls) == 1


@pytest.mark.parametrize("value", [5, 25])
async def test_options_interval_out_of_range_is_inline_error(
    hass: HomeAssistant, value: int
) -> None:
    entry = await _loaded_entry(hass)
    result = await _open_settings(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPDATE_INTERVAL: value}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_UPDATE_INTERVAL: "interval_out_of_range"}
    assert entry.options == {}


@pytest.mark.parametrize("value", [6, 24])
async def test_options_interval_bounds_accepted(hass: HomeAssistant, value: int) -> None:
    entry = await _loaded_entry(hass)
    result = await _open_settings(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPDATE_INTERVAL: value}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_UPDATE_INTERVAL] == value


async def test_options_empty_date_stored_as_absent(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass, {CONF_IMPORT_FROM: "2025-01-01"})
    result = await _open_settings(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_UPDATE_INTERVAL: 12},  # the frontend omits a cleared date
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_IMPORT_FROM not in entry.options
    assert entry.options == {CONF_UPDATE_INTERVAL: 12}


async def test_options_menu_offers_import_all_only_with_a_date(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass, {CONF_IMPORT_FROM: "2025-06-01", CONF_UPDATE_INTERVAL: 8})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["settings", "import_all"]


async def test_import_all_confirmation_removes_only_the_date(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass, {CONF_IMPORT_FROM: "2025-06-01", CONF_UPDATE_INTERVAL: 8})
    with (
        patch("custom_components.silverblaze.async_setup_entry", return_value=True) as setup,
        patch("custom_components.silverblaze.async_unload_entry", return_value=True) as unload,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "import_all"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "import_all"
        assert result["data_schema"].schema == {}
        assert result["description_placeholders"] == {"current_date": "June 1, 2025"}
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_UPDATE_INTERVAL: 8}
    assert len(unload.mock_calls) == 1
    assert len(setup.mock_calls) == 1


async def test_import_all_stale_menu_keeps_options(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass, {CONF_IMPORT_FROM: "2025-06-01", CONF_UPDATE_INTERVAL: 8})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    hass.config_entries.async_update_entry(entry, options={CONF_UPDATE_INTERVAL: 8})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "import_all"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_UPDATE_INTERVAL: 8}


async def test_import_all_shows_unparseable_date_verbatim(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass, {CONF_IMPORT_FROM: "not-a-date"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "import_all"}
    )
    assert result["description_placeholders"] == {"current_date": "not-a-date"}


async def test_settings_future_date_is_inline_error(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-03-11T06:30:00+00:00")  # Denver Mar 11, Pacific Mar 10
    entry = await _loaded_entry(hass, {CONF_UPDATE_INTERVAL: 8})
    result = await _open_settings(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_FROM: "2026-03-12", CONF_UPDATE_INTERVAL: 8}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_IMPORT_FROM: "date_in_future"}
    assert entry.options == {CONF_UPDATE_INTERVAL: 8}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_FROM: "2026-03-11", CONF_UPDATE_INTERVAL: 8}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_IMPORT_FROM: "2026-03-11", CONF_UPDATE_INTERVAL: 8}


def _dig(tree: dict, path: str) -> str:
    node = tree
    for part in path.split("."):
        node = node[part]
    return node


LATER = (
    "You can change this later in the integration's settings. "
    "Changing it later never deletes hours already imported."
)
TEXTS = {
    "config.step.history.title": "Usage history",
    "config.step.history.description": (
        "{utility_name} has hourly usage for this account from {history_start}.\n\n" + LATER
    ),
    "config.step.history.data.history": "History to import",
    "config.step.history_unknown.title": "Usage history",
    "config.step.history_unknown.description": LATER,
    "config.step.history_unknown.data.history": "History to import",
    "config.step.start_date.title": "Start date",
    "config.step.start_date.description": (
        "Only usage from this date on will be imported. Earlier hours are skipped. "
        "You can change this later in the integration's settings; changing it never deletes "
        "hours already imported."
    ),
    "config.step.start_date.data.import_from": "Start date",
    "config.error.date_in_future": "Pick a date on or before today.",
    "selector.history_choice.options.all": "Import all available history (recommended)",
    "selector.history_choice.options.from_date": "Start from a specific date",
    "options.step.init.title": "Silverblaze (Idaho Power) options",
    "options.step.init.description": (
        "Changing or removing the start date never deletes hours already imported."
    ),
    "options.step.init.menu_options.settings": "Change settings",
    "options.step.init.menu_options.import_all": "Import all available history",
    "options.step.settings.title": "Change settings",
    "options.step.settings.data.import_from": "Import history from",
    "options.step.settings.data.update_interval_hours": "Update interval (hours)",
    "options.step.settings.data_description.import_from": (
        "Only import usage from this date on. Once a date is saved, the previous menu shows an "
        'option labeled "Import all available history" that imports everything your utility '
        "still has. Changing this date never deletes hours already imported; to remove old "
        "hours, use Developer tools > Statistics."
    ),
    "options.step.settings.data_description.update_interval_hours": (
        "How often to check for new data (6-24). Your utility publishes usage about 2-4 days late."
    ),
    "options.step.import_all.title": "Import all available history",
    "options.step.import_all.description": (
        'This removes your "Import history from" date (currently {current_date}). Nothing you '
        "have already imported is deleted. Right after you confirm, any earlier usage your "
        "utility still has is added to your Energy history. This can take a few minutes."
    ),
    "options.error.date_in_future": "Pick a date on or before today.",
}


@pytest.mark.parametrize("path", sorted(TEXTS))
def test_setup_and_option_texts_verbatim(path: str) -> None:
    en = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert _dig(en, path) == TEXTS[path]
    assert "data" not in en["options"]["step"]["init"]


async def test_options_texts_verbatim() -> None:
    en = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    init = en["options"]["step"]["settings"]
    assert init["data"] == {
        "import_from": "Import history from",
        "update_interval_hours": "Update interval (hours)",
    }
    assert init["data_description"]["import_from"] == (
        "Only import usage from this date on. Once a date is saved, the previous menu shows an "
        'option labeled "Import all available history" that imports everything your utility '
        "still has. Changing this date never deletes hours already imported; to remove old "
        "hours, use Developer tools > Statistics."
    )
    assert init["data_description"]["update_interval_hours"] == (
        "How often to check for new data (6-24). Your utility publishes usage about 2-4 days late."
    )
    assert en["options"]["error"]["interval_out_of_range"] == (
        "Choose a value between 6 and 24 hours."
    )
    assert en["config"]["error"]["interval_out_of_range"] == (
        "Choose a value between 6 and 24 hours."
    )
    assert en["config"]["abort"]["reauth_successful"] == "Re-authentication was successful."
    assert en["exceptions"]["cannot_connect"]["message"] == (
        "Failed to connect to Idaho Power or Silverblaze; will retry on the next update."
    )
    assert en["exceptions"]["login_flow_changed"]["message"] == (
        "Your utility's login flow changed in a way this version does not understand. "
        "Check for an integration update."
    )


# --- re-auth flow ---


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry):
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    return flows[0]


async def test_reauth_form_password_only_with_username(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass)
    flow = await _start_reauth(hass, entry)
    assert flow["step_id"] == "reauth_confirm"
    assert flow["context"]["source"] == "reauth"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    assert result["step_id"] == "reauth_confirm"
    assert [str(k) for k in result["data_schema"].schema] == [CONF_PASSWORD]
    assert result["data_schema"].schema[CONF_PASSWORD].config["type"] == "password"
    placeholders = result["description_placeholders"]
    assert placeholders["utility_name"] == "Idaho Power"
    assert placeholders[CONF_USERNAME] == USERNAME
    en = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    step = en["config"]["step"]["reauth_confirm"]
    assert step["title"] == "Re-authenticate {utility_name}"
    assert step["description"] == (
        "The password for {username} no longer works. Enter your current {utility_name} password."
    )
    assert step["data"] == {"password": "Password"}


async def test_reauth_success_updates_password_and_reloads(hass: HomeAssistant) -> None:
    entry = await _loaded_entry(hass)
    flow = await _start_reauth(hass, entry)
    with (
        patch(OPEN_SESSION, return_value=CTX) as mock_open,
        _patch_meta() as mock_meta,
        patch("custom_components.silverblaze.async_setup_entry", return_value=True) as setup,
        patch("custom_components.silverblaze.async_unload_entry", return_value=True) as unload,
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()
    mock_meta.assert_not_called()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {
        CONF_UTILITY: "idahopower",
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: "new-password",
    }
    assert mock_open.await_args.args[2:] == (USERNAME, "new-password")
    assert len(unload.mock_calls) == 1
    assert len(setup.mock_calls) == 1


@pytest.mark.parametrize(
    ("exc", "key"),
    [
        (InvalidAuth(), "invalid_auth"),
        (CannotConnect(), "cannot_connect"),
        (LoginFlowChanged(), "login_flow_changed"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_reauth_errors_then_recovery(hass: HomeAssistant, exc: Exception, key: str) -> None:
    entry = await _loaded_entry(hass)
    flow = await _start_reauth(hass, entry)
    with patch(OPEN_SESSION, side_effect=exc):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_PASSWORD: "wrong"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": key}
    assert entry.data[CONF_PASSWORD] == PASSWORD

    with patch(OPEN_SESSION, return_value=CTX):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_PASSWORD: "right"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "right"


async def test_reauth_makes_no_meta_request(
    hass: HomeAssistant, mock_aioresponse, fast_http
) -> None:
    entry = await _loaded_entry(hass)
    flow = await _start_reauth(hass, entry)
    _real_chain(mock_aioresponse)
    with (
        patch("custom_components.silverblaze.async_setup_entry", return_value=True),
        patch("custom_components.silverblaze.async_unload_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert not [k for k in mock_aioresponse.requests if k[1].path == "/api/DataDownload/Meta"]

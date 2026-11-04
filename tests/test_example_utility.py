"""A synthetic utility module plugs in without touching the generic client."""

from __future__ import annotations

from unittest.mock import patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.silverblaze.api import (
    SILVERBLAZE_ENDPOINTS,
    SilverblazeHttp,
    build_allowlist,
    get_utilities,
    open_session,
)
from custom_components.silverblaze.api.utilities import UtilityModule, base
from custom_components.silverblaze.const import CONF_UTILITY, DOMAIN

from .conftest import load_fixture_text

LOGIN_HOST = "login.example-utility.test"
LOGIN_URL = f"https://{LOGIN_HOST}/signin"
GPP_URL = "https://acewebsite.silverblaze.com/api/Page/GetPageParameters"


@pytest.fixture
def example_utility():
    """Register ``ExampleUtility`` for one test, then restore the registry."""
    before = dict(base._REGISTRY)

    class ExampleUtility(UtilityModule):
        @classmethod
        def name(cls) -> str:
            return "Example Utility"

        @classmethod
        def slug(cls) -> str:
            return "exampleutility"

        @classmethod
        def client_id(cls) -> str:
            return "0"

        @classmethod
        def timezone(cls) -> str:
            return "UTC"

        @classmethod
        def login_endpoints(cls) -> frozenset[tuple[str, str]]:
            return frozenset({(LOGIN_HOST, "/signin")})

        @classmethod
        async def login(cls, http, username, password) -> str:
            await http.request("GET", LOGIN_URL)
            return "SYNTHETIC_ENC"

    try:
        yield ExampleUtility
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(before)


async def _no_sleep(_seconds: float) -> None:
    return None


def test_registered_only_while_fixture_active(example_utility):
    assert get_utilities()["exampleutility"] is example_utility


def test_not_registered_outside_test():
    assert "exampleutility" not in get_utilities()


def test_allowlist_is_silverblaze_plus_login_pairs(example_utility):
    allow = build_allowlist(example_utility.login_endpoints())
    assert allow.endpoints == SILVERBLAZE_ENDPOINTS | {(LOGIN_HOST, "/signin")}


async def test_open_session_uses_unchanged_generic_client(example_utility, mock_aioresponse):
    mock_aioresponse.get(LOGIN_URL, status=200, body="ok")
    mock_aioresponse.get(
        f"{GPP_URL}?enc=SYNTHETIC_ENC",
        status=200,
        body=load_fixture_text("silverblaze/get_page_parameters.json"),
    )
    async with aiohttp.ClientSession() as session:
        http = SilverblazeHttp(
            session, build_allowlist(example_utility.login_endpoints()), sleep=_no_sleep
        )
        ctx = await open_session(http, example_utility, "user@example.com", "SYNTHETIC_PASSWORD")
    assert ctx.parameters_b64


async def test_appears_in_config_flow_dropdown(hass: HomeAssistant, example_utility):
    hass.config.components.add("recorder")
    with patch("custom_components.silverblaze.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    options = result["data_schema"].schema[CONF_UTILITY].config["options"]
    assert ("exampleutility", "Example Utility") in [(o["value"], o["label"]) for o in options]
    assert ("idahopower", "Idaho Power") in [(o["value"], o["label"]) for o in options]

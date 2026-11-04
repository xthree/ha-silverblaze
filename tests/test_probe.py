"""silverblaze-probe: report writer redaction, known-utility flow, CLI surface."""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

from custom_components.silverblaze.api import probe
from custom_components.silverblaze.api.client import DATA_URL, GET_PAGE_PARAMETERS_URL, META_URL
from custom_components.silverblaze.api.redact import redact_headers, redact_text

from .conftest import load_fixture_text
from .helpers_portal import make_day_response

ROOT = Path(__file__).resolve().parent.parent
USER = "sentinel.user@example.com"
PASSWORD = "SentinelPassw0rd!"
TOOLS = "https://tools.idahopower.com"
AUTH = "https://auth.idahopower.com"
SB = "https://acewebsite.silverblaze.com"
USAGE = f"{TOOLS}/MyAccount/Usage"
ENC_VALUE = "SYNTHETIC_ENC+VALUE="
ENC_URL = f"{SB}/Page?enc=SYNTHETIC_ENC%2BVALUE%3D"
GPP = f"{GET_PAGE_PARAMETERS_URL}?{urlencode({'enc': ENC_VALUE})}"
SERVICE_POINT = "SP0000000001"


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fast_http(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(probe, "_HTTP_KWARGS", {"sleep": ft.sleep, "clock": ft.clock})


@pytest.fixture
def prompts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": USER)
    monkeypatch.setattr(probe, "getpass", lambda _prompt="": PASSWORD)
    return tmp_path


# --- mock helpers (synthetic fixtures only) ---


def _html(m, url, name, method="get"):
    m.add(url, method=method, status=200, body=load_fixture_text(f"idahopower/{name}"))


def _redirect(m, url, location, method="get"):
    m.add(url, method=method, status=302, headers={"Location": location})


def _mock_login_ok(m):
    _redirect(m, USAGE, f"{AUTH}/connect/authorize?client_id=SYNTHETIC")
    _redirect(
        m,
        re.compile(rf"^{re.escape(AUTH)}/connect/authorize\?.*$"),
        f"{AUTH}/Account/Login?ReturnUrl=SYNTHETIC",
    )
    _html(m, re.compile(rf"^{re.escape(AUTH)}/Account/Login\?.*$"), "login_form.html")
    _redirect(
        m,
        f"{AUTH}/Account/Login",
        f"{AUTH}/connect/authorize/callback?x=SYNTHETIC",
        method="post",
    )
    _html(
        m, re.compile(rf"^{re.escape(AUTH)}/connect/authorize/callback\?.*$"), "oidc_form_post.html"
    )
    _redirect(m, f"{TOOLS}/MyAccount/signin-oidc", USAGE, method="post")
    _redirect(m, USAGE, ENC_URL)
    _html(m, re.compile(rf"^{re.escape(SB)}/Page\?.*$"), "no_form.html")


def _mock_data_ok(m, day_values):
    m.get(GPP, status=200, body=load_fixture_text("silverblaze/get_page_parameters.json"))
    m.get(
        re.compile(rf"^{re.escape(META_URL)}\?.*$"),
        status=200,
        body=load_fixture_text("silverblaze/meta_one_sp.json"),
    )
    body = make_day_response(date(2026, 11, 1), ZoneInfo("America/Denver"), day_values)
    m.get(re.compile(rf"^{re.escape(DATA_URL)}\?.*$"), status=200, payload=body)


def _report(tmp_path: Path) -> tuple[Path, dict]:
    (path,) = tmp_path.glob("probe-report-*.json")
    return path, json.loads(path.read_text())


# --- report writer: positive schema + redaction ---

_SECRET_LITERALS = [
    "sentinel.user@example.com",
    "SENTINEL_SPID_0001",
    "SENTINEL_ENC_VALUE",
    "SENTINEL_PARAMETERS_VALUE",
    "SentinelPassw0rd!",
]

# One category per string, no header prefix hiding a later one, each well under the
# truncation limit. The plain-literal ones rely on the literal-secret list.
_ERROR_STRINGS = {
    "SENTINEL_QS_VALUE": f"GET {SB}/api/x?a=SENTINEL_QS_VALUE&b=2 failed",
    "SENTINEL_COOKIE_VALUE": "Set-Cookie: sid=SENTINEL_COOKIE_VALUE; Path=/",
    "SENTINEL_AUTH_VALUE": "Authorization: Bearer SENTINEL_AUTH_VALUE",
    "sentinel.user@example.com": "login as sentinel.user@example.com failed",
    "SENTINEL_SPID_0001": "no data for SENTINEL_SPID_0001 today",
    "SENTINEL_ENC_VALUE": "bad token SENTINEL_ENC_VALUE here",
    "SENTINEL_PARAMETERS_VALUE": "bad context SENTINEL_PARAMETERS_VALUE here",
    "SENTINEL_IDTOKEN_VALUE": "got id_token=SENTINEL_IDTOKEN_VALUE",
    "SENTINEL_CODE_VALUE": "got code=SENTINEL_CODE_VALUE",
    "SENTINEL_STATE_VALUE": "got state=SENTINEL_STATE_VALUE",
    "SENTINEL_ACCOUNT_VALUE": "AccountId=SENTINEL_ACCOUNT_VALUE",
    "SENTINEL_CUSTOMER_VALUE": "CustomerId=SENTINEL_CUSTOMER_VALUE",
    "SentinelPassw0rd": "password was SentinelPassw0rd! sorry",
    "SENTINEL_OPAQUE_" + "A" * 30: "blob SENTINEL_OPAQUE_" + "A" * 30,
}
# Fields the schema has no slot for: must be dropped wholesale.
_DROPPED = {
    "SENTINEL_FORM_VALUE": {"form_values": {"Username": "SENTINEL_FORM_VALUE"}},
    "SENTINEL_BODY_VALUE": {"body": {"name": "SENTINEL_BODY_VALUE", "kwh": 987.654}},
    "SENTINEL_HDR_VALUE": {"headers": {"Cookie": "session=SENTINEL_HDR_VALUE"}},
}


def test_report_writer_never_emits_sentinels(tmp_path):
    steps = [
        {"step": "login", "status": "failed", "error": err, **_DROPPED_EXTRA}
        for err in _ERROR_STRINGS.values()
        for _DROPPED_EXTRA in [{k: v for d in _DROPPED.values() for k, v in d.items()}]
    ]
    steps.append(
        {
            "step": "metadata",
            "status": 200,
            "at": f"{SB}/Page?enc=SENTINEL_ENC_VALUE&state=SENTINEL_STATE_VALUE#SENTINEL_FRAG",
            "field_names": ["Username", "Password"],
            "client_id": 999,
            "tab_key": "tab.myusage",
            "seconds": 1.234,
        }
    )
    raw = {
        "generated_at": date(2026, 11, 2),
        "mode": "known",
        "login_style": "sso_handoff",
        "verdict": "partially compatible",
        "failed_step": "metadata",
        **{k: v for d in _DROPPED.values() for k, v in d.items()},
        "steps": steps,
        "series": [
            {
                "interval": "60min",
                "kind": "usage",
                "uom": "kwh",
                "commodity": "1",
                "x": "SENTINEL_X",
            }
        ],
        "ami_span": {"oldest_date": date(2025, 1, 1), "newest_date": date(2026, 11, 1)},
        "sample_day": {"slots": 24, "non_null": 24, "values": [987.654]},
    }
    report = probe.build_report(raw, secrets=_SECRET_LITERALS)
    text = probe.write_report(report, tmp_path).read_text()
    for sentinel in [
        *_ERROR_STRINGS,
        *(k for k in _DROPPED),
        "987.654",
        "SENTINEL_X",
        "SENTINEL_FRAG",
    ]:
        assert sentinel not in text, sentinel
    data = json.loads(text)
    assert set(data) == {
        "probe_version",
        "library_version",
        "generated_at",
        "mode",
        "login_style",
        "verdict",
        "failed_step",
        "steps",
        "series",
        "ami_span",
        "sample_day",
    }
    for step in data["steps"]:
        assert set(step) <= {
            "step",
            "status",
            "at",
            "field_names",
            "client_id",
            "tab_key",
            "seconds",
            "error",
        }
    assert data["steps"][-1]["at"] == "acewebsite.silverblaze.com/Page"
    assert data["steps"][-1]["seconds"] == 1.2
    assert data["sample_day"] == {"slots": 24, "non_null": 24}
    assert data["ami_span"] == {"oldest_date": "2025-01-01", "newest_date": "2026-11-01"}


@pytest.mark.parametrize("secret", ["e", "login", "1", "ok"])
def test_short_secrets_do_not_corrupt_labels(tmp_path, secret):
    raw = {
        "generated_at": date(2026, 11, 2),
        "mode": "known",
        "login_style": "sso_handoff",
        "verdict": "not compatible",
        "failed_step": "login",
        "steps": [{"step": "login", "status": "failed", "error": "LoginFlowChanged: x"}],
        "series": [{"interval": "60min", "kind": "usage", "uom": "kwh", "commodity": "1"}],
    }
    data = probe.build_report(raw, secrets=[secret, "SENTINEL_SPID_0001"])
    assert data["verdict"] == "not compatible"
    assert data["login_style"] == "sso_handoff"
    assert data["mode"] == "known"
    assert data["failed_step"] == "login"
    assert data["steps"][0]["step"] == "login"
    assert data["steps"][0]["status"] == "failed"
    assert data["series"][0] == {
        "interval": "60min",
        "kind": "usage",
        "uom": "kwh",
        "commodity": "1",
    }


def test_free_text_secret_replacement_needs_four_chars():
    raw = {"steps": [{"step": "login", "error": "abcdef LONGSECRET xyz"}]}
    out = probe.build_report(raw, secrets=["e", "LONGSECRET"])["steps"][0]["error"]
    assert out == "abcdef <redacted> xyz"


def test_unknown_labels_become_unknown():
    data = probe.build_report(
        {"mode": "x@example.com", "verdict": "bogus", "login_style": "?", "failed_step": "zzz"}
    )
    assert (data["mode"], data["verdict"], data["login_style"], data["failed_step"]) == (
        "unknown",
        "unknown",
        "unknown",
        "unknown",
    )


def test_report_filename_matches_gitignore_pattern(tmp_path):
    path = probe.write_report(probe.build_report({"generated_at": date(2026, 11, 2)}), tmp_path)
    assert path.name == "probe-report-2026-11-02.json"  # no mode label: no suffix
    assert fnmatch.fnmatch(path.name, "probe-report*.json")
    assert "probe-report*.json" in (ROOT / ".gitignore").read_text().splitlines()


def test_library_version_comes_from_manifest():
    manifest = json.loads((ROOT / "custom_components/silverblaze/manifest.json").read_text())
    assert probe.build_report({})["library_version"] == manifest["version"]


@pytest.mark.parametrize(
    "text",
    [
        "Set-Cookie: a=SENTINEL_COOKIE_VALUE; Path=/",
        "cookie=SENTINEL_COOKIE_VALUE",
        "Authorization: Bearer SENTINEL_COOKIE_VALUE",
        "Bearer SENTINEL_COOKIE_VALUE",
    ],
)
def test_redact_text_covers_cookie_and_auth_headers(text):
    assert "SENTINEL_COOKIE_VALUE" not in redact_text(text)


def test_redact_headers_blanks_credential_headers():
    out = redact_headers({"Cookie": "a=b", "Authorization": "Bearer x", "Accept": "text/html"})
    assert out["Cookie"] == out["Authorization"] == "<redacted>"
    assert out["Accept"] == "text/html"


# --- known-utility flow end to end ---


def test_known_mode_compatible_end_to_end(fast_http, prompts, mock_aioresponse, capsys):
    _mock_login_ok(mock_aioresponse)
    _mock_data_ok(mock_aioresponse, [987.654] * 20 + [None] * 4)

    assert probe.main(["--utility", "idahopower"]) == 0

    out = capsys.readouterr().out
    assert "Verdict: compatible" in out
    assert "Review it before attaching" in out
    assert "no credentials" in out
    path, report = _report(prompts)
    assert report["verdict"] == "compatible"
    assert report["mode"] == "known"
    assert report["login_style"] == "sso_handoff"
    assert report["failed_step"] is None
    steps = {s["step"]: s for s in report["steps"]}
    assert list(steps) == ["login", "page_parameters", "metadata", "hourly_sample"]
    assert all(s["status"] == "ok" for s in steps.values())
    assert steps["page_parameters"]["client_id"] == 999
    assert steps["page_parameters"]["tab_key"] == "tab.myusage"
    assert steps["page_parameters"]["at"] == "acewebsite.silverblaze.com/api/Page/GetPageParameters"
    assert all("seconds" in s for s in steps.values())
    assert report["series"] == [
        {"interval": "60min", "kind": "usage", "uom": "kwh", "commodity": "1"}
    ]
    assert report["ami_span"] == {"oldest_date": "2025-01-01", "newest_date": "2026-11-01"}
    assert report["sample_day"] == {"slots": 24, "non_null": 20}
    text = path.read_text()
    for secret in (USER, PASSWORD, "SYNTHETIC_ENC", SERVICE_POINT, "987.654", "SYNTHETIC_LAYOUT"):
        assert secret not in text, secret
    assert fnmatch.fnmatch(path.name, "probe-report*.json")


def test_login_failure_is_not_compatible(fast_http, prompts, mock_aioresponse):
    _redirect(mock_aioresponse, USAGE, f"{AUTH}/connect/authorize?client_id=SYNTHETIC")
    _html(
        mock_aioresponse,
        re.compile(rf"^{re.escape(AUTH)}/connect/authorize\?.*$"),
        "no_form.html",
    )

    assert probe.main(["--utility", "idahopower"]) == 1

    _, report = _report(prompts)
    assert report["verdict"] == "not compatible"
    assert report["failed_step"] == "login"
    assert [s["step"] for s in report["steps"]] == ["login"]
    assert report["steps"][0]["status"] == "failed"
    assert report["steps"][0]["error"].startswith("LoginFlowChanged")


def test_invalid_credentials_do_not_leak(fast_http, prompts, mock_aioresponse):
    _redirect(mock_aioresponse, USAGE, f"{AUTH}/connect/authorize?client_id=SYNTHETIC")
    _redirect(
        mock_aioresponse,
        re.compile(rf"^{re.escape(AUTH)}/connect/authorize\?.*$"),
        f"{AUTH}/Account/Login?ReturnUrl=SYNTHETIC",
    )
    _html(
        mock_aioresponse, re.compile(rf"^{re.escape(AUTH)}/Account/Login\?.*$"), "login_form.html"
    )
    _html(mock_aioresponse, f"{AUTH}/Account/Login", "login_form_with_error.html", method="post")

    assert probe.main(["--utility", "idahopower"]) == 1

    path, report = _report(prompts)
    assert report["verdict"] == "not compatible"
    assert report["failed_step"] == "login"
    assert USER not in path.read_text()
    assert PASSWORD not in path.read_text()


def test_data_step_failure_is_partially_compatible(fast_http, prompts, mock_aioresponse):
    _mock_login_ok(mock_aioresponse)
    mock_aioresponse.get(
        GPP, status=200, body=load_fixture_text("silverblaze/get_page_parameters.json")
    )
    mock_aioresponse.get(
        re.compile(rf"^{re.escape(META_URL)}\?.*$"),
        status=200,
        body=load_fixture_text("silverblaze/meta_one_sp.json"),
    )
    mock_aioresponse.get(re.compile(rf"^{re.escape(DATA_URL)}\?.*$"), status=200, body="not json")

    assert probe.main(["--utility", "idahopower"]) == 1

    _, report = _report(prompts)
    assert report["verdict"] == "partially compatible"
    assert report["failed_step"] == "hourly_sample"
    assert "sample_day" not in report
    assert report["series"]


def test_known_mode_uses_only_allowlisted_hosts(fast_http, prompts, mock_aioresponse):
    _mock_login_ok(mock_aioresponse)
    _mock_data_ok(mock_aioresponse, [1.0] * 24)
    probe.main(["--utility", "idahopower"])
    hosts = {key[1].host for key in mock_aioresponse.requests}
    assert hosts <= {"tools.idahopower.com", "auth.idahopower.com", "acewebsite.silverblaze.com"}


_LEAKY = (
    f"boom GET {SB}/api/Data?enc=SENTINEL_ENC_VALUE&parameters=SENTINEL_PARAMETERS_VALUE "
    f'payload {{"ServicePointId": "SP9999999999", "Name": "SENTINEL_NAME", "Kwh": 987.654}} '
    f"user {USER}"
)


@pytest.mark.parametrize("target", ["get_meta", "login"])
def test_unvetted_exception_message_never_reaches_report(
    fast_http, prompts, mock_aioresponse, monkeypatch, capsys, target
):
    from custom_components.silverblaze.api.client import SilverblazeClient
    from custom_components.silverblaze.api.utilities import get_utility

    async def boom(*_args, **_kwargs):
        raise RuntimeError(_LEAKY)

    if target == "login":
        monkeypatch.setattr(get_utility("idahopower"), "login", boom)
    else:
        _mock_login_ok(mock_aioresponse)
        mock_aioresponse.get(
            GPP, status=200, body=load_fixture_text("silverblaze/get_page_parameters.json")
        )
        monkeypatch.setattr(SilverblazeClient, "get_meta", boom)

    assert probe.main(["--utility", "idahopower"]) == 1

    path, report = _report(prompts)
    text = path.read_text()
    out = capsys.readouterr().out
    for leak in (
        "SENTINEL_ENC_VALUE",
        "SENTINEL_PARAMETERS_VALUE",
        "SP9999999999",
        "SENTINEL_NAME",
        "987.654",
        USER,
        "sentinel.user",
        PASSWORD,
        "boom",
    ):
        assert leak not in text, leak
        assert leak not in out, leak
    failed = report["steps"][-1]
    assert failed["status"] == "failed"
    assert failed["error"] == "RuntimeError"
    assert report["failed_step"] == ("login" if target == "login" else "metadata")


def test_vetted_exception_message_is_kept_but_redacted(
    fast_http, prompts, mock_aioresponse, monkeypatch
):
    from custom_components.silverblaze.api.exceptions import LoginFlowChanged
    from custom_components.silverblaze.api.utilities import get_utility

    async def boom(*_args, **_kwargs):
        raise LoginFlowChanged(
            f"form moved at {AUTH}/Account/Login?code=SENTINEL_CODE_VALUE {USER}"
        )

    monkeypatch.setattr(get_utility("idahopower"), "login", boom)
    probe.main(["--utility", "idahopower"])
    path, report = _report(prompts)
    error = report["steps"][0]["error"]
    assert error.startswith("LoginFlowChanged: form moved")
    assert "SENTINEL_CODE_VALUE" not in path.read_text()
    assert USER not in path.read_text()


def test_meta_without_hourly_series_is_partial_and_lists_series(
    fast_http, prompts, mock_aioresponse
):
    _mock_login_ok(mock_aioresponse)
    mock_aioresponse.get(
        GPP, status=200, body=load_fixture_text("silverblaze/get_page_parameters.json")
    )
    meta = load_fixture_text("silverblaze/meta_one_sp.json").replace('"60min"', '"15min"')
    mock_aioresponse.get(re.compile(rf"^{re.escape(META_URL)}\?.*$"), status=200, body=meta)

    assert probe.main(["--utility", "idahopower"]) == 1

    _, report = _report(prompts)
    assert report["verdict"] == "partially compatible"
    assert report["failed_step"] == "metadata"
    assert [s["interval"] for s in report["series"]] == ["15min"]
    error = report["steps"][-1]["error"]
    assert error.startswith("UnexpectedResponse: Meta response has no hourly")


def test_vetted_error_after_meta_redacts_service_point_id(
    fast_http, prompts, mock_aioresponse, monkeypatch
):
    from custom_components.silverblaze.api.client import SilverblazeClient
    from custom_components.silverblaze.api.exceptions import UnexpectedResponse

    _mock_login_ok(mock_aioresponse)
    mock_aioresponse.get(
        GPP, status=200, body=load_fixture_text("silverblaze/get_page_parameters.json")
    )
    mock_aioresponse.get(
        re.compile(rf"^{re.escape(META_URL)}\?.*$"),
        status=200,
        body=load_fixture_text("silverblaze/meta_one_sp.json"),
    )

    async def boom(*_args, **_kwargs):
        raise UnexpectedResponse(f"bad day for {SERVICE_POINT}")

    monkeypatch.setattr(SilverblazeClient, "get_day", boom)
    assert probe.main(["--utility", "idahopower"]) == 1
    path, report = _report(prompts)
    assert SERVICE_POINT not in path.read_text()
    assert report["steps"][-1]["error"].startswith("UnexpectedResponse: bad day for")


def test_vetted_error_host_paths_are_cleaned_per_segment():
    raw = {
        "steps": [
            {
                "step": "login",
                "status": "failed",
                "error": (
                    "redirected to acewebsite.silverblaze.com/Page/jane.doe/home and "
                    "tools.idahopower.com/MyAccount/Account/12345678/Usage"
                ),
            }
        ]
    }
    error = probe.build_report(raw)["steps"][0]["error"]
    assert "acewebsite.silverblaze.com/Page/<redacted>/home" in error
    assert "tools.idahopower.com/MyAccount/Account/<redacted>/Usage" in error
    assert "jane" not in error
    assert "12345678" not in error


@pytest.mark.parametrize(
    ("values", "slots", "non_null"),
    [
        ([], 0, 0),
        ([None] * 24, 24, 0),
        ([1.5] * 25, 25, 25),
    ],
    ids=["empty-body", "all-null", "fall-back-25"],
)
def test_known_mode_sample_day_shapes(
    fast_http, prompts, mock_aioresponse, monkeypatch, values, slots, non_null
):
    _mock_login_ok(mock_aioresponse)
    mock_aioresponse.get(
        GPP, status=200, body=load_fixture_text("silverblaze/get_page_parameters.json")
    )
    mock_aioresponse.get(
        re.compile(rf"^{re.escape(META_URL)}\?.*$"),
        status=200,
        body=load_fixture_text("silverblaze/meta_one_sp.json"),
    )
    if values:
        body = make_day_response(date(2026, 11, 1), ZoneInfo("America/Denver"), values)
    else:
        body = {"Data": []}
    mock_aioresponse.get(re.compile(rf"^{re.escape(DATA_URL)}\?.*$"), status=200, payload=body)

    assert probe.main(["--utility", "idahopower"]) == 0
    _, report = _report(prompts)
    assert report["verdict"] == "compatible"
    assert report["sample_day"] == {"slots": slots, "non_null": non_null}


# --- CLI surface ---


@pytest.mark.parametrize(
    "argv",
    [
        ["--utility", "idahopower", "--password", "x"],
        ["--utility", "idahopower", "--username", "x"],
        ["--utility", "idahopower", "--pass", "x"],
        ["--password", "x"],
        [],
        ["--utility", "idahopower", "--portal-url", "https://example-utility.test/"],
        ["--utility", "nosuchutility"],
        ["--portal-url", "http://example-utility.test/"],
    ],
)
def test_argparse_rejects_bad_invocations(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        probe.main(argv)
    assert exc.value.code == 2
    capsys.readouterr()


def test_credentials_prompted_not_taken_from_environment(
    monkeypatch, tmp_path, capsys, mock_aioresponse
):
    monkeypatch.chdir(tmp_path)
    clock = [1000.0]

    async def _sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(probe, "_HTTP_KWARGS", {"sleep": _sleep, "clock": lambda: clock[0]})
    mock_aioresponse.get(
        "https://example-utility.test/",
        status=200,
        body=load_fixture_text("probe/no_login_form.html"),
    )
    monkeypatch.setenv("SILVERBLAZE_PASSWORD", "x")
    asked_input: list[str] = []
    asked_getpass: list[str] = []
    monkeypatch.setattr("builtins.input", lambda p="": asked_input.append(p) or USER)
    monkeypatch.setattr(probe, "getpass", lambda p="": asked_getpass.append(p) or PASSWORD)
    probe.main(["--portal-url", "https://example-utility.test/"])
    assert asked_input == ["Utility username: "]
    assert asked_getpass == ["Utility password (hidden): "]


_BLOCK = "import sys; sys.modules['homeassistant'] = None; "


def test_help_without_homeassistant():
    code = _BLOCK + "from custom_components.silverblaze.api.probe import main; main(['--help'])"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "--utility" in result.stdout
    assert "--password" not in result.stdout


def test_python_dash_m_help_without_homeassistant():
    code = (
        _BLOCK
        + "import runpy; sys.argv = ['probe', '--help']; "
        + "runpy.run_module('custom_components.silverblaze.api.probe', run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "silverblaze-probe" in result.stdout


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        probe.main(["--version"])
    assert exc.value.code == 0
    assert probe.PROBE_VERSION in capsys.readouterr().out


def test_console_script_declared():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'silverblaze-probe = "custom_components.silverblaze.api.probe:main"' in text


def test_reports_never_overwrite_each_other(tmp_path):
    first = probe.build_report({"generated_at": date(2026, 11, 2), "mode": "known"})
    second = probe.build_report(
        {"generated_at": date(2026, 11, 2), "mode": "known", "verdict": "compatible"}
    )
    a = probe.write_report(first, tmp_path)
    b = probe.write_report(second, tmp_path)
    c = probe.write_report(second, tmp_path)
    assert [p.name for p in (a, b, c)] == [
        "probe-report-2026-11-02-known.json",
        "probe-report-2026-11-02-known-2.json",
        "probe-report-2026-11-02-known-3.json",
    ]
    assert json.loads(a.read_text()) == first
    assert json.loads(b.read_text()) == second


def test_known_and_discovery_reports_have_different_names(tmp_path):
    stamp = date(2026, 11, 2)
    known = probe.write_report(
        probe.build_report({"generated_at": stamp, "mode": "known"}), tmp_path
    )
    found = probe.write_report(
        probe.build_report({"generated_at": stamp, "mode": "discovery"}), tmp_path
    )
    assert known.name == "probe-report-2026-11-02-known.json"
    assert found.name == "probe-report-2026-11-02-discovery.json"
    for path in (known, found):
        assert fnmatch.fnmatch(path.name, "probe-report*.json")


def test_missing_mode_still_never_overwrites(tmp_path):
    report = probe.build_report({"generated_at": date(2026, 11, 2)})
    names = [probe.write_report(report, tmp_path).name for _ in range(2)]
    assert names == ["probe-report-2026-11-02.json", "probe-report-2026-11-02-2.json"]
    assert all(fnmatch.fnmatch(n, "probe-report*.json") for n in names)


@pytest.mark.parametrize("mode", ["../x", "a/b", ["known"], None])
def test_unexpected_mode_never_reaches_the_file_name(tmp_path, mode):
    path = probe.write_report({"generated_at": "2026-11-02", "mode": mode}, tmp_path)
    assert path.name == "probe-report-2026-11-02.json"
    assert path.parent == tmp_path

"""Generic discovery (--portal-url): read-only policy, style detection, request log."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from yarl import URL

from custom_components.silverblaze.api import probe
from custom_components.silverblaze.api.client import DATA_URL, GET_PAGE_PARAMETERS_URL, META_URL
from custom_components.silverblaze.api.discovery import (
    DiscoveryPolicy,
    _login_candidate,
    is_oidc_form,
    zone_for_label,
)
from custom_components.silverblaze.api.exceptions import EndpointNotAllowed
from custom_components.silverblaze.api.forms import forms_of

from .conftest import load_fixture_text
from .helpers_portal import make_day_response

ROOT = Path(__file__).resolve().parent.parent
USER = "sentinel.user@example.com"
PASSWORD = "SentinelPassw0rd!"
SITE = "https://example-utility.test"
AUTH = "https://auth.example-utility.test"
SB = "https://acewebsite.silverblaze.com"
START = f"{SITE}/usage"
DIRECT_START = f"{SB}/ExampleTown"
ENC_VALUE = "SYNTHETIC_ENC+VALUE="
ENC_URL = f"{SB}/Page?enc=SYNTHETIC_ENC%2BVALUE%3D"
GPP = f"{GET_PAGE_PARAMETERS_URL}?{urlencode({'enc': ENC_VALUE})}"
LOGIN_POST = ("POST", f"{AUTH}/Account/Login")
DIRECT_POST = ("POST", f"{SB}/ExampleTown/Login")


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


def _html(m, url, name, method="get", status=200):
    m.add(url, method=method, status=status, body=load_fixture_text(f"probe/{name}"))


def _redirect(m, url, location, method="get"):
    m.add(url, method=method, status=302, headers={"Location": location})


def _mock_sso_chain(m, *, sb_page="silverblaze_page.html"):
    _redirect(m, START, f"{AUTH}/login?x=SYNTHETIC")
    _html(m, re.compile(rf"^{re.escape(AUTH)}/login\?.*$"), "sso_login_form.html")
    _redirect(m, f"{AUTH}/Account/Login", f"{AUTH}/callback?x=SYNTHETIC", method="post")
    _html(m, re.compile(rf"^{re.escape(AUTH)}/callback\?.*$"), "sso_form_post.html")
    _redirect(m, f"{SITE}/signin-oidc", START, method="post")
    _redirect(m, START, ENC_URL)
    _html(m, re.compile(rf"^{re.escape(SB)}/Page\?.*$"), sb_page)


def _mock_data_ok(m):
    m.get(GPP, status=200, body=load_fixture_text("silverblaze/get_page_parameters.json"))
    m.get(
        re.compile(rf"^{re.escape(META_URL)}\?.*$"),
        status=200,
        body=load_fixture_text("silverblaze/meta_one_sp.json"),
    )
    body = make_day_response(date(2026, 11, 1), ZoneInfo("America/Denver"), [1.5] * 24)
    m.get(re.compile(rf"^{re.escape(DATA_URL)}\?.*$"), status=200, payload=body)


def _report(tmp_path: Path) -> tuple[str, dict]:
    (path,) = tmp_path.glob("probe-report-*.json")
    return path.read_text(), json.loads(path.read_text())


def _requested(m) -> list[tuple[str, str]]:
    return [(method, str(url)) for (method, url) in m.requests]


def _step(report: dict, name: str) -> dict:
    return next(s for s in report["steps"] if s["step"] == name)


# --- style detection ----------------------------------------------------------------


def test_sso_chain_detected_and_compatible(fast_http, prompts, mock_aioresponse):
    _mock_sso_chain(mock_aioresponse)
    _mock_data_ok(mock_aioresponse)
    assert probe.main(["--portal-url", START]) == 0
    text, report = _report(prompts)
    assert report["mode"] == "discovery"
    assert report["login_style"] == "sso_handoff"
    assert report["verdict"] == "compatible"
    assert report["failed_step"] is None
    assert [s["step"] for s in report["steps"]] == [
        "start_page",
        "login",
        "form_post",
        "page_parameters",
        "metadata",
        "hourly_sample",
    ]
    assert _step(report, "start_page")["status"] == 200
    # Names only (sorted; long token-like names are redacted by the report writer).
    names = set(_step(report, "login")["field_names"])
    assert {"ReturnUrl", "Username", "Password", "button"} <= names
    assert report["sample_day"] == {"slots": 24, "non_null": 24}
    assert report["ami_span"]["newest_date"]
    for secret in (USER, PASSWORD, "SYNTHETIC_ENC", "SYNTHETIC_CODE", "SYNTHETIC_RVT"):
        assert secret not in text


def test_direct_login_page_detected(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _html(m, DIRECT_START, "direct_login_page.html")
    _redirect(m, f"{SB}/ExampleTown/Login", ENC_URL, method="post")
    _html(m, re.compile(rf"^{re.escape(SB)}/Page\?.*$"), "silverblaze_page.html")
    _mock_data_ok(m)
    assert probe.main(["--portal-url", DIRECT_START]) == 0
    _, report = _report(prompts)
    assert report["login_style"] == "direct_login"
    assert report["verdict"] == "compatible"
    assert {"Email", "Password", "submit"} <= set(_step(report, "login")["field_names"])
    # The username went to the "Email" field, the password to "Password" only.
    sent = m.requests[(DIRECT_POST[0], URL(DIRECT_POST[1]))][0].kwargs["data"]
    assert sent["Email"] == USER
    assert sent["Password"] == PASSWORD


def test_direct_login_accepted_without_enc_is_partial(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _html(m, DIRECT_START, "direct_login_page.html")
    _redirect(m, f"{SB}/ExampleTown/Login", f"{SB}/ExampleTown/Home", method="post")
    _html(m, f"{SB}/ExampleTown/Home", "silverblaze_page.html")
    assert probe.main(["--portal-url", DIRECT_START]) == 1
    _, report = _report(prompts)
    assert report["login_style"] == "direct_login"
    assert report["verdict"] == "partially compatible"
    assert report["failed_step"] == "handoff"


def test_direct_login_rejected_is_not_compatible(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _html(m, DIRECT_START, "direct_login_page.html")
    _html(m, f"{SB}/ExampleTown/Login", "direct_login_page.html", method="post")
    assert probe.main(["--portal-url", DIRECT_START]) == 1
    _, report = _report(prompts)
    assert report["login_style"] == "direct_login"
    assert report["verdict"] == "not compatible"
    assert report["failed_step"] == "login"


def test_direct_login_rejected_with_400_is_not_compatible(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _html(m, DIRECT_START, "direct_login_page.html")
    _html(m, f"{SB}/ExampleTown/Login", "direct_login_page.html", method="post", status=400)
    assert probe.main(["--portal-url", DIRECT_START]) == 1
    _, report = _report(prompts)
    assert report["login_style"] == "direct_login"
    assert report["verdict"] == "not compatible"
    assert report["failed_step"] == "login"


def test_no_login_form_is_unknown_and_nothing_submitted(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _html(m, START, "no_login_form.html")
    assert probe.main(["--portal-url", START]) == 1
    _, report = _report(prompts)
    assert report["login_style"] == "unknown"
    assert report["verdict"] == "not compatible"
    assert report["failed_step"] == "login"
    # Neither the page's link nor its (non-login) form was touched.
    assert _requested(m) == [("GET", START)]


def test_sso_chain_that_never_reaches_silverblaze_is_unknown(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _redirect(m, START, f"{AUTH}/login?x=SYNTHETIC")
    _html(m, re.compile(rf"^{re.escape(AUTH)}/login\?.*$"), "sso_login_form.html")
    _html(m, f"{AUTH}/Account/Login", "no_login_form.html", method="post")
    assert probe.main(["--portal-url", START]) == 1
    _, report = _report(prompts)
    assert report["login_style"] == "unknown"
    assert report["verdict"] == "not compatible"
    assert report["failed_step"] == "handoff"


def test_later_data_failure_is_partial(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _mock_sso_chain(m)
    m.get(GPP, status=200, body=load_fixture_text("silverblaze/session_expired.txt"))
    assert probe.main(["--portal-url", START]) == 1
    _, report = _report(prompts)
    assert report["login_style"] == "sso_handoff"
    assert report["verdict"] == "partially compatible"
    assert report["failed_step"] == "page_parameters"


# --- read-only behaviour (AC7) -------------------------------------------------------


def test_links_and_other_forms_never_requested(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _mock_sso_chain(m)
    _mock_data_ok(m)
    probe.main(["--portal-url", START])
    requested = _requested(m)
    # Page offers a billing link, a search form and a newsletter form: none is touched.
    for url in (
        f"{SITE}/billing/statements",
        f"{AUTH}/search",
        f"{AUTH}/newsletter/subscribe",
    ):
        assert not any(u.startswith(url) for _, u in requested), url
    posts = [(meth, u) for meth, u in requested if meth == "POST"]
    assert posts == [LOGIN_POST, ("POST", f"{SITE}/signin-oidc")]


def test_password_posted_only_to_login_form_action(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _mock_sso_chain(m)
    _mock_data_ok(m)
    probe.main(["--portal-url", START])
    carrying = []
    for key, calls in m.requests.items():
        for call in calls:
            blob = repr(call.kwargs) + repr(key)
            if PASSWORD in blob or USER in blob:
                carrying.append(key[0:2])
    assert [(meth, str(url)) for meth, url in carrying] == [LOGIN_POST]
    # The OIDC hop forwards only the form's own fields.
    hop = m.requests[("POST", URL(f"{SITE}/signin-oidc"))][0]
    assert set(hop.kwargs["data"]) == {"code", "id_token", "state"}


def test_silverblaze_billing_redirect_refused_before_io(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _redirect(m, START, f"{SB}/ExampleTown/Billing")
    assert probe.main(["--portal-url", START]) == 1
    _, report = _report(prompts)
    assert report["failed_step"] == "start_page"
    assert report["verdict"] == "not compatible"
    assert "not allowed" in _step(report, "start_page")["error"]
    assert _requested(m) == [("GET", START)]


def test_redirect_to_other_host_is_followed_but_not_to_http(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _redirect(m, START, "http://example-utility.test/plain")
    assert probe.main(["--portal-url", START]) == 1
    assert _requested(m) == [("GET", START)]


def test_integration_never_uses_discovery_policy():
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "custom_components").rglob("*.py")
        if "DiscoveryPolicy" in p.read_text() and p.name not in ("discovery.py", "probe.py")
    ]
    assert offenders == []


# --- policy unit tests --------------------------------------------------------------


def _form(html: str, index: int):
    return forms_of(html)[index]


def test_policy_refuses_post_to_non_password_non_oidc_form():
    policy = DiscoveryPolicy(START)
    html = load_fixture_text("probe/sso_login_form.html")
    for index in (0, 1):  # search (GET) and newsletter (POST, no password / OIDC fields)
        with pytest.raises(EndpointNotAllowed):
            policy.approve_form(f"{AUTH}/login", _form(html, index))
    with pytest.raises(EndpointNotAllowed):
        policy.check("POST", f"{AUTH}/newsletter/subscribe")


def test_policy_approves_login_and_oidc_forms_once():
    policy = DiscoveryPolicy(START)
    login = _form(load_fixture_text("probe/sso_login_form.html"), 2)
    assert policy.approve_form(f"{AUTH}/login", login) == ("login", f"{AUTH}/Account/Login")
    policy.check("POST", f"{AUTH}/Account/Login")
    policy.end_request()
    with pytest.raises(EndpointNotAllowed):
        policy.check("POST", f"{AUTH}/Account/Login")
    oidc = _form(load_fixture_text("probe/sso_form_post.html"), 0)
    kind, action = policy.approve_form(f"{AUTH}/callback", oidc)
    assert (kind, action) == ("oidc", f"{SITE}/signin-oidc")
    policy.check("POST", action)


def test_policy_login_form_must_post_to_its_own_host():
    policy = DiscoveryPolicy(START)
    form = {
        "action": "https://elsewhere.example-utility.test/collect",
        "method": "post",
        "fields": {"Username": "", "Password": ""},
    }
    with pytest.raises(EndpointNotAllowed):
        policy.approve_form(f"{AUTH}/login", form)


def test_policy_get_rules():
    policy = DiscoveryPolicy(START)
    with pytest.raises(EndpointNotAllowed):
        policy.check("GET", f"{SITE}/billing/statements")  # a link, not the start URL
    policy.check("GET", START)  # start URL; its redirect hops are now allowed
    policy.check("GET", f"{AUTH}/login?x=SYNTHETIC")
    policy.end_request()
    with pytest.raises(EndpointNotAllowed):
        policy.check("GET", f"{AUTH}/login?x=SYNTHETIC")
    for method, url in (
        ("GET", "http://example-utility.test/usage"),
        ("PUT", START),
        ("DELETE", START),
        ("GET", "https://example-utility.test:8443/usage"),
    ):
        with pytest.raises(EndpointNotAllowed):
            policy.check(method, url)


def test_policy_silverblaze_host_rules():
    policy = DiscoveryPolicy(START)
    policy.check("GET", START)
    # Shared usage endpoints are always fine.
    policy.check("GET", ENC_URL)
    policy.check("GET", GPP)
    # Non-usage Silverblaze paths are refused even mid-redirect.
    for path in ("/ExampleTown/Billing", "/Billing", "/api/Account/Details", "/api/Pay/Init"):
        with pytest.raises(EndpointNotAllowed):
            policy.check("GET", f"{SB}{path}")
    with pytest.raises(EndpointNotAllowed):
        policy.check("POST", f"{SB}/api/Pay/Init")
    # A direct-login start page may use its own utility prefix, nothing else.
    direct = DiscoveryPolicy(DIRECT_START)
    direct.check("GET", DIRECT_START)
    direct.check("GET", f"{SB}/ExampleTown/Home")
    with pytest.raises(EndpointNotAllowed):
        direct.check("GET", f"{SB}/OtherTown/Home")
    with pytest.raises(EndpointNotAllowed):
        direct.check("GET", f"{SB}/ExampleTownship")


def test_policy_rejects_bad_start_urls():
    for url in ("http://example-utility.test/", "https://user@example.test/", "nonsense"):
        with pytest.raises(ValueError):
            DiscoveryPolicy(url)


def test_cli_rejects_credentials_in_portal_url(capsys):
    with pytest.raises(SystemExit) as exc:
        probe.main(["--portal-url", "https://user@example.test/usage"])
    assert exc.value.code == 2


# --- review fixes -------------------------------------------------------------------


def test_policy_refuses_address_form_with_state_field():
    policy = DiscoveryPolicy(START)
    form = {
        "action": f"{SITE}/profile",
        "method": "post",
        "fields": {"state": "ID", "street": "1 Synthetic Way"},
    }
    with pytest.raises(EndpointNotAllowed):
        policy.approve_form(f"{SITE}/page", form)
    only_state = {"action": f"{SITE}/profile", "method": "post", "fields": {"state": "x"}}
    with pytest.raises(EndpointNotAllowed):
        policy.approve_form(f"{SITE}/page", only_state)
    with pytest.raises(EndpointNotAllowed):
        policy.check("POST", f"{SITE}/profile")
    # Subset check: a required OIDC field alongside a non-OIDC field is not an OIDC form.
    mixed = {
        "action": f"{SITE}/profile",
        "method": "post",
        "fields": {"code": "X", "street": "1 Synthetic Way"},
    }
    assert not is_oidc_form(mixed)
    with pytest.raises(EndpointNotAllowed):
        policy.approve_form(f"{SITE}/page", mixed)
    with pytest.raises(EndpointNotAllowed):
        policy.check("POST", f"{SITE}/profile")


def test_address_form_after_login_never_posted(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _redirect(m, START, f"{AUTH}/login?x=SYNTHETIC")
    _html(m, re.compile(rf"^{re.escape(AUTH)}/login\?.*$"), "sso_login_form.html")
    _redirect(m, f"{AUTH}/Account/Login", f"{AUTH}/callback?x=SYNTHETIC", method="post")
    _html(m, re.compile(rf"^{re.escape(AUTH)}/callback\?.*$"), "sso_form_post_with_address.html")
    _redirect(m, f"{SITE}/signin-oidc", START, method="post")
    _redirect(m, START, ENC_URL)
    _html(m, re.compile(rf"^{re.escape(SB)}/Page\?.*$"), "silverblaze_page.html")
    _mock_data_ok(m)
    assert probe.main(["--portal-url", START]) == 0
    posts = [(meth, u) for meth, u in _requested(m) if meth == "POST"]
    assert posts == [LOGIN_POST, ("POST", f"{SITE}/signin-oidc")]


def test_register_form_before_login_never_receives_password(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _redirect(m, START, f"{AUTH}/login?x=SYNTHETIC")
    _html(
        m,
        re.compile(rf"^{re.escape(AUTH)}/login\?.*$"),
        "sso_login_form_with_register.html",
    )
    _html(m, f"{AUTH}/Account/Login", "no_login_form.html", method="post")
    probe.main(["--portal-url", START])
    posts = [(meth, u) for meth, u in _requested(m) if meth == "POST"]
    assert posts == [LOGIN_POST]
    sent = m.requests[(LOGIN_POST[0], URL(LOGIN_POST[1]))][0].kwargs["data"]
    assert sent["Username"] == USER
    assert sent["Password"] == PASSWORD
    for key, calls in m.requests.items():
        if str(key[1]) != LOGIN_POST[1]:
            assert PASSWORD not in repr([c.kwargs for c in calls])


def test_mixed_oidc_and_address_form_never_posted(fast_http, prompts, mock_aioresponse):
    m = mock_aioresponse
    _redirect(m, START, f"{AUTH}/login?x=SYNTHETIC")
    _html(m, re.compile(rf"^{re.escape(AUTH)}/login\?.*$"), "sso_login_form.html")
    _redirect(m, f"{AUTH}/Account/Login", f"{AUTH}/callback?x=SYNTHETIC", method="post")
    m.add(
        re.compile(rf"^{re.escape(AUTH)}/callback\?.*$"),
        status=200,
        body=(
            '<form method="post" action="https://example-utility.test/profile">'
            '<input name="code" value="X"><input name="street" value="1 Synthetic Way"></form>'
        ),
    )
    probe.main(["--portal-url", START])
    posts = [(meth, u) for meth, u in _requested(m) if meth == "POST"]
    assert posts == [LOGIN_POST]


def test_login_candidate_rejects_multi_password_form():
    confirm = forms_of(load_fixture_text("probe/sso_login_form_with_confirm.html"))
    assert confirm[0]["action"] == "/Account/Create"
    assert not _login_candidate(confirm[0])
    assert _login_candidate(confirm[1])
    policy = DiscoveryPolicy(START)
    with pytest.raises(EndpointNotAllowed):
        policy.approve_form(f"{AUTH}/login", confirm[0])
    register = forms_of(load_fixture_text("probe/sso_login_form_with_register.html"))[0]
    with pytest.raises(EndpointNotAllowed):
        policy.approve_form(f"{AUTH}/login", register)
    assert policy.approve_form(f"{AUTH}/login", confirm[1])[0] == "login"


def test_multi_password_form_before_login_never_receives_password(
    fast_http, prompts, mock_aioresponse
):
    m = mock_aioresponse
    _redirect(m, START, f"{AUTH}/login?x=SYNTHETIC")
    _html(
        m,
        re.compile(rf"^{re.escape(AUTH)}/login\?.*$"),
        "sso_login_form_with_confirm.html",
    )
    _html(m, f"{AUTH}/Account/Login", "no_login_form.html", method="post")
    probe.main(["--portal-url", START])
    posts = [(meth, u) for meth, u in _requested(m) if meth == "POST"]
    assert posts == [LOGIN_POST]
    for key, calls in m.requests.items():
        if str(key[1]) != LOGIN_POST[1]:
            assert PASSWORD not in repr([c.kwargs for c in calls])


def test_login_307_to_shared_endpoint_never_replays_credentials(
    fast_http, prompts, mock_aioresponse
):
    m = mock_aioresponse
    _redirect(m, START, f"{AUTH}/login?x=SYNTHETIC")
    _html(m, re.compile(rf"^{re.escape(AUTH)}/login\?.*$"), "sso_login_form.html")
    m.add(
        f"{AUTH}/Account/Login",
        method="post",
        status=307,
        headers={"Location": f"{SB}/Page"},
    )
    m.add(f"{SB}/Page", method="post", status=200, body="replayed")
    probe.main(["--portal-url", START])
    assert _requested(m) == [
        ("GET", START),
        ("GET", f"{AUTH}/login?x=SYNTHETIC"),
        LOGIN_POST,
    ]
    _, report = _report(prompts)
    assert "not allowed" in _step(report, "login")["error"]


def test_policy_shared_endpoint_post_refused_during_credential_post():
    policy = DiscoveryPolicy(START)
    policy.check("POST", f"{SB}/Page")  # outside a credential chain: shared endpoint
    form = _form(load_fixture_text("probe/sso_login_form.html"), 2)
    policy.approve_form(f"{AUTH}/login", form)
    policy.check("POST", f"{AUTH}/Account/Login")
    with pytest.raises(EndpointNotAllowed):
        policy.check("POST", f"{SB}/Page")  # a 307/308 replay of the credential body
    policy.check("GET", f"{SB}/Page")  # a plain GET hop is still fine
    policy.end_request()
    policy.check("POST", f"{SB}/Page")


def test_username_field_guess_order():
    from custom_components.silverblaze.api.discovery import _username_field

    assert _username_field({"AccountId": "", "Email": "", "Password": ""}) == "Email"
    assert _username_field({"user_hidden": "SYNTHETIC", "Password": ""}) is None
    assert _username_field({"user_hidden": "SYNTHETIC", "loginName": "", "Password": ""}) == (
        "loginName"
    )


def test_reserved_first_segment_gets_no_prefix():
    policy = DiscoveryPolicy(f"{SB}/api/Something")
    policy.check("GET", f"{SB}/api/Something")
    with pytest.raises(EndpointNotAllowed):
        policy.check("GET", f"{SB}/api/Account/Details")
    with pytest.raises(EndpointNotAllowed):
        DiscoveryPolicy(f"{SB}/Page").check("GET", f"{SB}/Page/Other")


def test_zone_for_label():
    assert zone_for_label("America/Boise") == "America/Boise"
    assert zone_for_label("Arizona") == "America/Phoenix"
    assert zone_for_label("Mountain Standard Time (Arizona)") == "America/Phoenix"
    assert zone_for_label("Mountain") == "America/Denver"
    assert zone_for_label("nonsense") == "UTC"


def test_discovery_steps_carry_seconds(fast_http, prompts, mock_aioresponse):
    _mock_sso_chain(mock_aioresponse)
    _mock_data_ok(mock_aioresponse)
    probe.main(["--portal-url", START])
    _, report = _report(prompts)
    for name in ("start_page", "login", "form_post"):
        assert isinstance(_step(report, name)["seconds"], (int, float))

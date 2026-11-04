"""Idaho Power login module: redirect chain, classification, registry, log hygiene."""

from __future__ import annotations

import logging
import re
from unittest.mock import patch

import aiohttp
import pytest

from custom_components.silverblaze.api import (
    CannotConnect,
    EndpointNotAllowed,
    InvalidAuth,
    LoginFlowChanged,
    SilverblazeHttp,
    build_allowlist,
)
from custom_components.silverblaze.api.utilities import get_utilities, get_utility
from custom_components.silverblaze.api.utilities.idahopower import _MAX_FOLLOW_HOPS, IdahoPower

from .conftest import load_fixture_text

USER = "sentinel.user@example.com"
PASSWORD = "SentinelPassw0rd!"
TOOLS = "https://tools.idahopower.com"
AUTH = "https://auth.idahopower.com"
SB = "https://acewebsite.silverblaze.com"
USAGE = f"{TOOLS}/MyAccount/Usage"
AUTHORIZE = re.compile(rf"^{re.escape(AUTH)}/connect/authorize\?.*$")
LOGIN_PAGE = re.compile(rf"^{re.escape(AUTH)}/Account/Login\?.*$")
LOGIN_POST = f"{AUTH}/Account/Login"
CALLBACK = re.compile(rf"^{re.escape(AUTH)}/connect/authorize/callback\?.*$")
SIGNIN_OIDC = f"{TOOLS}/MyAccount/signin-oidc"
SB_PAGE = re.compile(rf"^{re.escape(SB)}/Page\?.*$")
ENC_URL = f"{SB}/Page?enc=SYNTHETIC_ENC%2BVALUE%3D"
ENC_VALUE = "SYNTHETIC_ENC+VALUE="


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
async def http():
    ft = FakeTime()
    async with aiohttp.ClientSession() as session:
        yield SilverblazeHttp(
            session, build_allowlist(IdahoPower.login_endpoints()), sleep=ft.sleep, clock=ft.clock
        )


def _html(m, url, name, status=200, method="get"):
    m.add(url, method=method, status=status, body=load_fixture_text(f"idahopower/{name}"))


def _redirect(m, url, location, method="get", status=302):
    m.add(url, method=method, status=status, headers={"Location": location})


def _login_form_chain(m):
    _redirect(m, USAGE, f"{AUTH}/connect/authorize?client_id=SYNTHETIC")
    _redirect(m, AUTHORIZE, f"{AUTH}/Account/Login?ReturnUrl=SYNTHETIC")
    _html(m, LOGIN_PAGE, "login_form.html")


def _post_to_oidc(m):
    _redirect(m, LOGIN_POST, f"{AUTH}/connect/authorize/callback?x=SYNTHETIC", method="post")
    _html(m, CALLBACK, "oidc_form_post.html")


def _happy_chain(m, *, enc_in_url=True):
    _login_form_chain(m)
    _post_to_oidc(m)
    _redirect(m, SIGNIN_OIDC, USAGE, method="post")
    _redirect(m, USAGE, ENC_URL if enc_in_url else f"{SB}/Page")


async def test_happy_path_returns_enc(http, mock_aioresponse):
    _happy_chain(mock_aioresponse)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    assert await IdahoPower.login(http, USER, PASSWORD) == ENC_VALUE


async def test_login_post_carries_credentials_and_hidden_fields(http, mock_aioresponse):
    _happy_chain(mock_aioresponse)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    await IdahoPower.login(http, USER, PASSWORD)
    post = next(
        v for k, v in mock_aioresponse.requests.items() if k[0] == "POST" and "Account" in str(k[1])
    )
    data = post[0].kwargs["data"]
    assert data["Username"] == USER
    assert data["Password"] == PASSWORD
    assert data["RememberLogin"] == "true"
    assert data["button"] == "login"
    assert data["__RequestVerificationToken"] == "SYNTHETIC_RVT_0123456789"
    assert post[0].kwargs["headers"]["Origin"] == AUTH


async def test_tools_host_usage_requested_again_after_login(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(mock_aioresponse, SIGNIN_OIDC, f"{TOOLS}/MyAccount/Usage", method="post", status=303)
    # First usage hit after sign-in lands on tools with 200, so a second GET is needed.
    mock_aioresponse.add(USAGE, status=200, body=load_fixture_text("idahopower/no_form.html"))
    _redirect(mock_aioresponse, USAGE, ENC_URL)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    assert await IdahoPower.login(http, USER, PASSWORD) == ENC_VALUE


async def test_enc_html_fallback(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(mock_aioresponse, SIGNIN_OIDC, USAGE, method="post")
    _redirect(mock_aioresponse, USAGE, f"{SB}/Page")
    _html(mock_aioresponse, re.compile(rf"^{re.escape(SB)}/Page$"), "silverblaze_page.html")
    assert await IdahoPower.login(http, USER, PASSWORD) == "SYNTHETIC_ENC+FALLBACK="


async def test_invalid_credentials(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _html(mock_aioresponse, LOGIN_POST, "login_form_with_error.html", method="post")
    with pytest.raises(InvalidAuth):
        await IdahoPower.login(http, USER, PASSWORD)


async def test_invalid_credentials_answered_with_400(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _html(mock_aioresponse, LOGIN_POST, "login_rejected_400.html", status=400, method="post")
    with pytest.raises(InvalidAuth):
        await IdahoPower.login(http, USER, PASSWORD)


async def test_400_without_form_is_login_flow_changed(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    mock_aioresponse.add(LOGIN_POST, method="post", status=400, body="")
    with pytest.raises(LoginFlowChanged):
        await IdahoPower.login(http, USER, PASSWORD)


async def test_invalid_auth_wins_over_extra_post_form(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _html(mock_aioresponse, LOGIN_POST, "login_error_with_extra_form.html", method="post")
    with pytest.raises(InvalidAuth):
        await IdahoPower.login(http, USER, PASSWORD)
    requested = [str(k[1]) for k in mock_aioresponse.requests]
    assert not any("ExternalLogin" in url for url in requested)


async def test_credential_post_timeout_sent_once(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    mock_aioresponse.add(LOGIN_POST, method="post", exception=TimeoutError())
    with pytest.raises(CannotConnect):
        await IdahoPower.login(http, USER, PASSWORD)
    posts = [v for k, v in mock_aioresponse.requests.items() if k[0] == "POST"]
    assert sum(len(v) for v in posts) == 1


async def test_oidc_hop_timeout_sent_once(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    mock_aioresponse.add(SIGNIN_OIDC, method="post", exception=TimeoutError())
    with pytest.raises(CannotConnect):
        await IdahoPower.login(http, USER, PASSWORD)
    sent = [v for k, v in mock_aioresponse.requests.items() if str(k[1]) == SIGNIN_OIDC]
    assert sum(len(v) for v in sent) == 1


async def test_enc_in_url_keeps_literal_plus(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(mock_aioresponse, SIGNIN_OIDC, USAGE, method="post")
    _redirect(mock_aioresponse, USAGE, f"{SB}/Page?enc=SYNTHETIC+ENC/VAL%3D")
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    assert await IdahoPower.login(http, USER, PASSWORD) == "SYNTHETIC+ENC/VAL="


def test_enc_html_regex_is_anchored():
    from custom_components.silverblaze.api.utilities.idahopower import _ENC_HTML_RE

    assert not _ENC_HTML_RE.search('<a href="/x?xenc=BAD" data-enc=BAD2>')
    assert _ENC_HTML_RE.search('<a href="/x?a=1&enc=GOOD">').group(1) == "GOOD"


async def test_flow_changed_no_login_form(http, mock_aioresponse):
    _redirect(mock_aioresponse, USAGE, f"{AUTH}/connect/authorize?client_id=SYNTHETIC")
    _html(mock_aioresponse, AUTHORIZE, "no_form.html")
    with pytest.raises(LoginFlowChanged):
        await IdahoPower.login(http, USER, PASSWORD)


async def test_flow_changed_no_handoff(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(mock_aioresponse, SIGNIN_OIDC, USAGE, method="post")
    # Stays on tools both times: no redirect to Silverblaze.
    _html(mock_aioresponse, USAGE, "no_form.html")
    _html(mock_aioresponse, USAGE, "no_form.html")
    with pytest.raises(LoginFlowChanged):
        await IdahoPower.login(http, USER, PASSWORD)


async def test_flow_changed_no_enc(http, mock_aioresponse):
    _happy_chain(mock_aioresponse, enc_in_url=False)
    _html(mock_aioresponse, re.compile(rf"^{re.escape(SB)}/Page$"), "no_form.html")
    with pytest.raises(LoginFlowChanged):
        await IdahoPower.login(http, USER, PASSWORD)


async def test_flow_changed_403_on_usage_page(http, mock_aioresponse):
    mock_aioresponse.add(USAGE, status=403, body="Forbidden")
    with pytest.raises(LoginFlowChanged):
        await IdahoPower.login(http, USER, PASSWORD)


async def test_flow_changed_non_login_auth_page_after_post(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _html(mock_aioresponse, LOGIN_POST, "no_form.html", method="post")
    with pytest.raises(LoginFlowChanged):
        await IdahoPower.login(http, USER, PASSWORD)


async def test_server_error_is_cannot_connect(http, mock_aioresponse):
    for _ in range(4):  # initial try + 3 retries
        mock_aioresponse.add(USAGE, status=503, body="down")
    with pytest.raises(CannotConnect):
        await IdahoPower.login(http, USER, PASSWORD)


def test_registry_lists_idahopower():
    assert "idahopower" in get_utilities()
    assert get_utility("idahopower") is IdahoPower
    assert IdahoPower.name() == "Idaho Power"
    assert IdahoPower.client_id() == "192"
    assert IdahoPower.timezone() == "America/Denver"
    assert re.fullmatch(r"[a-z0-9]+", IdahoPower.slug())


def test_union_allowlist_is_exactly_the_ten_documented_pairs():
    allow = build_allowlist(IdahoPower.login_endpoints())
    assert allow.endpoints == {
        ("tools.idahopower.com", "/MyAccount/Usage"),
        ("tools.idahopower.com", "/MyAccount/signin-oidc"),
        ("auth.idahopower.com", "/connect/authorize"),
        ("auth.idahopower.com", "/connect/authorize/callback"),
        ("auth.idahopower.com", "/Account/Login"),
        ("acewebsite.silverblaze.com", "/Page"),
        ("acewebsite.silverblaze.com", "/api/Page/GetPageParameters"),
        ("acewebsite.silverblaze.com", "/api/DataDownload/Meta"),
        ("acewebsite.silverblaze.com", "/api/ConsumptionV3/Init"),
        ("acewebsite.silverblaze.com", "/api/ConsumptionV3/Data"),
    }


async def test_no_credentials_in_debug_logs(http, mock_aioresponse, caplog):
    caplog.set_level(logging.DEBUG)
    _happy_chain(mock_aioresponse)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    await IdahoPower.login(http, USER, PASSWORD)
    mock_aioresponse.clear()
    _login_form_chain(mock_aioresponse)
    _html(mock_aioresponse, LOGIN_POST, "login_form_with_error.html", method="post")
    with pytest.raises(InvalidAuth) as exc:
        await IdahoPower.login(http, USER, PASSWORD)
    blob = caplog.text + str(exc.value)
    assert PASSWORD not in blob
    assert USER not in blob


def test_slug_validation_duplicates_and_abstract_intermediates():
    from custom_components.silverblaze.api.utilities import base

    before = dict(base._REGISTRY)
    try:
        with pytest.raises(ValueError):

            class Bad(IdahoPower):
                @classmethod
                def slug(cls) -> str:
                    return "Bad-Slug"

        with pytest.raises(ValueError):

            class Dup(IdahoPower):
                pass  # inherits slug "idahopower", a different class

        class Intermediate(base.UtilityModule):  # still abstract: no slug
            pass

        assert before == base._REGISTRY
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(before)


BOUNCE_LOCATION = f"{TOOLS}/MyAccount/Account/Login?ReturnUrl=SYNTHETIC"


def _bounced_attempt(m):
    _login_form_chain(m)
    _post_to_oidc(m)
    _redirect(m, SIGNIN_OIDC, BOUNCE_LOCATION, method="post")


def _count(m, method, path):
    return sum(len(v) for k, v in m.requests.items() if k[0] == method and k[1].path == path)


async def test_single_bounce_restarts_login_and_succeeds(http, mock_aioresponse, caplog):
    caplog.set_level(logging.DEBUG)
    _bounced_attempt(mock_aioresponse)
    _happy_chain(mock_aioresponse)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    assert await IdahoPower.login(http, USER, PASSWORD) == ENC_VALUE
    assert _count(mock_aioresponse, "POST", "/Account/Login") == 2
    assert _count(mock_aioresponse, "POST", "/MyAccount/signin-oidc") == 2
    assert _count(mock_aioresponse, "GET", "/MyAccount/Account/Login") == 0
    assert "bounced" in caplog.text
    assert PASSWORD not in caplog.text


async def test_bounce_path_compare_is_case_insensitive(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(mock_aioresponse, SIGNIN_OIDC, f"{TOOLS}/myaccount/account/LOGIN", method="post")
    _happy_chain(mock_aioresponse)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    assert await IdahoPower.login(http, USER, PASSWORD) == ENC_VALUE
    assert _count(mock_aioresponse, "GET", "/myaccount/account/LOGIN") == 0


async def test_double_bounce_raises_cannot_connect(http, mock_aioresponse):
    _bounced_attempt(mock_aioresponse)
    _bounced_attempt(mock_aioresponse)
    with pytest.raises(CannotConnect) as exc:
        await IdahoPower.login(http, USER, PASSWORD)
    assert str(exc.value) == "Utility sign-in was bounced back to its login page"
    assert _count(mock_aioresponse, "POST", "/Account/Login") == 2
    assert _count(mock_aioresponse, "GET", "/MyAccount/Account/Login") == 0


async def test_cookies_cleared_exactly_once_between_attempts(http, mock_aioresponse):
    _bounced_attempt(mock_aioresponse)
    _happy_chain(mock_aioresponse)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    with patch.object(http, "clear_cookies", wraps=http.clear_cookies) as spy:
        await IdahoPower.login(http, USER, PASSWORD)
    spy.assert_called_once_with()


async def test_no_cookie_clear_without_bounce(http, mock_aioresponse):
    _happy_chain(mock_aioresponse)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    with patch.object(http, "clear_cookies") as spy:
        await IdahoPower.login(http, USER, PASSWORD)
    spy.assert_not_called()


@pytest.mark.parametrize("failure", [{"status": 500}, {"exception": TimeoutError()}])
async def test_credential_post_failure_is_not_replayed(http, mock_aioresponse, failure):
    _login_form_chain(mock_aioresponse)
    mock_aioresponse.add(LOGIN_POST, method="post", **failure)
    # A replaying implementation would find a full second chain and POST again.
    _login_form_chain(mock_aioresponse)
    mock_aioresponse.add(LOGIN_POST, method="post", **failure)
    with pytest.raises(CannotConnect):
        await IdahoPower.login(http, USER, PASSWORD)
    assert _count(mock_aioresponse, "POST", "/Account/Login") == 1


@pytest.mark.parametrize("failure", [{"status": 500}, {"exception": TimeoutError()}])
async def test_signin_oidc_post_failure_is_not_replayed(http, mock_aioresponse, failure):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    mock_aioresponse.add(SIGNIN_OIDC, method="post", **failure)
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    mock_aioresponse.add(SIGNIN_OIDC, method="post", **failure)
    with pytest.raises(CannotConnect):
        await IdahoPower.login(http, USER, PASSWORD)
    assert _count(mock_aioresponse, "POST", "/MyAccount/signin-oidc") == 1
    assert _count(mock_aioresponse, "POST", "/Account/Login") == 1


@pytest.mark.parametrize("status", [307, 308])
async def test_oidc_hop_307_308_refused_without_replay(http, mock_aioresponse, status):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(mock_aioresponse, SIGNIN_OIDC, USAGE, method="post", status=status)
    with pytest.raises(LoginFlowChanged):
        await IdahoPower.login(http, USER, PASSWORD)
    assert _count(mock_aioresponse, "POST", "/MyAccount/signin-oidc") == 1
    assert _count(mock_aioresponse, "GET", "/MyAccount/Usage") == 1  # initial only


async def test_followup_server_error_not_retried(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(mock_aioresponse, SIGNIN_OIDC, USAGE, method="post")
    mock_aioresponse.add(USAGE, status=500)
    mock_aioresponse.add(USAGE, status=500)
    with pytest.raises(CannotConnect):
        await IdahoPower.login(http, USER, PASSWORD)
    assert _count(mock_aioresponse, "GET", "/MyAccount/Usage") == 2  # initial + one follow-up


async def test_bounce_shaped_location_on_other_host_is_not_a_bounce(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(
        mock_aioresponse,
        SIGNIN_OIDC,
        "https://other.example.com/MyAccount/Account/Login",
        method="post",
    )
    with pytest.raises(EndpointNotAllowed):
        await IdahoPower.login(http, USER, PASSWORD)


async def test_trailing_slash_bounce_detected(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(mock_aioresponse, SIGNIN_OIDC, f"{TOOLS}/MyAccount/Account/Login/", method="post")
    _happy_chain(mock_aioresponse)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    assert await IdahoPower.login(http, USER, PASSWORD) == ENC_VALUE
    assert _count(mock_aioresponse, "GET", "/MyAccount/Account/Login/") == 0


async def test_two_step_bounce_via_usage_detected(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    _redirect(mock_aioresponse, SIGNIN_OIDC, USAGE, method="post")
    _redirect(mock_aioresponse, USAGE, BOUNCE_LOCATION)
    _happy_chain(mock_aioresponse)
    _html(mock_aioresponse, SB_PAGE, "no_form.html")
    assert await IdahoPower.login(http, USER, PASSWORD) == ENC_VALUE
    assert _count(mock_aioresponse, "GET", "/MyAccount/Account/Login") == 0


async def test_redirect_loop_stops_at_the_hop_cap(http, mock_aioresponse):
    _login_form_chain(mock_aioresponse)
    _post_to_oidc(mock_aioresponse)
    loop = f"{TOOLS}/MyAccount/Usage"
    _redirect(mock_aioresponse, SIGNIN_OIDC, loop, method="post")
    for _ in range(_MAX_FOLLOW_HOPS + 5):
        _redirect(mock_aioresponse, loop, loop)
    with pytest.raises(LoginFlowChanged, match="Too many redirects"):
        await IdahoPower.login(http, USER, PASSWORD)
    loop_requests = sum(len(v) for k, v in mock_aioresponse.requests.items() if str(k[1]) == loop)
    # The login chain's first Usage GET, then one request per followed redirect.
    assert loop_requests == _MAX_FOLLOW_HOPS + 1

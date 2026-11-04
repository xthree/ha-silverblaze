"""Transport behaviour: policy, redirects, pacing, retries, redaction."""

from __future__ import annotations

import logging
import re
import traceback

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from custom_components.silverblaze.api import (
    API_HEADERS,
    BROWSER_HEADERS,
    CannotConnect,
    EndpointNotAllowed,
    SilverblazeHttp,
    UnexpectedResponse,
    build_allowlist,
)
from custom_components.silverblaze.api.redact import host_path, redact_mapping, redact_text

from .test_allowlist import FORBIDDEN

HOST = "https://acewebsite.silverblaze.com"
PAGE = f"{HOST}/Page"
META = f"{HOST}/api/DataDownload/Meta"
DATA = f"{HOST}/api/ConsumptionV3/Data"


class FakeTime:
    """Fake clock whose sleep advances it and records every wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture
async def http(ft):
    async with aiohttp.ClientSession() as session:
        yield SilverblazeHttp(session, build_allowlist([]), sleep=ft.sleep, clock=ft.clock)


@pytest.fixture
def mocked(mock_aioresponse):
    return mock_aioresponse


def requested_urls(m: aioresponses) -> list[str]:
    return [str(url) for (_method, url) in m.requests]


@pytest.mark.parametrize("url", FORBIDDEN)
async def test_forbidden_request_makes_no_http_call(http, mocked, url):
    with pytest.raises(EndpointNotAllowed):
        await http.request("POST", url, json={})
    assert not mocked.requests


async def test_allowed_request_returns_response(http, mocked):
    mocked.get(PAGE, status=200, body="hello")
    resp = await http.request("GET", PAGE)
    assert (resp.status, resp.text, resp.url, resp.history) == (200, "hello", PAGE, ())


async def test_redirect_to_unlisted_url_refused_and_never_requested(http, mocked):
    evil = f"{HOST}/api/Pay/Post"
    mocked.get(PAGE, status=302, headers={"Location": evil})
    mocked.get(evil, status=200, body="paid")
    with pytest.raises(EndpointNotAllowed):
        await http.request("GET", PAGE)
    assert requested_urls(mocked) == [PAGE]


async def test_redirect_to_other_host_refused(http, mocked):
    mocked.get(PAGE, status=301, headers={"Location": "https://evil.example.test/Page"})
    with pytest.raises(EndpointNotAllowed):
        await http.request("GET", PAGE)
    assert requested_urls(mocked) == [PAGE]


async def test_redirect_to_allowed_url_followed_with_history(http, mocked):
    mocked.get(PAGE, status=302, headers={"Location": "/api/DataDownload/Meta?enc=x"})
    mocked.get(f"{META}?enc=x", status=200, body="ok")
    resp = await http.request("GET", PAGE)
    assert resp.status == 200
    assert resp.history == ((302, "acewebsite.silverblaze.com/Page"),)
    assert resp.url == f"{META}?enc=x"


async def test_no_follow_returns_redirect(http, mocked):
    mocked.get(PAGE, status=302, headers={"Location": META})
    resp = await http.request("GET", PAGE, follow_redirects=False)
    assert resp.status == 302
    assert requested_urls(mocked) == [PAGE]


async def test_too_many_redirects(http, mocked):
    mocked.get(PAGE, status=302, headers={"Location": META})
    mocked.get(META, status=302, headers={"Location": PAGE}, repeat=True)
    mocked.get(PAGE, status=302, headers={"Location": META}, repeat=True)
    with pytest.raises(CannotConnect):
        await http.request("GET", PAGE, max_redirects=3)


async def test_303_after_post_becomes_get_without_body(http, mocked):
    mocked.post(PAGE, status=303, headers={"Location": META})
    mocked.get(META, status=200, body="done")
    resp = await http.request("POST", PAGE, data={"a": "b"})
    assert resp.status == 200
    assert [m for (m, _u) in mocked.requests] == ["POST", "GET"]
    get_call = mocked.requests[("GET", mocked_key(mocked, "GET"))][0]
    assert not get_call.kwargs.get("data")
    assert not get_call.kwargs.get("json")


def mocked_key(m, method):
    return next(u for (mm, u) in m.requests if mm == method)


async def test_307_keeps_method_and_body(http, mocked):
    mocked.post(PAGE, status=307, headers={"Location": META})
    mocked.post(META, status=200, body="done")
    await http.request("POST", PAGE, json={"k": 1})
    assert [m for (m, _u) in mocked.requests] == ["POST", "POST"]
    call = mocked.requests[("POST", mocked_key(mocked, "POST"))][0]
    assert call.kwargs["json"] == {"k": 1}
    second = mocked.requests[next(k for k in mocked.requests if str(k[1]) == META)][0]
    assert second.kwargs["json"] == {"k": 1}


async def test_pacing_enforces_min_interval(http, mocked, ft):
    mocked.get(PAGE, status=200, repeat=True)
    await http.request("GET", PAGE)
    ft.now += 0.25
    await http.request("GET", PAGE)
    assert ft.sleeps == [pytest.approx(0.75)]
    ft.now += 5
    await http.request("GET", PAGE)
    assert ft.sleeps == [pytest.approx(0.75)]


async def test_pacing_applies_to_redirect_hops(http, mocked, ft):
    mocked.get(PAGE, status=302, headers={"Location": META})
    mocked.get(META, status=200)
    await http.request("GET", PAGE)
    assert ft.sleeps == [pytest.approx(1.0)]


@pytest.mark.parametrize("failure", ["timeout", "503", "500", "conn"])
async def test_retries_with_growing_backoff_then_cannot_connect(http, mocked, ft, failure):
    for _ in range(4):
        if failure == "timeout":
            mocked.get(PAGE, exception=TimeoutError())
        elif failure == "conn":
            mocked.get(PAGE, exception=aiohttp.ClientConnectionError("boom"))
        else:
            mocked.get(PAGE, status=int(failure))
    with pytest.raises(CannotConnect):
        await http.request("GET", PAGE)
    assert len(mocked.requests[("GET", next(iter(mocked.requests))[1])]) == 4
    # Backoff 2, 4, 8 (pacing waits are zero because backoff already exceeds 1 s).
    assert ft.sleeps == [2.0, 4.0, 8.0]


async def test_max_retries_override_limits_attempts(http, mocked, ft):
    mocked.get(PAGE, exception=TimeoutError())
    with pytest.raises(CannotConnect):
        await http.request("GET", PAGE, max_retries=0)
    assert len(mocked.requests[next(iter(mocked.requests))]) == 1
    assert ft.sleeps == []


async def test_max_retries_override_applies_to_redirect_hops(http, mocked):
    mocked.get(PAGE, status=302, headers={"Location": META})
    mocked.get(META, exception=TimeoutError())
    with pytest.raises(CannotConnect):
        await http.request("GET", PAGE, max_retries=0)
    assert (
        len(mocked.requests[("GET", next(k[1] for k in mocked.requests if "Meta" in str(k[1])))])
        == 1
    )


async def test_500_then_success_is_retried(http, mocked, ft):
    mocked.get(PAGE, status=500)
    mocked.get(PAGE, status=200, body="ok")
    resp = await http.request("GET", PAGE)
    assert resp.text == "ok"
    assert ft.sleeps == [2.0]


async def test_404_not_retried(http, mocked, ft):
    mocked.get(PAGE, status=404, body="nope")
    resp = await http.request("GET", PAGE)
    assert resp.status == 404
    assert len(mocked.requests[next(iter(mocked.requests))]) == 1
    assert ft.sleeps == []


async def test_headers_and_manual_redirects_passed_to_session(http, mocked):
    mocked.get(PAGE, status=200)
    await http.request("GET", PAGE, headers=API_HEADERS)
    call = next(iter(mocked.requests.values()))[0]
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["headers"]["Pragma"] == "no-cache"
    assert call.kwargs["timeout"].total == 90


def test_header_constants():
    assert "User-Agent" in BROWSER_HEADERS
    assert BROWSER_HEADERS["Accept"].startswith("text/html")
    assert "Accept-Language" in BROWSER_HEADERS
    assert API_HEADERS["Accept"] == "application/json, text/plain, */*"
    assert API_HEADERS["Content-Type"] == "application/json"
    assert API_HEADERS["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("how", ["status", "exception"])
async def test_secrets_absent_from_exception(http, mocked, how):
    url = f"{PAGE}?enc=SENTINEL_ENC&parameters=SENTINEL_PARAMS"
    for _ in range(4):
        if how == "status":
            mocked.get(url, status=503)
        else:
            mocked.get(url, exception=aiohttp.ClientConnectionError(url))
    with pytest.raises(CannotConnect) as ei:
        await http.request("GET", url)
    exc = ei.value
    text = "".join(traceback.format_exception(exc)) + str(exc) + repr(exc)
    assert "SENTINEL_ENC" not in text
    assert "SENTINEL_PARAMS" not in text
    assert exc.__suppress_context__
    assert "acewebsite.silverblaze.com/Page" in str(exc)


def test_redact_text():
    out = redact_text(
        "GET https://h.example.test/p?enc=AAA&x=1 failed; code=abc123 state=zzz "
        "mail me@example.com tok A1b2C3d4E5f6G7h8I9j0K1l2M3"
    )
    for leaked in ("AAA", "abc123", "zzz", "me@example.com", "A1b2C3d4E5f6G7h8I9j0K1l2M3", "x=1"):
        assert leaked not in out
    assert "h.example.test/p" in out
    assert "<email>" in out


def test_redact_text_letters_only_token():
    token = "abcdefghijklmnopqrstuvwxyzabcd"
    assert len(token) == 30
    assert token not in redact_text(f"value {token} end")


def test_redact_text_standard_base64_fully_redacted():
    b64 = "Zm9vYmFy+/Zm9vYmFyQUJDREVGRw/xyz+abcdef0123=="
    out = redact_text(f"blob {b64} end")
    assert out == "blob <redacted> end"
    for i in range(len(b64) - 7):
        assert b64[i : i + 8] not in out


def test_redact_text_strips_query_after_path():
    out = redact_text("failed at /api/thing?x=1&y=2 now")
    assert out == "failed at /api/thing now"
    assert "x=1" not in redact_text("h.example.test/p?x=1")


async def test_cannot_connect_message_keeps_long_allowlisted_path(http, mocked):
    url = f"{HOST}/api/ConsumptionV3/Data"
    for _ in range(4):
        mocked.get(url, status=503)
    with pytest.raises(CannotConnect) as ei:
        await http.request("GET", url)
    assert "acewebsite.silverblaze.com/api/ConsumptionV3/Data" in str(ei.value)


async def test_undecodable_body_raises_unexpected_response_without_body(http, mocked):
    mocked.get(PAGE, status=200, body=b"\xff\xfeSENTINEL_BODY\xff")
    with pytest.raises(UnexpectedResponse) as ei:
        await http.request("GET", PAGE)
    exc = ei.value
    text = "".join(traceback.format_exception(exc)) + str(exc) + repr(exc)
    assert "SENTINEL_BODY" not in text
    assert exc.__suppress_context__
    assert "acewebsite.silverblaze.com/Page" in str(exc)


def test_redact_mapping_recursive():
    out = redact_mapping(
        {"enc": "v", "Nested": [{"WebToken": "t", "ok": "fine", "AccountId": 5}], "n": 3}
    )
    assert out == {
        "enc": "<redacted>",
        "Nested": [{"WebToken": "<redacted>", "ok": "fine", "AccountId": "<redacted>"}],
        "n": 3,
    }


async def test_redirect_hops_debug_logged_host_path_only(http, mocked, caplog):
    caplog.set_level(logging.DEBUG)
    mocked.get(PAGE, status=302, headers={"Location": f"{META}?enc=SENTINEL_ENC_VALUE"})
    mocked.get(re.compile(rf"^{re.escape(META)}\?.*$"), status=200, body="ok")
    await http.request("GET", PAGE)
    lines = [r.getMessage() for r in caplog.records if r.name.endswith("api.http")]
    assert len(lines) == 2
    assert lines[0] == f"GET {host_path(PAGE)} -> 302"
    assert lines[1] == f"GET {host_path(META)} -> 200"
    assert "SENTINEL_ENC_VALUE" not in caplog.text
    assert "?" not in "".join(lines)


async def test_clear_cookies_empties_real_jar(ft):
    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        client = SilverblazeHttp(session, build_allowlist([]), sleep=ft.sleep, clock=ft.clock)
        jar.update_cookies({"sid": "SYNTHETIC"}, URL(HOST))
        assert len(jar) == 1
        client.clear_cookies()
        assert len(jar) == 0

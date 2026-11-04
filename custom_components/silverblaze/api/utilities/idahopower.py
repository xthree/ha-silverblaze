"""Idaho Power login: tools.idahopower.com -> auth.idahopower.com -> Silverblaze."""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urljoin, urlsplit

from ..exceptions import CannotConnect, InvalidAuth, LoginFlowChanged
from ..forms import Form, forms_of
from ..http import BROWSER_HEADERS, HttpResponse, SilverblazeHttp
from .base import UtilityModule

_LOGGER = logging.getLogger(__name__)

_TOOLS_HOST = "tools.idahopower.com"
_AUTH_HOST = "auth.idahopower.com"
_SILVERBLAZE_HOST = "acewebsite.silverblaze.com"
_USAGE_URL = f"https://{_TOOLS_HOST}/MyAccount/Usage"
_BOUNCE_PATH = "/MyAccount/Account/Login"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_FOLLOW_HOPS = 10
_MAX_FORM_POST_HOPS = 3
_OIDC_FIELDS = frozenset({"code", "id_token", "state"})
_ENC_HTML_RE = re.compile(r"(?<![A-Za-z0-9_-])enc=([^\"'&\s<]+)")


def _host(resp: HttpResponse) -> str:
    return (urlsplit(resp.url).hostname or "").lower()


class _SignInBounced(Exception):
    """The utility redirected the sign-in callback back to its own login page."""


def _is_bounce(url: str, location: str) -> bool:
    parts = urlsplit(urljoin(url, location))
    return (parts.hostname or "").lower() == _TOOLS_HOST and (
        parts.path.rstrip("/").lower() == _BOUNCE_PATH.lower()
    )


def _password_form(resp: HttpResponse) -> Form | None:
    for form in forms_of(resp.text):
        if "Password" in form["fields"]:
            return form
    return None


def _enc_of(resp: HttpResponse) -> str | None:
    for pair in urlsplit(resp.url).query.split("&"):
        key, _, value = pair.partition("=")
        if key == "enc" and value:
            return unquote(value)
    match = _ENC_HTML_RE.search(resp.text)
    return unquote(match.group(1)) if match else None


class IdahoPower(UtilityModule):
    """Idaho Power (Silverblaze ClientId 192)."""

    @classmethod
    def name(cls) -> str:
        return "Idaho Power"

    @classmethod
    def slug(cls) -> str:
        return "idahopower"

    @classmethod
    def client_id(cls) -> str:
        return "192"

    @classmethod
    def timezone(cls) -> str:
        return "America/Denver"

    @classmethod
    def login_endpoints(cls) -> frozenset[tuple[str, str]]:
        return frozenset(
            {
                (_TOOLS_HOST, "/MyAccount/Usage"),
                (_TOOLS_HOST, "/MyAccount/signin-oidc"),
                (_AUTH_HOST, "/connect/authorize"),
                (_AUTH_HOST, "/connect/authorize/callback"),
                (_AUTH_HOST, "/Account/Login"),
            }
        )

    @classmethod
    async def login(cls, http: SilverblazeHttp, username: str, password: str) -> str:
        # The utility intermittently bounces the sign-in callback back to its login page;
        # restart the whole login once on a fresh cookie jar before giving up.
        try:
            for attempt in (1, 2):
                try:
                    return await cls._login_once(http, username, password)
                except _SignInBounced:
                    if attempt == 2:
                        raise CannotConnect(
                            "Utility sign-in was bounced back to its login page"
                        ) from None
                    _LOGGER.debug("Utility sign-in bounced to its login page; retrying once")
                    # The identity provider session would skip the password form.
                    http.clear_cookies()
            raise AssertionError  # pragma: no cover
        finally:
            del password

    @staticmethod
    async def _follow(
        http: SilverblazeHttp, from_url: str, location: str, headers: dict[str, str]
    ) -> HttpResponse:
        """Follow redirects by hand, refusing a bounce to the login page before requesting it."""
        for _ in range(_MAX_FOLLOW_HOPS):
            if _is_bounce(from_url, location):
                raise _SignInBounced
            url = urljoin(from_url, location)
            resp = await http.request(
                "GET", url, headers=headers, follow_redirects=False, max_retries=0
            )
            if resp.status not in _REDIRECT_STATUSES or not resp.location:
                return resp
            from_url, location = url, resp.location
        raise LoginFlowChanged("Too many redirects after sign-in callback")

    @classmethod
    async def _login_once(cls, http: SilverblazeHttp, username: str, password: str) -> str:
        # 1. Usage page redirects through OIDC authorize to the login form.
        resp = await http.request("GET", _USAGE_URL, headers=BROWSER_HEADERS)
        if resp.status != 200 or _host(resp) != _AUTH_HOST:
            raise LoginFlowChanged("Login page was not served as expected")
        form = _password_form(resp)
        if form is None:
            raise LoginFlowChanged("Login form not found")

        # 2. POST credentials. The password is dropped from local structures right after.
        data = dict(form["fields"])
        data.update({"Username": username, "Password": password, "RememberLogin": "true"})
        if not data.get("button"):
            data["button"] = "login"
        login_url = resp.url
        try:
            resp = await http.request(
                "POST",
                urljoin(login_url, form["action"]) or login_url,
                data=data,
                headers={
                    **BROWSER_HEADERS,
                    "Origin": f"https://{_AUTH_HOST}",
                    "Referer": login_url,
                },
                max_retries=0,  # non-idempotent: a replay could lock the account
            )
        finally:
            data["Password"] = ""
            del data, password

        # Rejected credentials re-render the login form on the auth host; check this
        # before treating any other form on the page as an OIDC hop.
        if (
            _host(resp) == _AUTH_HOST
            and resp.status in (200, 400)
            and _password_form(resp) is not None
        ):
            raise InvalidAuth("Invalid username or password")

        # 2b. OIDC form_post auto-submit page(s): POST the fields on to the utility.
        for _ in range(_MAX_FORM_POST_HOPS):
            if _host(resp) == _SILVERBLAZE_HOST or resp.status != 200:
                break
            hop = [
                f
                for f in forms_of(resp.text)
                if f["method"] == "post"
                and f["action"].startswith("https://")
                and "Password" not in f["fields"]
                and _OIDC_FIELDS & f["fields"].keys()
            ]
            if not hop:
                break
            referer = resp.url
            hop_url = hop[0]["action"]
            hop_data = hop[0]["fields"]
            hop_headers = {**BROWSER_HEADERS, "Referer": referer}
            resp = await http.request(
                "POST",
                hop_url,
                data=hop_data,
                headers=hop_headers,
                follow_redirects=False,
                max_retries=0,  # one-time codes must not be replayed
            )
            if resp.status in (307, 308):
                raise LoginFlowChanged("Unexpected redirect after sign-in callback")
            location = resp.location
            if resp.status in _REDIRECT_STATUSES and location:
                resp = await cls._follow(http, hop_url, location, hop_headers)

        if _host(resp) == _AUTH_HOST:
            raise LoginFlowChanged("Unexpected page on the login host after sign-in")

        # 3. Back on the utility host: the usage page hands off to Silverblaze.
        if _host(resp) == _TOOLS_HOST and resp.status == 200:
            resp = await http.request("GET", _USAGE_URL, headers=BROWSER_HEADERS)
        if _host(resp) != _SILVERBLAZE_HOST or resp.status != 200:
            raise LoginFlowChanged("No handoff to Silverblaze after sign-in")

        # 4. enc from the final URL query, falling back to the page HTML.
        enc = _enc_of(resp)
        if not enc:
            raise LoginFlowChanged("No enc token found after handoff")
        return enc

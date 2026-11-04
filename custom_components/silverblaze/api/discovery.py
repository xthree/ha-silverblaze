"""Generic, read-only discovery of an unknown utility portal (probe tool only).

``DiscoveryPolicy`` is the request policy for ``silverblaze-probe --portal-url``. It is
never used by the integration, which only ever uses the static ``Allowlist``.

Read-only rules, enforced by the policy before any request is sent:

* https only;
* GET the start URL, and GET the redirect targets of an approved request;
* POST only to the action of a form the discovery code approved: a form with a password
  field (the login form, posted to the host that served it) or an auto-submit OIDC form
  (a ``code`` or ``id_token`` field, nothing outside the OIDC response set). Any other
  form is refused;
* on the Silverblaze host only the shared usage endpoints, plus (for a direct-login
  start page) GET/POST under the start page's own utility prefix.

Links are never followed and no other form is ever submitted. Step records carry only
HTTP status, ``host/path`` and form field *names*. No Home Assistant imports.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .allowlist import SILVERBLAZE_ENDPOINTS, SILVERBLAZE_HOST, _normalise
from .exceptions import EndpointNotAllowed, InvalidAuth, SilverblazeError
from .forms import Form, forms_of
from .http import BROWSER_HEADERS, HttpResponse, SilverblazeHttp
from .redact import host_path

SSO_HANDOFF = "sso_handoff"
DIRECT_LOGIN = "direct_login"
UNKNOWN_STYLE = "unknown"
COMPATIBLE = "compatible"
PARTIAL = "partially compatible"
INCOMPATIBLE = "not compatible"

_MAX_FORM_POST_HOPS = 3
_OIDC_REQUIRED = frozenset({"code", "id_token"})
_OIDC_FIELDS = frozenset(
    {
        "code",
        "id_token",
        "state",
        "session_state",
        "scope",
        "iss",
        "access_token",
        "token_type",
        "expires_in",
    }
)
_NOT_LOGIN_ACTION = re.compile(r"register|signup|forgot|reset|change", re.IGNORECASE)
_USER_EXACT = ("username", "email", "user", "login")
_RESERVED_PREFIXES = frozenset({"api", "page"})
_PASSWORD_NAME = re.compile(r"pass(word|wd)|^pass$|pwd", re.IGNORECASE)
_USER_NAME = re.compile(r"user|e-?mail|login|account", re.IGNORECASE)
_ENC_HTML_RE = re.compile(r"(?<![A-Za-z0-9_-])enc=([^\"'&\s<]+)")

# Silverblaze time-zone label -> IANA zone (used only to place the sample day).
_ZONE_LABELS = {
    "eastern": "America/New_York",
    "central": "America/Chicago",
    "mountain": "America/Denver",
    "pacific": "America/Los_Angeles",
    "alaska": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
    "est": "America/New_York",
    "edt": "America/New_York",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "akst": "America/Anchorage",
    "akdt": "America/Anchorage",
    "hst": "Pacific/Honolulu",
    "arizona": "America/Phoenix",
    "az": "America/Phoenix",
}


def zone_for_label(label: str) -> str:
    """IANA zone for a Silverblaze time-zone label (UTC when unrecognised)."""
    if "/" in label:  # an IANA region name; bare "MST"/"EST" are fixed-offset zones
        try:
            return ZoneInfo(label.strip()).key
        except ZoneInfoNotFoundError, ValueError, OSError:
            pass
    text = label.lower()
    if "arizona" in text:
        return "America/Phoenix"
    if text in _ZONE_LABELS:
        return _ZONE_LABELS[text]
    return next((zone for key, zone in _ZONE_LABELS.items() if len(key) > 4 and key in text), "UTC")


def is_password_field(name: str) -> bool:
    """True if a form field name looks like a password field."""
    return bool(_PASSWORD_NAME.search(name))


def is_oidc_form(form: Form) -> bool:
    """True for an auto-submit OIDC response form (code/id_token, only OIDC fields)."""
    names = {n.lower() for n in form["fields"]}
    return bool(_OIDC_REQUIRED & names) and names <= _OIDC_FIELDS


def _login_candidate(form: Form) -> bool:
    """A login form: exactly one password field, not a register / reset style action."""
    if sum(1 for n in form["fields"] if is_password_field(n)) != 1:
        return False
    return not _NOT_LOGIN_ACTION.search(urlsplit(form["action"]).path)


def _password_form(html: str) -> Form | None:
    return next((f for f in forms_of(html) if _login_candidate(f)), None)


def _has_password_field(html: str) -> bool:
    return any(_password_form_of(f) for f in forms_of(html))


def _full_key(url: str) -> tuple[str, str, str] | None:
    key = _normalise(url)
    if key is None:
        return None
    return key[0], key[1], urlsplit(url).query


class DiscoveryPolicy:
    """Read-only request policy for generic discovery (implements ``RequestPolicy``)."""

    def __init__(self, start_url: str) -> None:
        start = _full_key(start_url)
        if start is None:
            raise ValueError("start URL must be a plain https URL")
        self._start = start
        self._prefix: str | None = None
        if start[0] == SILVERBLAZE_HOST:
            segment = start[1].strip("/").split("/", 1)[0].lower()
            if segment and segment not in _RESERVED_PREFIXES:
                self._prefix = f"/{segment}"
        self._approved: set[tuple[str, str]] = set()
        self._in_chain = False
        self._post_chain = False  # an approved credential POST is in flight

    # --- approvals and request scoping ---------------------------------------------

    def approve_form(self, page_url: str, form: Form) -> tuple[str, str]:
        """Approve POSTing ``form`` (found on ``page_url``); return ``(kind, action_url)``.

        ``kind`` is ``"login"`` or ``"oidc"``. Anything else raises EndpointNotAllowed.
        """
        action = urljoin(page_url, form["action"]) if form["action"] else page_url
        key = _normalise(action)
        page = _normalise(page_url)
        if form["method"] != "post" or key is None or page is None:
            raise EndpointNotAllowed("Form is not an https POST form")
        names = form["fields"].keys()
        if any(is_password_field(n) for n in names):
            if not _login_candidate(form):
                raise EndpointNotAllowed("Form with password field is not a login form")
            if key[0] != page[0]:
                raise EndpointNotAllowed("Login form posts to a different host")
            kind = "login"
        elif is_oidc_form(form):
            kind = "oidc"
        else:
            raise EndpointNotAllowed("Form is neither a login nor an auto-submit OIDC form")
        if key[0] == SILVERBLAZE_HOST and not self._under_prefix(key[1]):
            raise EndpointNotAllowed(f"Form action not allowed: {host_path(action)}")
        self._approved.add(key)
        return kind, action

    def end_request(self) -> None:
        """Close the current request chain: redirect GETs and approvals lapse."""
        self._in_chain = False
        self._post_chain = False
        self._approved.clear()

    def _under_prefix(self, path: str) -> bool:
        if self._prefix is None:
            return False
        lowered = path.lower()
        return lowered == self._prefix or lowered.startswith(self._prefix + "/")

    # --- the policy ----------------------------------------------------------------

    def check(self, method: str, url: str) -> None:
        """Raise EndpointNotAllowed unless this request is a permitted read-only step."""
        verb = method.upper()
        full = _full_key(url)
        if full is None:
            raise EndpointNotAllowed(f"Endpoint not allowed: {verb} {host_path(url)}")
        host, path = full[0], full[1]
        key = (host, path)
        shared = host == SILVERBLAZE_HOST and key in SILVERBLAZE_ENDPOINTS
        # While a credential POST is in flight (a 307/308 would replay its body), the
        # shared-endpoint shortcut is GET only; a POST must be the approved action.
        if shared and (verb == "GET" or (verb == "POST" and not self._post_chain)):
            return
        if verb == "GET":
            is_start = full == self._start
            if is_start or self._in_chain:
                if host == SILVERBLAZE_HOST and not is_start and not self._under_prefix(path):
                    raise EndpointNotAllowed(f"Endpoint not allowed: GET {host_path(url)}")
                self._in_chain = True
                return
        elif verb == "POST" and key in self._approved:
            self._in_chain = True
            self._post_chain = True
            return
        raise EndpointNotAllowed(f"Endpoint not allowed: {verb} {host_path(url)}")


# --- discovery flow ---------------------------------------------------------------------

DataSteps = Callable[[SilverblazeHttp, dict[str, Any], list[str], str], Awaitable[None]]


class _Stop(Exception):
    """Internal: discovery cannot continue (message is fixed, vetted text)."""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _enc_of(resp: HttpResponse) -> str | None:
    for pair in urlsplit(resp.url).query.split("&"):
        key, _, value = pair.partition("=")
        if key == "enc" and value:
            return unquote(value)
    match = _ENC_HTML_RE.search(resp.text)
    return unquote(match.group(1)) if match else None


def _username_field(fields: dict[str, str]) -> str | None:
    candidates = [n for n in fields if not is_password_field(n)]
    for exact in _USER_EXACT:
        found = next((n for n in candidates if n.lower() == exact), None)
        if found:
            return found
    # Pattern match: only empty-valued fields (a hidden field carries a value).
    named = next((n for n in candidates if _USER_NAME.search(n) and not fields[n]), None)
    return named or next((n for n in candidates if not fields[n]), None)


async def run_discovery(
    http: SilverblazeHttp,
    policy: DiscoveryPolicy,
    start_url: str,
    username: str,
    password: str,
    data_steps: DataSteps | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Discover the login style of ``start_url``; return raw report data and secrets.

    ``data_steps`` (supplied by the probe) runs the shared Silverblaze calls once an
    ``enc`` token has been found.
    """
    secrets = [username, password]
    raw: dict[str, Any] = {
        "mode": "discovery",
        "login_style": UNKNOWN_STYLE,
        "steps": [],
        "series": [],
        "failed_step": None,
        "verdict": INCOMPATIBLE,
    }
    step = "start_page"

    async def get(url: str) -> HttpResponse:
        try:
            return await http.request("GET", url, headers=BROWSER_HEADERS)
        finally:
            policy.end_request()

    clock = {"t": time.monotonic()}

    def done(name: str, resp: HttpResponse, **extra: Any) -> None:
        now = time.monotonic()
        raw["steps"].append(
            {
                "step": name,
                "status": resp.status,
                "at": host_path(resp.url),
                "seconds": round(now - clock["t"], 1),
                **extra,
            }
        )
        clock["t"] = now
        print(f" [{len(raw['steps'])}] {name} ".ljust(44, ".") + f" {resp.status}")

    resp: HttpResponse | None = None
    try:
        clock["t"] = time.monotonic()
        resp = await get(start_url)
        done("start_page", resp)
        if resp.status != 200:
            raise _Stop("Start page did not return 200")

        # 1. The login form: a form with a password field. Nothing else is looked at.
        step = "login"
        clock["t"] = time.monotonic()
        form = _password_form(resp.text)
        if form is None:
            raise _Stop("No login form found")
        raw["steps"][-1]["field_names"] = list(form["fields"])
        login_host = _host(resp.url)
        direct = login_host == SILVERBLAZE_HOST
        if direct:
            raw["login_style"] = DIRECT_LOGIN
        _kind, action = policy.approve_form(resp.url, form)
        data = dict(form["fields"])
        pw_names = [n for n in data if is_password_field(n)]
        user_name = _username_field(data)
        if user_name is None:
            raise _Stop("No username field found")
        data[user_name] = username
        for name in pw_names:
            data[name] = password
        field_names = list(data)
        referer = resp.url
        try:
            resp = await http.request(
                "POST",
                action,
                data=data,
                headers={**BROWSER_HEADERS, "Referer": referer},
                max_retries=0,  # non-idempotent: a replay could lock the account
            )
        finally:
            policy.end_request()
            for name in pw_names:
                data[name] = ""
            del data, password
        done("login", resp, field_names=field_names)
        if (
            resp.status in (200, 400)
            and _host(resp.url) == login_host
            and _has_password_field(resp.text)
        ):
            raise InvalidAuth("Credentials were rejected or the login form re-rendered")

        # 2. Auto-submit OIDC form(s): POST their fields on, at most three hops.
        step = "form_post"
        for _ in range(_MAX_FORM_POST_HOPS):
            if _host(resp.url) == SILVERBLAZE_HOST or resp.status != 200:
                break
            hop = None
            for candidate in forms_of(resp.text):
                if candidate["method"] != "post" or _password_form_of(candidate):
                    continue
                if is_oidc_form(candidate):
                    hop = candidate
                    break
            if hop is None:
                break
            _kind, hop_action = policy.approve_form(resp.url, hop)
            referer = resp.url
            clock["t"] = time.monotonic()
            try:
                resp = await http.request(
                    "POST",
                    hop_action,
                    data=hop["fields"],
                    headers={**BROWSER_HEADERS, "Referer": referer},
                    max_retries=0,  # one-time codes must not be replayed
                )
            finally:
                policy.end_request()
            done("form_post", resp, field_names=list(hop["fields"]))

        # 3. Back on the utility host: the start page may hand off to Silverblaze.
        step = "handoff"
        if (
            _host(resp.url) != SILVERBLAZE_HOST
            and resp.status == 200
            and _host(resp.url) == _host(start_url)
        ):
            clock["t"] = time.monotonic()
            resp = await get(start_url)
            done("handoff", resp)
        on_sb = _host(resp.url) == SILVERBLAZE_HOST and resp.status == 200
        enc = _enc_of(resp) if on_sb else None
        if enc and not direct:
            raw["login_style"] = SSO_HANDOFF
        if not enc:
            if direct:
                raw["verdict"] = PARTIAL
            raise _Stop("No enc token found after sign-in")
        secrets.append(enc)
    except (_Stop, SilverblazeError, EndpointNotAllowed) as err:
        _fail(raw, step, resp, f"{type(err).__name__}: {err}")
        return raw, secrets
    except Exception as err:  # the probe must always produce a report
        # Unvetted exceptions may embed response data: keep only the class name.
        _fail(raw, step, resp, type(err).__name__)
        return raw, secrets

    if data_steps is not None:
        await data_steps(http, raw, secrets, enc)
    raw["verdict"] = COMPATIBLE if raw["failed_step"] is None else PARTIAL
    return raw, secrets


def _password_form_of(form: Form) -> bool:
    return any(is_password_field(n) for n in form["fields"])


def _fail(
    raw: dict[str, Any],
    step: str,
    resp: HttpResponse | None,
    error: str,
) -> None:
    """Record the failing step (annotating the last step record when it is the same one)."""
    last = raw["steps"][-1] if raw["steps"] else None
    if last is not None and last["step"] == step:
        last["error"] = error
    else:
        record: dict[str, Any] = {"step": step, "status": "failed", "error": error}
        if resp is not None:
            record["at"] = host_path(resp.url)
        raw["steps"].append(record)
    raw["failed_step"] = step
    print(f" FAIL {step}: {error.split(':', 1)[0]}")


__all__ = [
    "COMPATIBLE",
    "DIRECT_LOGIN",
    "INCOMPATIBLE",
    "PARTIAL",
    "SSO_HANDOFF",
    "UNKNOWN_STYLE",
    "DiscoveryPolicy",
    "run_discovery",
    "zone_for_label",
]

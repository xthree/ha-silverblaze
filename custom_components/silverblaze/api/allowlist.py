"""Endpoint allowlist: the only (host, path) pairs the client may ever contact."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from .exceptions import EndpointNotAllowed
from .redact import host_path

SILVERBLAZE_HOST = "acewebsite.silverblaze.com"

# Widening this set is a deliberate, reviewed change (a test pins the contents).
SILVERBLAZE_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        (SILVERBLAZE_HOST, "/Page"),
        (SILVERBLAZE_HOST, "/api/Page/GetPageParameters"),
        (SILVERBLAZE_HOST, "/api/DataDownload/Meta"),
        (SILVERBLAZE_HOST, "/api/ConsumptionV3/Init"),
        (SILVERBLAZE_HOST, "/api/ConsumptionV3/Data"),
    }
)


class RequestPolicy(Protocol):
    """Anything that can veto a request before it is sent."""

    def check(self, method: str, url: str) -> None:
        """Raise EndpointNotAllowed if the request must not be made."""


def _normalise(url: str) -> tuple[str, str] | None:
    """Return (host, path) for an acceptable https URL, else None."""
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != "https" or not host:
        return None
    if "@" in parts.netloc or port not in (None, 443):
        return None
    return host.lower(), parts.path


@dataclass(frozen=True)
class Allowlist:
    """Exact-match (host, path) allowlist over https."""

    endpoints: frozenset[tuple[str, str]]

    def check(self, method: str, url: str) -> None:
        """Raise EndpointNotAllowed unless scheme, host and path match exactly."""
        key = _normalise(url)
        if key is None or key not in self.endpoints:
            raise EndpointNotAllowed(f"Endpoint not allowed: {method.upper()} {host_path(url)}")


def build_allowlist(login_endpoints: Iterable[tuple[str, str]]) -> Allowlist:
    """Union of a login module's (host, path) pairs and the shared Silverblaze set."""
    login = {(host.lower(), path) for host, path in login_endpoints}
    return Allowlist(frozenset(login) | SILVERBLAZE_ENDPOINTS)

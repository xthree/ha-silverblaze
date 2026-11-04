"""Silverblaze client: no Home Assistant imports, no imports from the parent package."""

from __future__ import annotations

from .allowlist import SILVERBLAZE_ENDPOINTS, Allowlist, RequestPolicy, build_allowlist
from .client import SilverblazeClient
from .exceptions import (
    CannotConnect,
    EndpointNotAllowed,
    InvalidAuth,
    LoginFlowChanged,
    SessionExpired,
    SilverblazeError,
    UnexpectedResponse,
)
from .http import API_HEADERS, BROWSER_HEADERS, HttpResponse, SilverblazeHttp
from .models import PageContext
from .utilities import UtilityModule, get_utilities, get_utility


async def open_session(
    http: SilverblazeHttp,
    module_cls: type[UtilityModule],
    username: str,
    password: str,
) -> PageContext:
    """Log in through the utility module, then exchange ``enc`` for the page context."""
    enc = await module_cls.login(http, username, password)
    return await SilverblazeClient(http).get_page_parameters(enc)


__all__ = [
    "API_HEADERS",
    "BROWSER_HEADERS",
    "SILVERBLAZE_ENDPOINTS",
    "Allowlist",
    "CannotConnect",
    "EndpointNotAllowed",
    "HttpResponse",
    "InvalidAuth",
    "LoginFlowChanged",
    "PageContext",
    "RequestPolicy",
    "SessionExpired",
    "SilverblazeClient",
    "SilverblazeError",
    "SilverblazeHttp",
    "UnexpectedResponse",
    "UtilityModule",
    "build_allowlist",
    "get_utilities",
    "get_utility",
    "open_session",
]

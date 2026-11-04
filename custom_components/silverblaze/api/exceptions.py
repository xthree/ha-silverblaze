"""Error taxonomy for the Silverblaze client."""

from __future__ import annotations


class SilverblazeError(Exception):
    """Base class for expected, recoverable client errors."""


class InvalidAuth(SilverblazeError):
    """The utility rejected the supplied credentials."""


class CannotConnect(SilverblazeError):
    """The utility or portal could not be reached (timeouts, 5xx, DNS)."""


class UnexpectedResponse(CannotConnect):
    """A response had a shape the client does not understand."""


class LoginFlowChanged(SilverblazeError):
    """The login flow no longer looks like the one this client knows."""


class SessionExpired(SilverblazeError):
    """The web token expired; a fresh login is required."""


class EndpointNotAllowed(Exception):
    """A request targeted an endpoint outside the allowlist.

    Deliberately NOT a SilverblazeError: this is a programming error and must
    never be swallowed by handlers that catch the client's expected errors.
    """

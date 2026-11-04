"""Login-module interface; concrete modules self-register on subclassing."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import ClassVar

from ..http import SilverblazeHttp

_SLUG_RE = re.compile(r"[a-z0-9]+")
_REGISTRY: dict[str, type[UtilityModule]] = {}


class UtilityModule(ABC):
    """A utility whose portal login yields a Silverblaze ``enc`` token."""

    _registry: ClassVar[dict[str, type[UtilityModule]]] = _REGISTRY

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        try:
            slug = cls.slug()
        except NotImplementedError:
            return  # still abstract: an intermediate base, not a real utility
        if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
            raise ValueError(f"Invalid utility slug (expected [a-z0-9]+): {slug!r}")
        existing = _REGISTRY.get(slug)
        if existing is not None and existing is not cls:
            raise ValueError(f"Duplicate utility slug: {slug}")
        _REGISTRY[slug] = cls

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Display name."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def slug(cls) -> str:
        """Lower-case ``a-z0-9`` identifier used in statistic IDs."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def client_id(cls) -> str:
        """Silverblaze ClientId (informational)."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def timezone(cls) -> str:
        """IANA time zone of the utility."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def login_endpoints(cls) -> frozenset[tuple[str, str]]:
        """Allowlisted (host, path) pairs this module's login may contact."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    async def login(cls, http: SilverblazeHttp, username: str, password: str) -> str:
        """Log in and return ``enc``.

        Raises InvalidAuth, CannotConnect or LoginFlowChanged.
        """
        raise NotImplementedError

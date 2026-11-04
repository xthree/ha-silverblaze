"""Registry of utility login modules."""

from __future__ import annotations

from . import idahopower  # noqa: F401  (import registers the module)
from .base import _REGISTRY, UtilityModule


def get_utilities() -> dict[str, type[UtilityModule]]:
    """Return a copy of the slug -> module-class registry."""
    return dict(_REGISTRY)


def get_utility(slug: str) -> type[UtilityModule]:
    """Return the module class for ``slug`` (KeyError if unknown)."""
    return _REGISTRY[slug]


__all__ = ["UtilityModule", "get_utilities", "get_utility"]

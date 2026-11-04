"""Redaction helpers shared by logging, exceptions and any report writer."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

REDACTED = "<redacted>"
EMAIL = "<email>"

SENSITIVE_KEYS = (
    "enc",
    "parameters",
    "WebToken",
    "code",
    "id_token",
    "state",
    "session_state",
    "AccountId",
    "CustomerId",
    "PremiseId",
    "collectionId",
)
# Extra names only used for mapping redaction (never appear as query keys in practice).
_MAPPING_EXTRA_KEYS = ("password", "authorization", "cookie", "set-cookie")
_SENSITIVE_LOWER = frozenset(k.lower() for k in (*SENSITIVE_KEYS, *_MAPPING_EXTRA_KEYS))

_KEY_ALT = "|".join(re.escape(k) for k in SENSITIVE_KEYS)
_QUERY_URL_RE = re.compile(r"(https?://[^\s?#\"'<>]*)\?[^\s#\"'<>]*")
_KEY_VALUE_RE = re.compile(rf"(?<![A-Za-z0-9_])({_KEY_ALT})=[^&\s\"'<>]*", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A query string following a path-like token that starts with "/".
_PATH_QUERY_RE = re.compile(r"(/[^\s?#\"'<>]*)\?[^\s#\"'<>]*")
# Header-style lines ("Set-Cookie: ..."), bearer tokens and cookie pairs.
_HEADER_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(set-cookie|cookie|authorization|proxy-authorization)\s*[:=]\s*[^\r\n]*",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\b(Bearer|Basic)\s+[^\s\"'<>]+", re.IGNORECASE)
# Opaque runs: 24+ token characters (base64 / base64url / hex / ids).
_OPAQUE_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


def host_path(url: str) -> str:
    """Return ``host/path`` for a URL: no scheme, credentials, query or fragment."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return "<invalid-url>"
    return f"{host}{parts.path}"


def redact_text(text: str) -> str:
    """Remove query strings, sensitive key values, e-mail addresses and opaque tokens.

    Long path-like text is also caught by the opaque rule; callers that already hold a
    vetted ``host/path`` (see ``host_path``) should embed it verbatim instead.
    """
    text = _HEADER_RE.sub(lambda m: f"{m.group(1)}: {REDACTED}", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _QUERY_URL_RE.sub(r"\1", text)
    text = _PATH_QUERY_RE.sub(r"\1", text)
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    text = _EMAIL_RE.sub(EMAIL, text)
    return _OPAQUE_RE.sub(REDACTED, text)


def redact_mapping(obj: Any) -> Any:
    """Return a copy of ``obj`` with sensitive keys blanked and strings redacted."""
    if isinstance(obj, dict):
        return {
            k: REDACTED
            if isinstance(k, str) and k.lower() in _SENSITIVE_LOWER
            else redact_mapping(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact_mapping(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def redact_headers(headers: Any) -> dict[str, str]:
    """Return a copy of a header mapping with credential headers blanked."""
    return {
        str(k): REDACTED if str(k).lower() in _SENSITIVE_LOWER else redact_text(str(v))
        for k, v in dict(headers).items()
    }

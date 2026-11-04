"""Guard against real identifiers or secrets landing in test fixtures."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from custom_components.silverblaze.api import build_allowlist
from custom_components.silverblaze.api.utilities.idahopower import IdahoPower

FIXTURES_DIR = Path(__file__).parent / "fixtures"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
OPAQUE_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+")
_ENDPOINTS = build_allowlist(IdahoPower.login_endpoints()).endpoints
# Known-safe literals that would otherwise look like opaque runs.
SAFE_LITERALS = tuple(
    sorted(
        {"__RequestVerificationToken", "https://auth.idahopower.com/Account/ExternalLogin"}
        | {f"https://{host}{path}" for host, path in _ENDPOINTS},
        key=len,
        reverse=True,
    )
)
ALLOWED_EMAIL_DOMAINS = ("@example.com", "@example.test")


def _fixture_files() -> list[Path]:
    return [p for p in FIXTURES_DIR.rglob("*") if p.is_file() and p.name != ".gitkeep"]


def find_problems(text: str) -> list[str]:
    """Return a description of every hygiene violation found in text."""
    problems = [
        f"e-mail address: {m.group(0)}"
        for m in EMAIL_RE.finditer(text)
        if not m.group(0).lower().endswith(ALLOWED_EMAIL_DOMAINS)
    ]
    problems += [f"JWT-like string: {m.group(0)[:12]}..." for m in JWT_RE.finditer(text)]
    scrubbed = text
    for literal in SAFE_LITERALS:
        scrubbed = scrubbed.replace(literal, " ")
    problems += [
        f"opaque value: {m.group(0)[:12]}..."
        for m in OPAQUE_RE.finditer(scrubbed)
        if not m.group(0).startswith("SYNTHETIC")
    ]
    return problems


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: str(p.relative_to(FIXTURES_DIR)))
def test_fixture_is_clean(path: Path) -> None:
    """Every committed fixture holds only synthetic data."""
    problems = find_problems(path.read_text(encoding="utf-8", errors="replace"))
    assert not problems, f"{path.name}: {problems}"


def test_scanner_flags_bad_content() -> None:
    """The scanner itself rejects e-mails, opaque runs and JWTs, and allows safe data."""
    assert find_problems("someone" + "@" + "gmail.com")
    assert find_problems("A" * 30)
    assert find_problems("eyJhbGciOiJIUzI1NiJ9.payload")
    assert not find_problems("user@example.com SYNTHETIC_PARAMETERS_VALUE_0000001 plain text")


def test_scanner_safe_literals_and_their_abuse() -> None:
    """Safe literals pass; the same literals followed by real-looking values still flag."""
    assert not find_problems('name="__RequestVerificationToken"')
    for host, path in sorted(_ENDPOINTS):
        assert not find_problems(f'action="https://{host}{path}"')
    assert find_problems(
        '__RequestVerificationToken" value="' + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ2bD4" + '"'
    )
    url = f"https://{sorted(_ENDPOINTS)[0][0]}{sorted(_ENDPOINTS)[0][1]}"
    assert find_problems(url + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ2bD4")
    assert find_problems("0123456789abcdef0123456789abcdef")
    assert find_problems("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo+/a==")
    assert find_problems("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig")

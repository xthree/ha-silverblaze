"""Minimal HTML form parser (stdlib only)."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TypedDict


class Form(TypedDict):
    """One parsed ``<form>``."""

    action: str
    method: str
    fields: dict[str, str]


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self._cur: Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self._cur = {
                "action": a.get("action") or "",
                "method": (a.get("method") or "get").lower(),
                "fields": {},
            }
            self.forms.append(self._cur)
        elif self._cur is not None and tag == "input" and a.get("name"):
            if (a.get("type") or "").lower() in ("checkbox", "radio") and "checked" not in a:
                return
            self._cur["fields"][a["name"]] = a.get("value") or ""
        elif self._cur is not None and tag == "button" and a.get("name"):
            self._cur["fields"].setdefault(a["name"], a.get("value") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._cur = None


def forms_of(html: str) -> list[Form]:
    """Return every form in ``html`` with its action, method and submittable fields."""
    parser = _FormParser()
    parser.feed(html)
    parser.close()
    return parser.forms

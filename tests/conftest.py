"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aioresponses import aioresponses
from aioresponses import core as aioresponses_core

import custom_components

from .fake_portal import FakePortal

# The editable install exposes ``custom_components`` as a namespace package whose
# ``__path__`` holds an import-hook entry instead of a real directory. Home
# Assistant's loader lists that path and fails, and the test plugin's own
# ``testing_config/custom_components`` would otherwise shadow this repository.
# Pin the package to the real directory before any test runs.
custom_components.__path__ = [str(Path(__file__).resolve().parent.parent / "custom_components")]

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Allow Home Assistant to load the custom integration under test.

    Tests that request ``recorder_mock`` need it set up before ``hass`` exists, so it
    is resolved first, then ``enable_custom_integrations``.
    """
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_mock")
    request.getfixturevalue("enable_custom_integrations")


def load_fixture_text(relpath: str) -> str:
    """Return the text of a file under tests/fixtures/."""
    return (FIXTURES_DIR / relpath).read_text(encoding="utf-8")


def load_fixture_json(relpath: str) -> Any:
    """Return the parsed JSON of a file under tests/fixtures/."""
    return json.loads(load_fixture_text(relpath))


class _CompatClientResponse(aiohttp.ClientResponse):
    """Response class that supplies the ``stream_writer`` argument newer aiohttp requires."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
        super().__init__(*args, **kwargs)


@pytest.fixture
def mock_aioresponse(monkeypatch):
    """Yield an ``aioresponses`` mock that works with the installed aiohttp.

    aioresponses builds fake responses without the ``stream_writer`` argument that
    aiohttp 3.13+ made mandatory, so constructing one raises ``TypeError``. This
    fixture swaps in a response subclass that fills in a default for it.
    """
    monkeypatch.setattr(aioresponses_core, "ClientResponse", _CompatClientResponse)
    with aioresponses() as m:
        yield m


@pytest.fixture
def mock_config_entry():
    """A synthetic Silverblaze config entry (not yet added to hass)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain="silverblaze",
        data={
            "utility": "idahopower",
            "username": "user@example.com",
            "password": "SYNTHETIC_PASSWORD",
        },
        options={},
    )


@pytest.fixture
def fake_portal():
    """A fake portal patched in at the API boundary."""
    portal = FakePortal()
    with portal.patched():
        yield portal

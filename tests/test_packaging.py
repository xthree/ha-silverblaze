"""Packaging metadata checks."""

from __future__ import annotations

import json
import tomllib
from importlib.metadata import version as dist_version
from pathlib import Path

from custom_components.silverblaze import const

ROOT = Path(__file__).parent.parent
MANIFEST = json.loads((ROOT / "custom_components/silverblaze/manifest.json").read_text())
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
HACS = json.loads((ROOT / "hacs.json").read_text())


def test_manifest_values() -> None:
    assert MANIFEST["domain"] == const.DOMAIN == "silverblaze"
    assert MANIFEST["name"] == "Silverblaze (Idaho Power)"
    assert MANIFEST["codeowners"] == ["@xthree"]
    assert MANIFEST["config_flow"] is True
    assert MANIFEST["dependencies"] == ["recorder"]
    assert MANIFEST["integration_type"] == "service"
    assert MANIFEST["iot_class"] == "cloud_polling"
    assert MANIFEST["version"] == "0.1.1"
    assert MANIFEST["documentation"] == "https://github.com/xthree/ha-silverblaze"
    assert MANIFEST["issue_tracker"] == "https://github.com/xthree/ha-silverblaze/issues"


def test_manifest_has_no_runtime_requirements() -> None:
    assert MANIFEST["requirements"] == []


def test_hacs_json() -> None:
    assert HACS == {
        "name": "Silverblaze (Idaho Power)",
        "homeassistant": "2026.10.0",
        "render_readme": True,
        "country": "US",
    }


def _ver(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def test_manifest_version_matches_pyproject() -> None:
    assert MANIFEST["version"] == PYPROJECT["project"]["version"]


def test_probe_library_version_matches_manifest() -> None:
    from custom_components.silverblaze.api.probe import library_version

    assert library_version() == MANIFEST["version"]


def test_hacs_name_matches_manifest() -> None:
    assert HACS["name"] == MANIFEST["name"]


def test_hacs_min_ha_not_above_pinned_test_ha() -> None:
    assert _ver(HACS["homeassistant"]) <= _ver(dist_version("homeassistant"))


def test_const_bounds() -> None:
    assert (
        const.MIN_UPDATE_INTERVAL_HOURS
        <= const.DEFAULT_UPDATE_INTERVAL_HOURS
        <= const.MAX_UPDATE_INTERVAL_HOURS
    )
    assert (const.MIN_UPDATE_INTERVAL_HOURS, const.DEFAULT_UPDATE_INTERVAL_HOURS) == (6, 12)
    assert const.MAX_UPDATE_INTERVAL_HOURS == 24
    assert const.CORRECTION_WINDOW_DAYS == 30

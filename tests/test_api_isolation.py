"""The api package must import without Home Assistant."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_api_imports_without_homeassistant():
    code = (
        "import sys; sys.modules['homeassistant'] = None; "
        "import custom_components.silverblaze.api as api; "
        "import custom_components.silverblaze.api.utilities; "
        "assert api.SilverblazeHttp and api.Allowlist"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_api_sources_have_no_forbidden_imports():
    api_dir = ROOT / "custom_components" / "silverblaze" / "api"
    for path in api_dir.rglob("*.py"):
        depth = len(path.relative_to(api_dir).parts) - 1
        for line in path.read_text().splitlines():
            stripped = line.strip()
            assert not stripped.startswith(("import homeassistant", "from homeassistant")), path
            assert not stripped.startswith("from custom_components"), path
            if stripped.startswith("from ."):
                dots = len(stripped[5:]) - len(stripped[5:].lstrip("."))
                assert dots <= depth + 1, f"{path}: relative import leaves the api package"

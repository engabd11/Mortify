"""Basic smoke tests for Mortify — validates imports and manifest structure."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path


# ---------------------------------------------------------------------------
# Ensure custom_components is importable.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Stub the homeassistant package just enough that our modules import.
# ---------------------------------------------------------------------------

def _install_ha_stubs() -> None:
    """Provide minimal stubs for the bits of homeassistant we touch."""
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    ha.__path__ = []
    sys.modules["homeassistant"] = ha

    ha_util = types.ModuleType("homeassistant.util")
    ha_util.__path__ = []
    sys.modules["homeassistant.util"] = ha_util

    ha_const = types.ModuleType("homeassistant.const")
    sys.modules["homeassistant.const"] = ha_const
    ha_const.CONF_NAME = "name"
    ha_const.MAJOR_VERSION = 2024
    ha_const.MINOR_VERSION = 1
    ha_const.PLATFORM_FORMAT = "{}.{}"
    ha_const.__version__ = "2024.1.0"

    ha_loader = types.ModuleType("homeassistant.loader")
    sys.modules["homeassistant.loader"] = ha_loader
    ha_loader.async_get_integration = lambda *a, **k: None

    ha_helpers = types.ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers"] = ha_helpers

    ha_core = types.ModuleType("homeassistant.core")
    sys.modules["homeassistant.core"] = ha_core
    ha_core.callback = lambda f: f
    ha_core.split_entity_id = lambda eid: eid.split(".", 1)

    class _StubHomeAssistant:
        config = types.SimpleNamespace(components=set())
        states = types.SimpleNamespace(async_all=lambda _domain: [])

    ha_core.HomeAssistant = _StubHomeAssistant

    ha_components = types.ModuleType("homeassistant.components")
    sys.modules["homeassistant.components"] = ha_components

    ha_http = types.ModuleType("homeassistant.components.http")
    sys.modules["homeassistant.components.http"] = ha_http
    ha_http.HomeAssistantView = type("HomeAssistantView", (), {})
    # Some HA versions have StaticPathConfig, others don't — stub it.
    if not hasattr(ha_http, "StaticPathConfig"):
        ha_http.StaticPathConfig = type("StaticPathConfig", (), {})

    ha_frontend = types.ModuleType("homeassistant.components.frontend")
    sys.modules["homeassistant.components.frontend"] = ha_frontend

    ha_exceptions = types.ModuleType("homeassistant.exceptions")
    sys.modules["homeassistant.exceptions"] = ha_exceptions
    ha_exceptions.HomeAssistantError = Exception
    ha_exceptions.ServiceValidationError = Exception

    ha_config_entries = types.ModuleType("homeassistant.config_entries")
    sys.modules["homeassistant.config_entries"] = ha_config_entries
    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})


_install_ha_stubs()

# In CI, the real homeassistant is already imported and may not have
# StaticPathConfig (added in a later HA release). Patch it unconditionally.
import homeassistant.components.http as _ha_http
if not hasattr(_ha_http, "StaticPathConfig"):
    _ha_http.StaticPathConfig = type("StaticPathConfig", (), {})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_manifest_valid():
    """Manifest must be valid JSON with required keys."""
    manifest_path = REPO_ROOT / "custom_components" / "mortify" / "manifest.json"
    with open(manifest_path) as f:
        data = json.load(f)
    assert data["domain"] == "mortify"
    assert data["name"] == "Mortify"
    assert "version" in data
    assert "config_flow" in data


def test_const_imports():
    """const.py must import without errors."""
    from custom_components.mortify import const  # noqa: F401


def test_stock_mysteries_exist():
    """Stock mystery files must be present and valid JSON."""
    mysteries_dir = REPO_ROOT / "custom_components" / "mortify" / "mysteries"
    files = list(mysteries_dir.glob("*.json"))
    assert len(files) >= 2, "Expected at least 2 stock mysteries"

    for fpath in files:
        with open(fpath) as f:
            data = json.load(f)
        assert "title" in data, f"{fpath.name}: missing 'title'"
        assert "suspects" in data, f"{fpath.name}: missing 'suspects'"
        assert "clues" in data, f"{fpath.name}: missing 'clues'"
        assert "killer_id" in data, f"{fpath.name}: missing 'killer_id'"


def test_config_flow_imports():
    """config_flow must import without errors."""
    from custom_components.mortify import config_flow  # noqa: F401

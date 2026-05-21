"""The Mortify integration.

Sets up:
* The Mortify manager (sessions, music, agent/speaker/entity discovery,
  HMAC player tokens).
* Authenticated WebSocket commands for the admin.
* An unauthenticated WebSocket endpoint + public HTTP routes for guest
  players.
* A *custom* sidebar panel that loads the bundled admin UI inside HA's
  authenticated frame (no iframe, no 401, no double-login).
"""
from __future__ import annotations

import logging

from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    PANEL_COMPONENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    STATIC_URL,
)
from .http_views import async_register_views
from .manager import MortifyManager
from .websocket_api import async_register_commands

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Mortify (config-flow only)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mortify from a config entry."""
    manager = MortifyManager(hass)
    await manager.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    # Register everything once across config entries (we only support one,
    # but guard regardless — Quizify does the same).
    if not hass.data[DOMAIN].get("_registered"):
        async_register_commands(hass)
        await async_register_views(hass)

        # Register a CUSTOM sidebar panel. The HA frontend dynamically
        # imports the JS module while authenticated, instantiates the
        # ``mortify-panel`` custom element, and hands it the ``hass``
        # object. No iframe -> no separate auth handshake -> no 401.
        # This is the headline fix versus the original Mortify, which
        # used an iframe and couldn't reliably talk to HA's auth.
        frontend.async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=PANEL_URL,
            config={
                "_panel_custom": {
                    "name": PANEL_COMPONENT,
                    "embed_iframe": False,
                    "trust_external": False,
                    "module_url": f"{STATIC_URL}/mortify.js",
                },
            },
            require_admin=False,
        )
        hass.data[DOMAIN]["_registered"] = True

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry, tearing down sessions and the panel."""
    manager: MortifyManager | None = (
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    )
    if manager:
        for session in list(manager.list_sessions()):
            await manager.end_session(session.session_id)

    remaining = [
        k for k in hass.data.get(DOMAIN, {}).keys()
        if not k.startswith("_")
    ]
    if not remaining:
        try:
            frontend.async_remove_panel(hass, PANEL_URL)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Panel removal skipped", exc_info=True)
        hass.data[DOMAIN].pop("_registered", None)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

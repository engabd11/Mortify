"""Mortify - AI Murder Mystery Game for Home Assistant."""
from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.frontend import async_register_built_in_panel, async_remove_panel

from .const import DOMAIN
from .game_manager import MortifyGameManager
from .websocket_api import async_register_websocket_commands
from .views import (
    MortifyAdminView,
    MortifyPlayerView,
    MortifyStaticView,
    MortifyAPIView,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Mortify integration (called before config entries)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mortify from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    game_manager = MortifyGameManager(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = game_manager

    # Register HTTP views
    hass.http.register_view(MortifyAdminView(hass, game_manager))
    hass.http.register_view(MortifyPlayerView(hass, game_manager))
    hass.http.register_view(MortifyStaticView(hass))
    hass.http.register_view(MortifyAPIView(hass, game_manager))

    # Register WebSocket API
    async_register_websocket_commands(hass, game_manager)

    # Add sidebar panel
    async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="Mortify",
        sidebar_icon="mdi:knife",
        frontend_url_path="mortify",
        config={"url": "/mortify/admin"},
        require_admin=False,
    )

    _LOGGER.info("Mortify integration loaded successfully")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Mortify config entry."""
    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        game_manager = hass.data[DOMAIN].pop(entry.entry_id)
        await game_manager.async_shutdown()

    try:
        async_remove_panel(hass, "mortify")
    except Exception:
        pass

    return True

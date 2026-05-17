"""WebSocket API for Mortify."""
from __future__ import annotations

import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.components import websocket_api

from .const import (
    DOMAIN,
    WS_TYPE_JOIN,
    WS_TYPE_INTERROGATE,
    WS_TYPE_SUBMIT_CLUE,
    WS_TYPE_ACCUSE,
    WS_TYPE_GET_STATE,
    WS_TYPE_ADMIN_START,
    WS_TYPE_ADMIN_NEXT_ACT,
    WS_TYPE_ADMIN_CANCEL,
    WS_TYPE_ADMIN_GET_ENTITIES,
    WS_TYPE_ADMIN_GET_SPEAKERS,
)
from .game_manager import MortifyGameManager

_LOGGER = logging.getLogger(__name__)


def _get_manager(hass: HomeAssistant) -> MortifyGameManager | None:
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data:
        return None
    return next(iter(domain_data.values()), None)


def async_register_websocket_commands(hass: HomeAssistant, manager: MortifyGameManager) -> None:
    """Register all WebSocket commands."""

    @websocket_api.websocket_command({vol.Required("type"): WS_TYPE_ADMIN_GET_SPEAKERS})
    @websocket_api.async_response
    async def ws_get_speakers(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        speakers = await mgr.async_get_available_speakers()
        connection.send_result(msg["id"], {"speakers": speakers})

    @websocket_api.websocket_command({vol.Required("type"): WS_TYPE_ADMIN_GET_ENTITIES})
    @websocket_api.async_response
    async def ws_get_entities(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        entities = await mgr.async_get_available_entities()
        connection.send_result(msg["id"], {"entities": entities})

    @websocket_api.websocket_command({
        vol.Required("type"): WS_TYPE_ADMIN_START,
        vol.Required("speaker_entity_id"): str,
        vol.Required("entity_ids"): [str],
        vol.Optional("player_names"): [str],
    })
    @websocket_api.async_response
    async def ws_admin_start(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        result = await mgr.async_start_game(
            msg["speaker_entity_id"],
            msg["entity_ids"],
            msg.get("player_names"),
        )
        connection.send_result(msg["id"], result)

    @websocket_api.websocket_command({vol.Required("type"): WS_TYPE_ADMIN_NEXT_ACT})
    @websocket_api.async_response
    async def ws_next_act(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        result = await mgr.async_next_act()
        connection.send_result(msg["id"], result)

    @websocket_api.websocket_command({vol.Required("type"): WS_TYPE_ADMIN_CANCEL})
    @websocket_api.async_response
    async def ws_cancel(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        await mgr.async_reset_game()
        connection.send_result(msg["id"], {"success": True})

    @websocket_api.websocket_command({
        vol.Required("type"): WS_TYPE_JOIN,
        vol.Required("name"): str,
    })
    @websocket_api.async_response
    async def ws_join(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        result = await mgr.async_player_join(msg["name"], str(id(connection)))
        connection.send_result(msg["id"], result)

    @websocket_api.websocket_command({
        vol.Required("type"): WS_TYPE_INTERROGATE,
        vol.Required("player_id"): str,
        vol.Required("suspect_role_id"): str,
        vol.Required("question"): str,
    })
    @websocket_api.async_response
    async def ws_interrogate(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        result = await mgr.async_interrogate_suspect(
            msg["player_id"], msg["suspect_role_id"], msg["question"]
        )
        connection.send_result(msg["id"], result)

    @websocket_api.websocket_command({
        vol.Required("type"): WS_TYPE_SUBMIT_CLUE,
        vol.Required("player_id"): str,
        vol.Required("entity_id"): str,
    })
    @websocket_api.async_response
    async def ws_clue(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        result = await mgr.async_discover_clue(msg["player_id"], msg["entity_id"])
        connection.send_result(msg["id"], result)

    @websocket_api.websocket_command({
        vol.Required("type"): WS_TYPE_ACCUSE,
        vol.Required("player_id"): str,
        vol.Required("accused_role_id"): str,
    })
    @websocket_api.async_response
    async def ws_accuse(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        result = await mgr.async_submit_accusation(msg["player_id"], msg["accused_role_id"])
        connection.send_result(msg["id"], result)

    @websocket_api.websocket_command({vol.Required("type"): WS_TYPE_GET_STATE})
    @websocket_api.async_response
    async def ws_get_state(hass, connection, msg):
        mgr = _get_manager(hass)
        if not mgr:
            connection.send_error(msg["id"], "not_found", "Mortify not configured")
            return
        connection.send_result(msg["id"], mgr._build_full_state())

    # Register all
    for fn in [
        ws_get_speakers, ws_get_entities, ws_admin_start, ws_next_act, ws_cancel,
        ws_join, ws_interrogate, ws_clue, ws_accuse, ws_get_state,
    ]:
        websocket_api.async_register_command(hass, fn)

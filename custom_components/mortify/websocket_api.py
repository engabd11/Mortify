"""Admin WebSocket API for Mortify.

Admin commands ride Home Assistant's built-in ``websocket_api``, so they
inherit HA's auth, replay protection, and rate-limiting. Guest players
use the separate unauthenticated socket from ``http_views``.

Commands:
    mortify/agents/list           -> {agents: [...]}
    mortify/speakers/list         -> {speakers: [...]}
    mortify/tts/list              -> {tts_entities: [...]}
    mortify/entities/list         -> {entities: [...]}
    mortify/game/create           -> {session_id, join_code, game}
    mortify/game/start            -> {ok: true}
    mortify/game/next_act         -> {ok, state, act}
    mortify/game/reveal           -> {ok: true}
    mortify/game/end              -> {ok: true}
    mortify/game/rematch          -> {session_id, join_code, game}
    mortify/admin/subscribe       -> stream of game events
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import (
    DIFFICULTIES,
    TTS_SPEEDS,
    WS_TYPE_ADMIN_SUBSCRIBE,
    WS_TYPE_GAME_CREATE,
    WS_TYPE_GAME_END,
    WS_TYPE_GAME_NEXT_ACT,
    WS_TYPE_GAME_REMATCH,
    WS_TYPE_GAME_REVEAL,
    WS_TYPE_GAME_START,
    WS_TYPE_LIST_AGENTS,
    WS_TYPE_LIST_ENTITIES,
    WS_TYPE_LIST_LIGHTS,
    WS_TYPE_LIST_SPEAKERS,
    WS_TYPE_LIST_TTS,
)
from .game import GameSettings, InvalidStateError
from .manager import MortifyManager, TooManySessionsError, get_manager

_LOGGER = logging.getLogger(__name__)


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register all admin websocket commands."""
    websocket_api.async_register_command(hass, ws_list_agents)
    websocket_api.async_register_command(hass, ws_list_speakers)
    websocket_api.async_register_command(hass, ws_list_tts)
    websocket_api.async_register_command(hass, ws_list_entities)
    websocket_api.async_register_command(hass, ws_list_lights)
    websocket_api.async_register_command(hass, ws_game_create)
    websocket_api.async_register_command(hass, ws_game_start)
    websocket_api.async_register_command(hass, ws_game_next_act)
    websocket_api.async_register_command(hass, ws_game_reveal)
    websocket_api.async_register_command(hass, ws_game_end)
    websocket_api.async_register_command(hass, ws_game_rematch)
    websocket_api.async_register_command(hass, ws_admin_subscribe)


def _manager(hass: HomeAssistant) -> MortifyManager | None:
    return get_manager(hass)


# --- read-only commands -----------------------------------------------------

@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST_AGENTS})
@callback
def ws_list_agents(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    connection.send_result(msg["id"], {"agents": mgr.list_agents()})


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST_SPEAKERS})
@callback
def ws_list_speakers(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    connection.send_result(msg["id"], {"speakers": mgr.list_speakers()})


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST_TTS})
@callback
def ws_list_tts(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    connection.send_result(msg["id"], {"tts_entities": mgr.list_tts()})


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST_ENTITIES})
@callback
def ws_list_entities(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    connection.send_result(msg["id"], {"entities": mgr.list_entities()})


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST_LIGHTS})
@callback
def ws_list_lights(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    connection.send_result(msg["id"], {"lights": mgr.list_lights()})


# --- session management -----------------------------------------------------

@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_GAME_CREATE,
    vol.Required("agent_entity_id"): str,
    vol.Required("entity_ids"): [str],
    vol.Optional("music_player"): vol.Any(str, None),
    vol.Optional("tts_entity"): vol.Any(str, None),
    vol.Optional("difficulty", default="medium"): vol.In(DIFFICULTIES),
    vol.Optional("suspect_count", default=4): vol.All(
        int, vol.Range(min=3, max=8),
    ),
    vol.Optional("tts_speed", default="normal"): vol.In(list(TTS_SPEEDS.keys())),
    vol.Optional("light_entity_ids", default=list): [str],
    vol.Optional("lights_enabled", default=True): bool,
})
@websocket_api.async_response
async def ws_game_create(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Create a new session. Doesn't start it yet."""
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    settings = GameSettings(
        agent_entity_id=msg["agent_entity_id"],
        music_player=msg.get("music_player") or None,
        tts_entity=msg.get("tts_entity") or None,
        entity_ids=list(msg["entity_ids"]),
        difficulty=msg["difficulty"],
        suspect_count=msg["suspect_count"],
        tts_speed=msg.get("tts_speed", "normal"),
        light_entity_ids=list(msg.get("light_entity_ids") or []),
        lights_enabled=bool(msg.get("lights_enabled", True)),
    )
    try:
        session = mgr.create_session(settings)
    except TooManySessionsError as err:
        connection.send_error(msg["id"], "too_many_sessions", str(err))
        return
    mgr.wire_session_music(session)
    mgr.wire_session_lights(session)
    connection.send_result(msg["id"], {
        "session_id": session.session_id,
        "join_code": session.join_code,
        "game": session.to_dict(include_secrets=True),
    })


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_GAME_START,
    vol.Required("session_id"): str,
})
@websocket_api.async_response
async def ws_game_start(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    session = mgr.get_session(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return
    try:
        await session.start()
    except InvalidStateError as err:
        connection.send_error(msg["id"], "invalid_state", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_GAME_NEXT_ACT,
    vol.Required("session_id"): str,
})
@websocket_api.async_response
async def ws_game_next_act(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    session = mgr.get_session(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return
    try:
        result = await session.next_act()
    except InvalidStateError as err:
        connection.send_error(msg["id"], "invalid_state", str(err))
        return
    connection.send_result(msg["id"], {"ok": True, **result})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_GAME_REVEAL,
    vol.Required("session_id"): str,
})
@websocket_api.async_response
async def ws_game_reveal(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Force the reveal phase regardless of accusations submitted."""
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    session = mgr.get_session(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return
    await session.reveal_killer()
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_GAME_END,
    vol.Required("session_id"): str,
})
@websocket_api.async_response
async def ws_game_end(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    await mgr.end_session(msg["session_id"])
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_GAME_REMATCH,
    vol.Required("session_id"): str,
})
@websocket_api.async_response
async def ws_game_rematch(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Start a new game with the same settings.

    The old session is ended after the new one is allocated, so a failed
    rematch never kills the session the admin was looking at.
    """
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    old = mgr.get_session(msg["session_id"])
    if old is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return
    try:
        new_session = mgr.create_session(old.settings)
    except TooManySessionsError as err:
        connection.send_error(msg["id"], "too_many_sessions", str(err))
        return
    mgr.wire_session_music(new_session)
    mgr.wire_session_lights(new_session)
    await mgr.end_session(msg["session_id"])
    connection.send_result(msg["id"], {
        "session_id": new_session.session_id,
        "join_code": new_session.join_code,
        "game": new_session.to_dict(include_secrets=True),
    })


# --- subscription ------------------------------------------------------------

@websocket_api.websocket_command({
    vol.Required("type"): WS_TYPE_ADMIN_SUBSCRIBE,
    vol.Required("session_id"): str,
})
@websocket_api.async_response
async def ws_admin_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe an admin connection to a session's events."""
    mgr = _manager(hass)
    if mgr is None:
        connection.send_error(msg["id"], "not_ready", "Mortify is not initialised")
        return
    session = mgr.get_session(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return

    async def forward(event: dict[str, Any]) -> None:
        connection.send_event(msg["id"], {
            **event,
            "game": session.to_dict(include_secrets=True),
        })

    unsubscribe = session.subscribe(forward)

    @callback
    def cancel() -> None:
        unsubscribe()

    connection.subscriptions[msg["id"]] = cancel
    connection.send_result(msg["id"])
    # Send an immediate snapshot so the admin doesn't have to wait for
    # the next event to populate its UI.
    connection.send_event(msg["id"], {
        "event": "snapshot",
        "game": session.to_dict(include_secrets=True),
    })

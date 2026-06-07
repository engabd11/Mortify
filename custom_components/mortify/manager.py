"""Mortify manager: sessions registry, player tokens, music control,
speaker/entity discovery.

This module mirrors Quizify's manager closely. The differences come from
Mortify's domain: instead of trivia categories we expose conversation
agents and clue-entity discovery; instead of streamed music we pulse
the speaker on act transitions and TTS bursts.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from .const import (
    ACT_LIGHT_MOODS,
    DOMAIN,
    LIGHT_FLASH_RGB,
    LIGHT_TRANSITION_S,
    MAX_CONCURRENT_SESSIONS,
    MAX_TOKEN_LENGTH,
    PLAYER_TOKEN_TTL,
)
from .game import GameSession, GameSettings
from .lights import MortifyLights
from .llm_client import list_conversation_agents

_LOGGER = logging.getLogger(__name__)


class TooManySessionsError(Exception):
    """Raised when create_session is called past MAX_CONCURRENT_SESSIONS."""


# Domains that make sensible murder-mystery clue entities.
_CLUE_ENTITY_DOMAINS = frozenset({
    "binary_sensor", "sensor", "light", "switch",
    "lock", "cover", "camera", "input_boolean", "input_select",
})


class MortifyManager:
    """Singleton-per-config-entry manager for Mortify."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._sessions: dict[str, GameSession] = {}
        self._join_index: dict[str, str] = {}  # join_code -> session_id
        self._lock = asyncio.Lock()
        # HMAC secret for issuing/verifying player tokens. Generated per
        # process start — tokens don't need to survive a restart because
        # in-memory game state doesn't either.
        self._token_secret = secrets.token_bytes(32)
        # Party lights service — stateful across a single game.
        self._lights = MortifyLights(hass)
        # Callbacks for session create/end (e.g. an HA sensor might want
        # to track active games).
        self._session_listeners: list[Callable[[str, GameSession], None]] = []

    async def async_setup(self) -> None:
        """Async setup hook (kept for symmetry with Quizify)."""
        return

    # --- session lifecycle ------------------------------------------------

    def create_session(self, settings: GameSettings) -> GameSession:
        """Create a new session with a unique join code.

        Raises:
            TooManySessionsError: too many active games, or couldn't find
                a unique join code (vanishingly unlikely).
        """
        if len(self._sessions) >= MAX_CONCURRENT_SESSIONS:
            raise TooManySessionsError(
                f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent games reached"
            )
        session_id = secrets.token_urlsafe(12)
        session: GameSession | None = None
        # Try a few times to dodge collisions with in-flight join codes.
        for _ in range(16):
            candidate = GameSession(session_id, settings, hass=self.hass)
            if candidate.join_code not in self._join_index:
                session = candidate
                break
        if session is None:
            _LOGGER.error("Could not find a unique join code in 16 attempts")
            raise TooManySessionsError("Could not allocate a unique join code")
        # Snapshot the chosen entities once at creation, so we don't have
        # to re-walk the state machine later.
        session.entities = self.resolve_entities(settings.entity_ids)
        self._sessions[session_id] = session
        self._join_index[session.join_code] = session_id
        _LOGGER.info(
            "Mortify session %s created (join code %s)",
            session_id, session.join_code,
        )
        self._notify_session_listeners("created", session)
        return session

    def get_session(self, session_id: str) -> GameSession | None:
        return self._sessions.get(session_id)

    def get_by_join_code(self, join_code: str) -> GameSession | None:
        sid = self._join_index.get(join_code.upper())
        if sid is None:
            return None
        return self._sessions.get(sid)

    def list_sessions(self) -> list[GameSession]:
        return list(self._sessions.values())

    async def end_session(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return
            self._join_index.pop(session.join_code, None)
        await session.cancel()
        await self.stop_music(session)
        await self.restore_lights(session)
        self._notify_session_listeners("ended", session)

    # --- listeners --------------------------------------------------------

    def subscribe_sessions(
        self, callback: Callable[[str, GameSession], None],
    ) -> Callable[[], None]:
        self._session_listeners.append(callback)

        def unsubscribe() -> None:
            try:
                self._session_listeners.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def _notify_session_listeners(
        self, action: str, session: GameSession,
    ) -> None:
        for cb in list(self._session_listeners):
            try:
                cb(action, session)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Session listener raised; continuing")

    # --- HMAC-signed player tokens ----------------------------------------

    def issue_player_token(self, session_id: str, player_id: str) -> str:
        """Issue an HMAC-signed token binding a player_id to a session.

        Format: ``<expiry>.<session_id>.<player_id>.<hex_signature>``.
        Tokens are opaque to the client — only the server checks them.
        """
        expiry = int(time.time()) + PLAYER_TOKEN_TTL
        body = f"{expiry}.{session_id}.{player_id}"
        sig = hmac.new(
            self._token_secret, body.encode("utf-8"), sha256,
        ).hexdigest()
        return f"{body}.{sig}"

    def verify_player_token(self, token: str, session_id: str) -> str | None:
        """Verify a token; return player_id if valid, else None."""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        try:
            expiry_str, sid, player_id, sig = token.split(".", 3)
        except ValueError:
            return None
        if sid != session_id:
            return None
        try:
            expiry = int(expiry_str)
        except ValueError:
            return None
        if expiry < time.time():
            return None
        body = f"{expiry_str}.{sid}.{player_id}"
        expected = hmac.new(
            self._token_secret, body.encode("utf-8"), sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        return player_id

    # --- discovery: agents / speakers / TTS / entities -------------------

    def list_agents(self) -> list[dict[str, Any]]:
        return list_conversation_agents(self.hass)

    def list_speakers(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        registry = er.async_get(self.hass)
        for state in self.hass.states.async_all("media_player"):
            entry = registry.async_get(state.entity_id)
            platform = entry.platform if entry else "unknown"
            out.append({
                "entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name", state.entity_id),
                "platform": platform,
                "state": state.state,
            })
        out.sort(key=lambda p: p["name"].lower())
        return out

    def list_lights(self) -> list[dict[str, Any]]:
        """Return light entities (including groups) for the lighting picker."""
        area_reg = ar.async_get(self.hass)
        entity_reg = er.async_get(self.hass)
        out: list[dict[str, Any]] = []
        for state in self.hass.states.async_all("light"):
            entity_id = state.entity_id
            entry = entity_reg.async_get(entity_id)
            area_name = "Unknown Room"
            if entry and entry.area_id:
                area = area_reg.async_get_area(entry.area_id)
                if area:
                    area_name = area.name
            members = state.attributes.get("entity_id")
            is_group = isinstance(members, (list, tuple)) and len(members) > 0
            out.append({
                "entity_id": entity_id,
                "name": state.attributes.get("friendly_name", entity_id),
                "area": area_name,
                "state": state.state,
                "is_group": is_group,
            })
        out.sort(key=lambda e: (e["area"].lower(), e["name"].lower()))
        return out

    def list_tts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for state in self.hass.states.async_all("tts"):
            out.append({
                "entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name", state.entity_id),
            })
        out.sort(key=lambda e: e["name"].lower())
        return out

    def list_entities(self) -> list[dict[str, Any]]:
        """Return entities that make good clue candidates, grouped by area."""
        area_reg = ar.async_get(self.hass)
        entity_reg = er.async_get(self.hass)
        out: list[dict[str, Any]] = []
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if "." not in entity_id:
                continue
            domain = entity_id.split(".", 1)[0]
            if domain not in _CLUE_ENTITY_DOMAINS:
                continue
            if state.state in ("unavailable", "unknown"):
                continue
            entry = entity_reg.async_get(entity_id)
            area_name = "Unknown Room"
            if entry and entry.area_id:
                area = area_reg.async_get_area(entry.area_id)
                if area:
                    area_name = area.name
            out.append({
                "entity_id": entity_id,
                "name": state.attributes.get("friendly_name", entity_id),
                "domain": domain,
                "state": state.state,
                "area": area_name,
                "unit": state.attributes.get("unit_of_measurement", ""),
            })
        out.sort(key=lambda e: (e["area"].lower(), e["name"].lower()))
        return out

    def resolve_entities(self, entity_ids: list[str]) -> list[dict[str, Any]]:
        """Resolve a list of entity_ids to the full dict shape used by game.py."""
        wanted = set(entity_ids or [])
        if not wanted:
            return []
        return [e for e in self.list_entities() if e["entity_id"] in wanted]

    # --- music control ---------------------------------------------------

    async def play_music(self, session: GameSession) -> None:
        """Start background music on the session's media_player."""
        if not session.settings.music_player:
            return
        try:
            await self.hass.services.async_call(
                "media_player",
                "media_play",
                {"entity_id": session.settings.music_player},
                blocking=False,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("media_play failed (nothing queued?)", exc_info=True)

    async def stop_music(self, session: GameSession) -> None:
        if not session.settings.music_player:
            return
        try:
            await self.hass.services.async_call(
                "media_player",
                "media_stop",
                {"entity_id": session.settings.music_player},
                blocking=False,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to stop background music", exc_info=True)

    def wire_session_music(self, session: GameSession) -> None:
        """Hook a session's lifecycle into the music controls."""
        async def on_start() -> None:
            await self.play_music(session)

        async def on_stop() -> None:
            await self.stop_music(session)

        session.set_music_callbacks(on_start, on_stop)

    # --- dramatic lighting -----------------------------------------------

    async def start_lights(self, session: GameSession) -> None:
        """Take control of the lights at game start (save states, apply mood)."""
        if not session.settings.light_entity_ids:
            return
        targets = self._resolve_light_targets(session.settings.light_entity_ids)
        if not targets:
            return
        await self._lights.start(
            entity_ids=targets,
            intensity="medium",
            light_mode="dynamic",
        )
        await self._lights.set_phase(session.state)

    async def drive_lights(self, session: GameSession, mood: str) -> None:
        """Apply a lighting mood (an act state) or a flash."""
        if not session.settings.lights_enabled:
            return
        if mood == "flash":
            await self._lights.flash("red")
            return
        await self._lights.set_phase(mood)

    async def celebrate_lights(self) -> None:
        """Run the rainbow celebration sequence on game end."""
        await self._lights.celebrate()

    async def restore_lights(self, session: GameSession) -> None:
        """Restore saved light states at game end (or on cleanup)."""
        if not session.settings.light_entity_ids:
            return
        await self._lights.stop()

    def wire_session_lights(self, session: GameSession) -> None:
        """Hook the session's lighting callback to this manager."""
        async def on_mood(mood: str) -> None:
            await self.drive_lights(session, mood)
        session.set_lights_callback(on_mood)

    # -- kept for compatibility -------------------------------------------

    def _resolve_light_targets(self, entity_ids: list[str]) -> list[str]:
        """Expand the host's chosen light selection into concrete light.* ids.

        The host may pick individual ``light.*`` entities OR a light *group*
        (also a ``light.*`` entity whose ``entity_id`` attribute lists its
        members). We pass the group entity through as-is (HA fans the service
        call out to members for us), and additionally include any member ids
        we can see, de-duplicated.
        """
        out: list[str] = []
        seen: set[str] = set()
        for eid in entity_ids or []:
            if not isinstance(eid, str) or "." not in eid:
                continue
            domain = eid.split(".", 1)[0]
            if domain != "light":
                _LOGGER.debug("Mortify lighting: skipping non-light %s", eid)
                continue
            if eid not in seen:
                out.append(eid)
                seen.add(eid)
            state = self.hass.states.get(eid)
            if state is not None:
                members = state.attributes.get("entity_id")
                if isinstance(members, (list, tuple)):
                    for m in members:
                        if isinstance(m, str) and m.startswith("light.") and m not in seen:
                            out.append(m)
                            seen.add(m)
        return out


def get_manager(hass: HomeAssistant) -> MortifyManager | None:
    """Return the singleton manager if Mortify is set up."""
    data = hass.data.get(DOMAIN)
    if not data:
        return None
    for v in data.values():
        if isinstance(v, MortifyManager):
            return v
    return None

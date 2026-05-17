"""Mortify Game Manager — central state machine and orchestrator."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er, area_registry as ar

from .const import (
    DOMAIN,
    CONF_LLM_URL,
    CONF_LLM_MODEL,
    CONF_TTS_ENTITY,
    CONF_HA_URL,
    GAME_STATE_LOBBY,
    GAME_STATE_BRIEFING,
    GAME_STATE_INVESTIGATION,
    GAME_STATE_ACCUSATION,
    GAME_STATE_REVEAL,
    GAME_STATE_ENDED,
    MUSIC_LOBBY,
    MUSIC_BRIEFING,
    MUSIC_INVESTIGATION,
    MUSIC_TENSION,
    MUSIC_REVEAL,
    MUSIC_WINNER,
)
from .llm_client import LocalLLMClient
from .story_generator import generate_mystery, generate_npc_response, generate_act_narration

_LOGGER = logging.getLogger(__name__)


class MortifyPlayer:
    """Represents a connected player."""

    def __init__(self, player_id: str, name: str, connection_id: str):
        self.player_id = player_id
        self.name = name
        self.connection_id = connection_id
        self.role: dict | None = None
        self.clues_found: list[str] = []
        self.accusation: str | None = None
        self.score: int = 0
        self.is_correct: bool = False
        self.chat_history: dict[str, list] = {}  # suspect_id -> messages

    def to_dict(self, include_role_secret=False) -> dict:
        d = {
            "player_id": self.player_id,
            "name": self.name,
            "clues_found": len(self.clues_found),
            "accusation": self.accusation,
            "score": self.score,
            "is_correct": self.is_correct,
        }
        if self.role:
            d["role"] = {
                "id": self.role["id"],
                "name": self.role["name"],
                "emoji": self.role["emoji"],
                "description": self.role["description"],
            }
        return d


class MortifyGameManager:
    """Manages the full lifecycle of a Mortify game session."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.config = entry.data

        self.llm = LocalLLMClient(
            self.config.get(CONF_LLM_URL, "http://localhost:11434"),
            self.config.get(CONF_LLM_MODEL, "llama3.2"),
        )

        # Game state
        self.game_id: str = str(uuid.uuid4())[:8]
        self.game_state: str = GAME_STATE_LOBBY
        self.story: dict | None = None
        self.players: dict[str, MortifyPlayer] = {}
        self.current_act: int = 0
        self.selected_speaker: str | None = None
        self.selected_entities: list[dict] = []
        self.tts_entity: str = self.config.get(CONF_TTS_ENTITY, "")
        self.ha_url: str = self.config.get(CONF_HA_URL, "http://homeassistant.local:8123")

        # WebSocket connections
        self._ws_connections: dict[str, Any] = {}
        self._generating = False

        _LOGGER.info("MortifyGameManager initialised")

    # ── Entity & Speaker Discovery ────────────────────────────────────────────

    async def async_get_available_speakers(self) -> list[dict]:
        """Return all media player entities that can play audio."""
        speakers = []
        states = self.hass.states.async_all("media_player")
        for state in states:
            if state.state not in ("unavailable", "unknown"):
                attrs = state.attributes
                speakers.append({
                    "entity_id": state.entity_id,
                    "name": attrs.get("friendly_name", state.entity_id),
                    "state": state.state,
                    "source": attrs.get("source", ""),
                    "platform": state.entity_id.split(".")[1].split("_")[0],
                })
        return sorted(speakers, key=lambda x: x["name"])

    async def async_get_available_entities(self) -> list[dict]:
        """Return interesting entities grouped by area."""
        area_reg = ar.async_get(self.hass)
        entity_reg = er.async_get(self.hass)

        # Domains that make good clues
        interesting_domains = {
            "binary_sensor", "sensor", "light", "switch",
            "lock", "cover", "camera", "input_boolean",
        }

        entities = []
        for state in self.hass.states.async_all():
            domain = state.entity_id.split(".")[0]
            if domain not in interesting_domains:
                continue
            if state.state in ("unavailable", "unknown"):
                continue

            entity_entry = entity_reg.async_get(state.entity_id)
            area_name = "Unknown Room"
            if entity_entry and entity_entry.area_id:
                area = area_reg.async_get_area(entity_entry.area_id)
                if area:
                    area_name = area.name

            attrs = state.attributes
            entities.append({
                "entity_id": state.entity_id,
                "name": attrs.get("friendly_name", state.entity_id),
                "domain": domain,
                "state": state.state,
                "area": area_name,
                "unit": attrs.get("unit_of_measurement", ""),
            })

        return sorted(entities, key=lambda x: (x["area"], x["name"]))

    # ── Game Lifecycle ────────────────────────────────────────────────────────

    async def async_start_game(
        self,
        speaker_entity_id: str,
        entity_ids: list[str],
        player_names: list[str] | None = None,
    ) -> dict:
        """Generate story and move to briefing state."""
        if self._generating:
            return {"success": False, "error": "Already generating a story"}

        self._generating = True
        self.selected_speaker = speaker_entity_id
        self.game_id = str(uuid.uuid4())[:8]

        # Collect selected entity data
        all_entities = await self.async_get_available_entities()
        self.selected_entities = [e for e in all_entities if e["entity_id"] in entity_ids]

        # Extract rooms from entity areas
        rooms = list({e["area"] for e in self.selected_entities if e["area"] != "Unknown Room"})
        if not rooms:
            rooms = ["Living Room", "Kitchen", "Bedroom", "Study", "Hallway"]

        # Get joined player names
        names = player_names or [p.name for p in self.players.values()]
        if not names:
            names = ["The Detective"]

        try:
            self.game_state = GAME_STATE_BRIEFING
            await self._broadcast({"type": "state_change", "state": GAME_STATE_BRIEFING, "message": "Generating mystery..."})

            self.story = await generate_mystery(
                self.llm, rooms, self.selected_entities, names, suspect_count=min(len(names) + 1, 6)
            )

            # Assign roles to players
            for i, player in enumerate(self.players.values()):
                suspects = self.story.get("suspects", [])
                if i < len(suspects):
                    role_id = suspects[i]["role_id"]
                    from .const import SUSPECT_ROLES
                    player.role = next((r for r in SUSPECT_ROLES if r["id"] == role_id), SUSPECT_ROLES[i % len(SUSPECT_ROLES)])

            self.current_act = 1
            self._generating = False

            # Play intro music
            await self._play_music(MUSIC_BRIEFING, volume=0.4)

            # TTS opening narration after short delay
            asyncio.create_task(self._delayed_tts(
                self.story.get("opening_narration", ""), delay=2.0
            ))

            full_state = self._build_full_state()
            await self._broadcast({"type": "game_started", **full_state})

            return {"success": True, "state": full_state}

        except Exception as e:
            _LOGGER.error("Failed to start game: %s", e)
            self._generating = False
            self.game_state = GAME_STATE_LOBBY
            return {"success": False, "error": str(e)}

    async def async_next_act(self) -> dict:
        """Advance to the next act."""
        if self.game_state == GAME_STATE_BRIEFING:
            self.game_state = GAME_STATE_INVESTIGATION
            self.current_act = 1
            await self._play_music(MUSIC_INVESTIGATION, volume=0.35)
            narration = await generate_act_narration(self.llm, self.story, 1, self._get_recent_home_event())
            await self._speak(narration)
            await self._broadcast({"type": "act_started", "act": 1, "narration": narration, "state": self.game_state})

        elif self.game_state == GAME_STATE_INVESTIGATION:
            self.current_act += 1
            if self.current_act >= 4:
                self.game_state = GAME_STATE_ACCUSATION
                self.current_act = 4
                await self._play_music(MUSIC_TENSION, volume=0.5)
                narration = await generate_act_narration(self.llm, self.story, 4)
                await self._speak(narration)
                await self._broadcast({"type": "accusation_phase", "narration": narration, "state": self.game_state})
            else:
                narration = await generate_act_narration(self.llm, self.story, self.current_act, self._get_recent_home_event())
                await self._speak(narration)
                await self._broadcast({"type": "act_started", "act": self.current_act, "narration": narration, "state": self.game_state})

        elif self.game_state == GAME_STATE_ACCUSATION:
            await self._resolve_game()

        return {"success": True, "state": self.game_state, "act": self.current_act}

    async def _resolve_game(self) -> None:
        """Reveal the killer and end the game."""
        self.game_state = GAME_STATE_REVEAL
        killer_id = self.story.get("killer_id", "")

        # Score players
        for player in self.players.values():
            if player.accusation == killer_id:
                player.is_correct = True
                player.score += 100 + (len(player.clues_found) * 10)
            player.score += len(player.clues_found) * 5

        await self._play_music(MUSIC_REVEAL, volume=0.6)
        await asyncio.sleep(1.5)

        reveal = self.story.get("reveal_narration", "The truth is finally revealed.")
        await self._speak(reveal)

        await self._broadcast({
            "type": "game_revealed",
            "killer_id": killer_id,
            "reveal_narration": reveal,
            "motive": self.story.get("motive", ""),
            "scores": {p.player_id: p.to_dict() for p in self.players.values()},
            "state": GAME_STATE_REVEAL,
        })

        # Winner fanfare after reveal
        await asyncio.sleep(4.0)
        await self._play_music(MUSIC_WINNER, volume=0.7)
        self.game_state = GAME_STATE_ENDED

    # ── Player Actions ────────────────────────────────────────────────────────

    async def async_player_join(self, name: str, connection_id: str) -> dict:
        """Add a player to the lobby."""
        player_id = str(uuid.uuid4())[:8]
        player = MortifyPlayer(player_id, name, connection_id)
        self.players[player_id] = player
        await self._broadcast({"type": "player_joined", "player": player.to_dict(), "player_count": len(self.players)})
        _LOGGER.info("Player %s joined as %s", name, player_id)
        return {"success": True, "player_id": player_id, "game_state": self.game_state}

    async def async_interrogate_suspect(
        self, player_id: str, suspect_role_id: str, question: str
    ) -> dict:
        """Player interrogates a suspect NPC via local LLM."""
        player = self.players.get(player_id)
        if not player or self.game_state != GAME_STATE_INVESTIGATION:
            return {"success": False, "error": "Not in investigation phase"}

        # Find suspect data
        suspect = next(
            (s for s in self.story.get("suspects", []) if s.get("role_id") == suspect_role_id),
            None,
        )
        if not suspect:
            return {"success": False, "error": "Suspect not found"}

        # Build chat history for this player/suspect pair
        key = f"{player_id}:{suspect_role_id}"
        if suspect_role_id not in player.chat_history:
            player.chat_history[suspect_role_id] = []

        history = player.chat_history[suspect_role_id]
        response = await generate_npc_response(
            self.llm, suspect, self.story, history, question, suspect.get("is_killer", False)
        )

        # Update history
        player.chat_history[suspect_role_id].append({"role": "user", "content": question})
        player.chat_history[suspect_role_id].append({"role": "assistant", "content": response})

        # Keep history to last 10 turns
        player.chat_history[suspect_role_id] = player.chat_history[suspect_role_id][-10:]

        return {"success": True, "response": response, "suspect_id": suspect_role_id}

    async def async_discover_clue(self, player_id: str, entity_id: str) -> dict:
        """Player discovers a clue linked to a specific entity."""
        player = self.players.get(player_id)
        if not player or self.game_state != GAME_STATE_INVESTIGATION:
            return {"success": False, "error": "Not in investigation phase"}

        if entity_id in player.clues_found:
            return {"success": False, "error": "Already found this clue"}

        # Find a clue associated with this entity
        state = self.hass.states.get(entity_id)
        if not state:
            return {"success": False, "error": "Entity not found"}

        entity_info = next((e for e in self.selected_entities if e["entity_id"] == entity_id), None)
        if not entity_info:
            return {"success": False, "error": "Entity not in game"}

        # Generate a contextual clue from the entity's current state
        suspects = self.story.get("suspects", [])
        clue_pool = [s.get("room_clue", "") for s in suspects if s.get("room_clue")]
        clue_text = random.choice(clue_pool) if clue_pool else f"The {entity_info['name']} shows signs of recent tampering."

        player.clues_found.append(entity_id)

        return {
            "success": True,
            "clue": {
                "entity_id": entity_id,
                "entity_name": entity_info["name"],
                "area": entity_info["area"],
                "clue_text": clue_text,
                "current_state": f"{state.state} {entity_info.get('unit', '')}".strip(),
            },
        }

    async def async_submit_accusation(self, player_id: str, accused_role_id: str) -> dict:
        """Player submits their final accusation."""
        player = self.players.get(player_id)
        if not player or self.game_state != GAME_STATE_ACCUSATION:
            return {"success": False, "error": "Not in accusation phase"}

        player.accusation = accused_role_id
        await self._broadcast({
            "type": "accusation_submitted",
            "player_name": player.name,
            "player_id": player_id,
        })

        # Auto-resolve if all players have accused
        if all(p.accusation for p in self.players.values()):
            await self._resolve_game()

        return {"success": True}

    # ── State ─────────────────────────────────────────────────────────────────

    def _build_full_state(self) -> dict:
        """Build the full shareable game state dict."""
        suspects_public = []
        if self.story:
            for s in self.story.get("suspects", []):
                suspects_public.append({
                    "role_id": s.get("role_id"),
                    "role_name": s.get("role_name"),
                    "role_emoji": s.get("role_emoji"),
                    "player": s.get("player"),
                    "alibi": s.get("alibi"),
                })

        return {
            "game_id": self.game_id,
            "game_state": self.game_state,
            "current_act": self.current_act,
            "title": self.story.get("title", "") if self.story else "",
            "victim_name": self.story.get("victim_name", "") if self.story else "",
            "crime_scene": self.story.get("crime_scene", "") if self.story else "",
            "time_of_death": self.story.get("time_of_death", "") if self.story else "",
            "acts": self.story.get("acts", []) if self.story else [],
            "suspects": suspects_public,
            "players": {pid: p.to_dict() for pid, p in self.players.items()},
            "player_count": len(self.players),
            "ha_url": self.ha_url,
        }

    def get_player_private_state(self, player_id: str) -> dict:
        """Get the private state for a specific player (their role, clues, etc.)."""
        player = self.players.get(player_id)
        if not player:
            return {}

        suspects = self.story.get("suspects", []) if self.story else []
        my_suspect = next((s for s in suspects if s.get("role_id") == (player.role or {}).get("id")), None)

        return {
            "player_id": player_id,
            "role": player.role,
            "clues_found": player.clues_found,
            "secret": my_suspect.get("secret", "") if my_suspect else "",
            "real_clue": my_suspect.get("real_clue", "") if my_suspect else "",
            "accusation": player.accusation,
            "score": player.score,
        }

    # ── Media ─────────────────────────────────────────────────────────────────

    async def _play_music(self, track: str, volume: float = 0.4) -> None:
        """Play background music on the selected speaker."""
        if not self.selected_speaker:
            return
        try:
            await self.hass.services.async_call(
                "media_player",
                "volume_set",
                {"entity_id": self.selected_speaker, "volume_level": volume},
                blocking=False,
            )
            await self.hass.services.async_call(
                "media_player",
                "play_media",
                {
                    "entity_id": self.selected_speaker,
                    "media_content_id": f"/local/{track}",
                    "media_content_type": "audio/mp3",
                },
                blocking=False,
            )
        except Exception as e:
            _LOGGER.warning("Could not play music %s: %s", track, e)

    async def _speak(self, text: str) -> None:
        """Use TTS to narrate text through the selected speaker."""
        if not text:
            return
        speaker = self.tts_entity or self.selected_speaker
        if not speaker:
            return
        try:
            # Duck music first
            if self.selected_speaker and self.selected_speaker != speaker:
                await self.hass.services.async_call(
                    "media_player", "volume_set",
                    {"entity_id": self.selected_speaker, "volume_level": 0.15},
                    blocking=False,
                )

            await self.hass.services.async_call(
                "tts",
                "speak",
                {
                    "entity_id": speaker,
                    "cache": False,
                    "message": text,
                    "language": "en-US",
                },
                blocking=True,
            )

            # Restore music volume
            if self.selected_speaker and self.selected_speaker != speaker:
                await asyncio.sleep(1.0)
                await self.hass.services.async_call(
                    "media_player", "volume_set",
                    {"entity_id": self.selected_speaker, "volume_level": 0.35},
                    blocking=False,
                )
        except Exception as e:
            _LOGGER.warning("TTS failed: %s", e)

    async def _delayed_tts(self, text: str, delay: float = 2.0) -> None:
        await asyncio.sleep(delay)
        await self._speak(text)

    # ── WebSocket Broadcasting ────────────────────────────────────────────────

    def register_connection(self, connection_id: str, send_fn) -> None:
        self._ws_connections[connection_id] = send_fn

    def unregister_connection(self, connection_id: str) -> None:
        self._ws_connections.pop(connection_id, None)

    async def _broadcast(self, message: dict) -> None:
        """Send a message to all connected WebSocket clients."""
        dead = []
        for conn_id, send_fn in self._ws_connections.items():
            try:
                await send_fn(message)
            except Exception:
                dead.append(conn_id)
        for conn_id in dead:
            self._ws_connections.pop(conn_id, None)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_recent_home_event(self) -> str | None:
        """Get a recent interesting home event to weave into narration."""
        interesting = ["binary_sensor", "lock", "cover"]
        for entity_id in [e["entity_id"] for e in self.selected_entities]:
            domain = entity_id.split(".")[0]
            if domain in interesting:
                state = self.hass.states.get(entity_id)
                if state:
                    name = state.attributes.get("friendly_name", entity_id)
                    return f"The {name} just changed to {state.state}"
        return None

    async def async_reset_game(self) -> None:
        """Reset to lobby state."""
        self.game_state = GAME_STATE_LOBBY
        self.story = None
        self.current_act = 0
        self.players.clear()
        self.game_id = str(uuid.uuid4())[:8]
        await self._broadcast({"type": "game_reset", "state": GAME_STATE_LOBBY})

    async def async_shutdown(self) -> None:
        """Clean shutdown."""
        _LOGGER.info("Mortify shutting down")

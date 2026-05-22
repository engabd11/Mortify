"""Game state machine for Mortify.

A ``GameSession`` represents a single murder mystery from lobby through
to reveal. Mirrors Quizify's pattern:

* One async lock per session serialises state transitions and answer
  submissions so a late accusation can't be recorded against a new act.
* Listeners are async callables; the WS layer registers one per
  connected client and the session fans events out to them.
* ``to_dict()`` is the single source of truth for what clients see —
  it strips secret fields by default and only exposes them when the
  caller is allowed to see them (e.g. the admin during reveal).

Concurrency: every mutation goes through ``_lock``. ``submit_accusation``,
``discover_clue`` and the act transitions all acquire it before checking
state, so two clients racing on the last accusation can't accidentally
double-resolve the game.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    ACT_ORDER,
    ACT_TITLES,
    AUTO_ADVANCE_ENABLED,
    AUTO_ADVANCE_MIN_INTERVAL,
    AUTO_ADVANCE_THRESHOLDS,
    EVENT_ACCUSATION_SUBMITTED,
    EVENT_ACT_AUTO_ADVANCED,
    EVENT_ACT_STARTED,
    EVENT_AWAITING_KILLER,
    EVENT_CLUE_DISCOVERED,
    EVENT_CLUE_UNLOCKED,
    EVENT_CONFRONT_RESOLVED,
    EVENT_GAME_ENDED,
    EVENT_GENERATING,
    EVENT_INTERROGATION_REPLY,
    EVENT_KILLER_PROMPT,
    EVENT_NARRATION,
    EVENT_PLAYER_JOINED,
    EVENT_PLAYER_LEFT,
    EVENT_REVEALED,
    KILLER_OPTION_COUNT,
    KILLER_RESPONSE_TIMEOUT,
    MAX_INTERROGATION_LENGTH,
    MAX_PLAYER_NAME_LENGTH,
    MAX_PLAYERS_PER_SESSION,
    MAX_SCORED_QUESTIONS_PER_PLAYER,
    MIN_PLAYERS_FOR_HUMAN_KILLER,
    POINTS_CONFRONT_SUCCESS,
    POINTS_CONFRONT_WASTED,
    POINTS_CORRECT_ACCUSATION,
    POINTS_EVIDENCE_BONUS,
    POINTS_ON_TOPIC_QUESTION,
    POINTS_PER_CLUE_DISCOVERED,
    STATE_ACCUSATION,
    STATE_ACT_1,
    STATE_ENDED,
    STATE_GENERATING,
    STATE_LOBBY,
    STATE_REVEAL,
    TTS_SPEEDS,
)
from .llm_client import LLMClient
from .story_generator import (
    generate_act_narration,
    generate_killer_options,
    generate_mystery,
    generate_npc_reply,
)

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[dict[str, Any]], Awaitable[None]]


class SessionFullError(Exception):
    """Raised when a player tries to join a full session."""


class InvalidStateError(Exception):
    """Raised when an action is attempted in the wrong game state."""


# ---------------------------------------------------------------------------
# Name sanitisation — mirrors Quizify exactly.
# ---------------------------------------------------------------------------

def _sanitize_player_name(name: str) -> str:
    """Strip control chars / zero-width glyphs and cap length.

    Player names appear in the UI, in TTS announcements, and in attributes
    that may surface in HA sensor states. Control chars break TTS, and
    zero-width glyphs let one player spoof another's identical-looking
    name. We allow normal printable text (including non-Latin scripts and
    emoji) and strip the dangerous bits.
    """
    if not name:
        return ""
    out: list[str] = []
    for ch in name:
        # ASCII control + DEL
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            continue
        # Zero-width / invisible joiners and the BOM
        if ch in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"):
            continue
        out.append(ch)
    cleaned = "".join(out).strip()
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned[:MAX_PLAYER_NAME_LENGTH]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Player:
    """A player in a session."""

    player_id: str
    name: str
    # Public state
    score: int = 0
    # Role is assigned at game start. May be None for players who join
    # mid-game (they become detective-only observers).
    role_id: str | None = None
    role_name: str | None = None
    role_emoji: str | None = None
    role_description: str | None = None
    # Engagement state
    clues_found: list[str] = field(default_factory=list)  # clue ids
    accusation: str | None = None   # role_id of accused
    is_correct: bool = False        # after reveal
    # Number of questions the LLM judged genuinely on-topic (used for the
    # questioning bonus, capped by MAX_SCORED_QUESTIONS_PER_PLAYER).
    on_topic_questions: int = 0
    # The one-shot high-stakes confrontation: None until used, then a dict
    # describing the outcome.
    confront_result: dict[str, Any] | None = None
    # Per-suspect chat history (private). Maps role_id -> [{role, content}, ...]
    chat_history: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    # Per-suspect rate-limit timestamps; used in submit_interrogation.
    _question_times: dict[str, list[float]] = field(default_factory=dict)
    joined_at: float = field(default_factory=time.time)
    # True only for the one human player who secretly IS the killer (when the
    # game has enough players to assign the killer to a human). This player
    # answers questions other players put to "their" suspect by choosing from
    # pre-generated evasions. Never exposed in any public projection.
    is_human_killer: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """Public projection — sent to all clients, every event.

        Deliberately omits the player's assigned role/character. Knowing who
        plays whom would let detectives shortcut the mystery (and would tip
        off everyone if the killer is a human). The mapping is only revealed
        at the end of the game.
        """
        d: dict[str, Any] = {
            "player_id": self.player_id,
            "name": self.name,
            "score": self.score,
            "clues_found_count": len(self.clues_found),
            "has_accused": self.accusation is not None,
            "has_confronted": self.confront_result is not None,
            "is_correct": self.is_correct,
            # Whether this player has been assigned a character at all (so the
            # UI can show "playing" vs "observer") — WITHOUT saying which one.
            "has_role": self.role_id is not None,
        }
        return d

    def to_reveal_dict(self) -> dict[str, Any]:
        """Public projection used only at the reveal — includes the role and
        whether this player was the killer, so the final scoreboard can show
        who played whom."""
        d = self.to_public_dict()
        if self.role_id:
            d["role"] = {
                "id": self.role_id,
                "name": self.role_name,
                "emoji": self.role_emoji,
                "description": self.role_description,
            }
        d["was_killer"] = self.is_human_killer
        return d

    def to_private_dict(self) -> dict[str, Any]:
        """Private projection — sent only to this player.

        Includes their own accusation, the list of clue ids they've
        personally unlocked, and their assigned role. NEVER includes
        the killer's identity of OTHER suspects — but DOES tell this player
        if they themselves are the killer (they need to know to play it).
        """
        d = self.to_public_dict()
        if self.role_id:
            d["role"] = {
                "id": self.role_id,
                "name": self.role_name,
                "emoji": self.role_emoji,
                "description": self.role_description,
            }
        d["clues_found"] = list(self.clues_found)
        d["accusation"] = self.accusation
        d["on_topic_questions"] = self.on_topic_questions
        d["confront_result"] = self.confront_result
        d["is_killer"] = self.is_human_killer
        return d


@dataclass
class GameSettings:
    """Settings picked by the admin at game creation."""

    agent_entity_id: str            # required — the LLM
    music_player: str | None = None
    tts_entity: str | None = None
    entity_ids: list[str] = field(default_factory=list)  # the clue pool
    difficulty: str = "medium"
    suspect_count: int = 4
    # TTS narration speed key ("slow" | "normal" | "fast"); maps to a numeric
    # rate in const.TTS_SPEEDS. Lets the host slow down a too-fast voice.
    tts_speed: str = "normal"
    # Lights to drive for dramatic effect (act moods, flashes). Distinct from
    # the atmosphere `entity_ids` used only as narration flavour. May be light
    # entities OR light groups OR a whole area is fine — we expand groups at
    # call time. Empty means no lighting effects.
    light_entity_ids: list[str] = field(default_factory=list)
    lights_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_entity_id": self.agent_entity_id,
            "music_player": self.music_player,
            "tts_entity": self.tts_entity,
            "entity_ids": list(self.entity_ids),
            "difficulty": self.difficulty,
            "suspect_count": self.suspect_count,
            "tts_speed": self.tts_speed,
            "light_entity_ids": list(self.light_entity_ids),
            "lights_enabled": self.lights_enabled,
        }


# ---------------------------------------------------------------------------
# GameSession
# ---------------------------------------------------------------------------

class GameSession:
    """A single Mortify game from creation to reveal."""

    def __init__(
        self,
        session_id: str,
        settings: GameSettings,
        hass: HomeAssistant | None = None,
    ) -> None:
        self.session_id = session_id
        self.join_code = self._generate_join_code()
        self.settings = settings
        self._hass = hass

        self.state: str = STATE_LOBBY
        self.players: dict[str, Player] = {}
        self.story: dict[str, Any] | None = None
        # Snapshot of the entities chosen by the admin (with friendly_name
        # + area). Set when the game is created so we don't have to look
        # them up again during clue discovery.
        self.entities: list[dict[str, Any]] = []

        self.created_at: float = time.time()
        self._listeners: set[Listener] = set()
        self._lock = asyncio.Lock()
        self._cancelled = False

        # The LLM client is created when we start the game (the agent
        # entity id is in settings). Held here so NPC interrogation
        # during the game reuses it.
        self._llm: LLMClient | None = None

        # Music control callbacks — wired up by the manager.
        self._music_start_cb: Callable[[], Awaitable[None]] | None = None
        self._music_stop_cb: Callable[[], Awaitable[None]] | None = None
        # Lighting callback — wired up by the manager. Takes a "mood" key
        # (an act state) or "flash" and drives the host's selected lights.
        self._lights_cb: Callable[[str], Awaitable[None]] | None = None

        # --- human killer / auto-progression bookkeeping -----------------
        # player_id of the human who secretly IS the killer (or None when the
        # killer is an NPC / not yet assigned).
        self.human_killer_id: str | None = None
        # Last time we auto-advanced an act, to debounce a flurry of unlocks.
        self._last_auto_advance: float = 0.0
        # Pending question awaiting a human killer's chosen reply.
        # Maps a request_id -> dict with the asker, question, options, and an
        # asyncio.Future the asker's handler awaits.
        self._killer_pending: dict[str, dict[str, Any]] = {}

    # --- listeners ---------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register a listener; returns an unsubscribe callable."""
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Fan an event out to every subscriber.

        Listeners that raise are logged-and-ignored — one bad client
        socket must not break the broadcast.
        """
        msg = {"event": event_type, **payload}
        for listener in list(self._listeners):
            try:
                await listener(msg)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Mortify listener raised; continuing")

    def set_music_callbacks(
        self,
        on_start: Callable[[], Awaitable[None]] | None,
        on_stop: Callable[[], Awaitable[None]] | None,
    ) -> None:
        """Wire in side effects for game start/end (music control)."""
        self._music_start_cb = on_start
        self._music_stop_cb = on_stop

    def set_lights_callback(
        self, cb: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        """Wire in the lighting side effect (driven by the manager).

        The callback receives a mood key — one of the act state strings
        (``act_1`` ... ``reveal``) for a smooth mood change, or ``"flash"``
        for a short dramatic pulse on a clue reveal / confrontation.
        """
        self._lights_cb = cb

    async def _set_lights(self, mood: str) -> None:
        """Fire the lighting callback, swallowing any error.

        Lights are pure flavour: a failure here (bulb offline, group empty,
        no lights selected) must never interrupt the game.
        """
        if self._lights_cb is None:
            return
        try:
            await self._lights_cb(mood)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Mortify lighting callback raised", exc_info=True)

    # --- player management ------------------------------------------------

    def add_player(self, name: str) -> Player:
        """Add a player; returns the Player object.

        Late-joiners (after the game has started) join as observers and
        do not receive an assigned role. They can still interrogate
        suspects and submit an accusation.

        Raises:
            SessionFullError: capacity reached.
        """
        if len(self.players) >= MAX_PLAYERS_PER_SESSION:
            raise SessionFullError(
                f"Session is full ({MAX_PLAYERS_PER_SESSION} max)"
            )
        player_id = secrets.token_urlsafe(8)

        # Dedup against case-insensitive collisions.
        existing = {p.name.lower() for p in self.players.values()}
        base = _sanitize_player_name(name) or "Detective"
        final = base
        n = 2
        while final.lower() in existing:
            final = f"{base} {n}"[:MAX_PLAYER_NAME_LENGTH]
            n += 1
            if n > MAX_PLAYERS_PER_SESSION + 2:
                final = f"{base} {player_id[:4]}"
                break

        player = Player(player_id=player_id, name=final)
        self.players[player_id] = player

        # If we're past the lobby, assign a role if any are still free.
        # This lets observers participate in the interrogation phase with
        # full identity — but only if the story already has unused roles.
        if self.state != STATE_LOBBY and self.story is not None:
            self._assign_role_to_player(player)

        # Fire-and-forget event broadcast.
        if self._hass is not None:
            self._hass.async_create_task(
                self._emit(EVENT_PLAYER_JOINED, {"player": player.to_public_dict()})
            )
        return player

    def _assign_role_to_player(self, player: Player) -> None:
        """Assign the first unclaimed suspect role to a player."""
        if not self.story:
            return
        claimed: set[str] = {
            p.role_id for p in self.players.values() if p.role_id is not None
        }
        for suspect in self.story.get("suspects", []):
            rid = suspect.get("role_id")
            if rid and rid not in claimed:
                # Don't hand a late-joiner the killer role — they'd have
                # missed the setup and the killer mechanic. Skip it; they
                # take the next free innocent role, or become an observer.
                if rid == self.story.get("killer_id"):
                    continue
                player.role_id = rid
                player.role_name = suspect.get("role_name")
                player.role_emoji = suspect.get("role_emoji")
                player.role_description = suspect.get("role_description")
                return
        # No free roles — they become an observer.

    def remove_player(self, player_id: str) -> None:
        """Quietly remove a player (e.g. socket closed). No-op if absent."""
        player = self.players.pop(player_id, None)
        if player is not None and self._hass is not None:
            self._hass.async_create_task(
                self._emit(
                    EVENT_PLAYER_LEFT,
                    {"player_id": player_id, "name": player.name},
                )
            )

    # --- game flow --------------------------------------------------------

    async def start(self) -> None:
        """Move from lobby to act 1, generating the story along the way.

        Story generation is the slow part (LLM round-trip). We emit a
        ``generating`` event up front so clients can show a loading
        state, then run the LLM, then transition to act 1.
        """
        async with self._lock:
            if self.state != STATE_LOBBY:
                raise InvalidStateError(
                    f"Cannot start from state {self.state!r}"
                )
            if not self.settings.agent_entity_id:
                raise InvalidStateError("No conversation agent selected")
            self.state = STATE_GENERATING

        await self._emit(
            EVENT_GENERATING,
            {"message": "The AI is crafting your murder mystery..."},
        )

        # Create the LLM client. It uses HA's conversation.process under the
        # hood and is stateless — we hold one per session so NPC chats can
        # reuse it.
        if self._hass is None:
            raise InvalidStateError("Session has no HomeAssistant; cannot run LLM")
        self._llm = LLMClient(self._hass, self.settings.agent_entity_id)

        player_names = [p.name for p in self.players.values()]
        rooms = sorted({
            e.get("area") for e in self.entities
            if e.get("area") and e.get("area") != "Unknown Room"
        })

        try:
            story = await generate_mystery(
                self._llm,
                rooms=list(rooms),
                entities=self.entities,
                player_names=player_names,
                suspect_count=self.settings.suspect_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Mystery generation crashed unexpectedly")
            # Force-end so the admin gets out of the generating state.
            self.state = STATE_LOBBY
            await self._emit(
                EVENT_GAME_ENDED,
                {"reason": "generation_failed"},
            )
            return

        async with self._lock:
            self.story = story
            self._assign_roles(story)
            self.state = STATE_ACT_1

        # Music starts now (the admin's chosen "scene" speaker).
        if self._music_start_cb is not None:
            try:
                await self._music_start_cb()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Music start callback raised", exc_info=True)

        # Set the act-1 lighting mood.
        await self._set_lights(STATE_ACT_1)

        # Announce act 1.
        narration = story.get("opening_narration", "") or self._fallback_act_narration(1)
        await self._announce(narration)
        await self._emit(
            EVENT_ACT_STARTED,
            {
                "act": 1,
                "state": STATE_ACT_1,
                "title": ACT_TITLES.get(STATE_ACT_1, "Discovery"),
                "narration": narration,
            },
        )

    def _assign_roles(self, story: dict[str, Any]) -> None:
        """Assign suspect roles to the players present at game start.

        Two goals beyond the original "zip in join order":

        1. **Hidden mapping** — who plays whom is never broadcast (see
           ``Player.to_public_dict``), so assignment order doesn't matter for
           secrecy, but we still randomise it so a human killer can't be
           guessed from join position.
        2. **Human killer** — when at least ``MIN_PLAYERS_FOR_HUMAN_KILLER``
           humans are present, the suspect flagged ``is_killer`` in the story
           is handed to a randomly chosen human, who is told they're guilty
           and will field other players' questions to that suspect. With fewer
           players the killer stays an NPC so small games still work.

        Caller must hold ``self._lock``.
        """
        suspects = list(story.get("suspects", []))
        players = list(self.players.values())
        if not suspects:
            return

        killer_role_id = story.get("killer_id")
        human_count = len(players)
        want_human_killer = human_count >= MIN_PLAYERS_FOR_HUMAN_KILLER

        # Randomise which player takes which role.
        shuffled_players = players[:]
        secrets.SystemRandom().shuffle(shuffled_players)

        killer_suspect = next(
            (s for s in suspects if s.get("role_id") == killer_role_id), None,
        )

        assigned: set[str] = set()  # role_ids already handed out

        # Step 1: if we want a human killer, give the killer role to the first
        # shuffled player and pull them out of the pool.
        self.human_killer_id = None
        remaining_players = shuffled_players[:]
        if want_human_killer and killer_suspect is not None and remaining_players:
            killer_player = remaining_players.pop(0)
            killer_player.role_id = killer_suspect.get("role_id")
            killer_player.role_name = killer_suspect.get("role_name")
            killer_player.role_emoji = killer_suspect.get("role_emoji")
            killer_player.role_description = killer_suspect.get("role_description")
            killer_player.is_human_killer = True
            self.human_killer_id = killer_player.player_id
            assigned.add(killer_suspect.get("role_id"))

        # Step 2: hand the remaining (non-killer if a human killer was set)
        # suspects to the remaining players in shuffled order.
        free_suspects = [s for s in suspects if s.get("role_id") not in assigned]
        for player, suspect in zip(remaining_players, free_suspects):
            player.role_id = suspect.get("role_id")
            player.role_name = suspect.get("role_name")
            player.role_emoji = suspect.get("role_emoji")
            player.role_description = suspect.get("role_description")
            assigned.add(suspect.get("role_id"))

        # We intentionally do NOT stamp suspect["player"] with the human's
        # name any more — the cast must stay anonymous during play. The
        # reveal projection reconstructs who-played-whom from the Player
        # objects instead.

    async def next_act(self, *, auto: bool = False, reason: str = "") -> dict[str, Any]:
        """Advance to the next act.

        Transitions:
          act_1 -> act_2 -> act_3 -> accusation
          accusation -> reveal (via ``reveal_killer``, not this method)

        ``auto=True`` marks a clue-driven automatic advance and emits an extra
        ``act_auto_advanced`` event (with ``reason``) so every client can show
        a "the investigation moves on…" banner. Returns the new state. Raises
        InvalidStateError on misuse.
        """
        async with self._lock:
            if self.state not in ACT_ORDER:
                raise InvalidStateError(
                    f"next_act not allowed from {self.state!r}"
                )
            try:
                current_idx = ACT_ORDER.index(self.state)
            except ValueError:
                raise InvalidStateError(f"Unknown state {self.state!r}") from None
            if current_idx + 1 >= len(ACT_ORDER):
                # We're already at accusation — caller should reveal instead.
                raise InvalidStateError(
                    "Already at accusation; call reveal_killer next"
                )
            new_state = ACT_ORDER[current_idx + 1]
            self.state = new_state
            act_num = current_idx + 2  # 1-indexed: act_2 is the 2nd act
            self._last_auto_advance = time.time()

        # Lighting mood follows the new act.
        await self._set_lights(new_state)

        narration = ""
        if self.story:
            home_event = self._pick_home_event()
            llm = self._llm
            if llm is not None:
                try:
                    narration = await generate_act_narration(
                        llm, self.story, act_num, home_event,
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Narration enhancement failed", exc_info=True)
            if not narration:
                narration = self._raw_act_narration(act_num) or self._fallback_act_narration(act_num)

        # When the story moved on by itself, lead the narration with a short
        # "the investigation advances" line so it's clear to everyone why.
        if auto and reason:
            narration = f"{reason} {narration}".strip()
            await self._emit(
                EVENT_ACT_AUTO_ADVANCED,
                {
                    "act": act_num,
                    "state": new_state,
                    "title": ACT_TITLES.get(new_state, f"Act {act_num}"),
                    "reason": reason,
                },
            )

        await self._announce(narration)
        await self._emit(
            EVENT_ACT_STARTED,
            {
                "act": act_num,
                "state": new_state,
                "title": ACT_TITLES.get(new_state, f"Act {act_num}"),
                "narration": narration,
                "auto": auto,
            },
        )
        return {"state": new_state, "act": act_num}

    # --- clue-driven automatic progression --------------------------------

    def _clue_unlock_fraction(self) -> float:
        """Fraction of all clues that have been unlocked by *anyone*.

        Drives auto-progression. We count distinct clue ids unlocked across
        all players, divided by the total clue count. Returns 0.0 when there
        are no clues yet.
        """
        clues = self._all_clues()
        if not clues:
            return 0.0
        unlocked: set[str] = set()
        for p in self.players.values():
            unlocked.update(p.clues_found)
        # Only count ids that actually exist in the story.
        valid = {c.get("id") for c in clues}
        unlocked &= valid
        return len(unlocked) / len(clues)

    def _auto_advance_reason(self, new_state: str) -> str:
        """A dramatic, story-agnostic banner line for an automatic advance."""
        return {
            STATE_ACT_1: "",  # never auto-advance INTO act 1
            "act_2": "The first threads unravel — the investigation deepens.",
            "act_3": "Too much has surfaced to ignore. The shadows close in.",
            STATE_ACCUSATION: "The evidence has spoken. It is time to name a killer.",
        }.get(new_state, "The investigation advances.")

    async def _maybe_auto_advance(self) -> None:
        """Advance the act if collective clue progress crossed a threshold.

        Called after every successful clue unlock. Debounced by
        ``AUTO_ADVANCE_MIN_INTERVAL`` so several near-simultaneous unlocks
        can't skip multiple acts. The host's manual "Advance Act" still works
        independently; this just means the story keeps moving even if the host
        is distracted (or there's no host watching at all).
        """
        if not AUTO_ADVANCE_ENABLED:
            return
        # Snapshot decision under the lock to avoid racing two unlocks.
        async with self._lock:
            if self.state not in AUTO_ADVANCE_THRESHOLDS:
                return
            now = time.time()
            if now - self._last_auto_advance < AUTO_ADVANCE_MIN_INTERVAL:
                return
            threshold = AUTO_ADVANCE_THRESHOLDS[self.state]
            if self._clue_unlock_fraction() < threshold:
                return
            current_idx = ACT_ORDER.index(self.state)
            if current_idx + 1 >= len(ACT_ORDER):
                return
            new_state = ACT_ORDER[current_idx + 1]
        # Release the lock before calling next_act (which re-acquires it).
        reason = self._auto_advance_reason(new_state)
        try:
            await self.next_act(auto=True, reason=reason)
        except InvalidStateError:
            # Raced with a manual advance — harmless.
            _LOGGER.debug("Auto-advance raced with a manual transition")

    async def submit_accusation(
        self, player_id: str, accused_role_id: str,
    ) -> dict[str, Any]:
        """Record a player's accusation.

        If every player has now accused, automatically resolve into the
        reveal phase. Returns ``{"submitted": True, "all_in": bool}``.
        """
        async with self._lock:
            if self.state != STATE_ACCUSATION:
                raise InvalidStateError("Not in accusation phase")
            player = self.players.get(player_id)
            if player is None:
                raise InvalidStateError("Unknown player")
            if not self.story:
                raise InvalidStateError("No story yet")
            valid_ids = {s.get("role_id") for s in self.story.get("suspects", [])}
            if accused_role_id not in valid_ids:
                raise InvalidStateError("Accused role does not exist")
            player.accusation = accused_role_id
            all_in = all(
                p.accusation is not None for p in self.players.values()
            )

        await self._emit(
            EVENT_ACCUSATION_SUBMITTED,
            {
                "player_id": player_id,
                "name": player.name,
                "accused_role_id": accused_role_id,
            },
        )
        if all_in:
            await self.reveal_killer()
        return {"submitted": True, "all_in": all_in}

    async def reveal_killer(self) -> None:
        """End the game and reveal the killer with full reveal narration.

        Computes final scores, emits the reveal event, and transitions
        to ENDED. Safe to call from accusation OR if forced from the
        admin panel before all players have accused.
        """
        async with self._lock:
            if self.state == STATE_REVEAL or self.state == STATE_ENDED:
                return
            if self.state != STATE_ACCUSATION:
                # Admins can short-circuit by ending early. Switching
                # state still works; scoring just operates on whoever
                # has accused.
                _LOGGER.info(
                    "reveal_killer called from %r; forcing reveal anyway",
                    self.state,
                )
            if not self.story:
                self.state = STATE_ENDED
                await self._emit(EVENT_GAME_ENDED, {"reason": "no_story"})
                return
            self.state = STATE_REVEAL
            killer_id = self.story.get("killer_id", "")
            killer_suspect = next(
                (s for s in self.story.get("suspects", [])
                 if s.get("role_id") == killer_id),
                None,
            )

            # Score every player.
            for player in self.players.values():
                player.is_correct = (
                    player.accusation is not None
                    and player.accusation == killer_id
                )
                self._score_player(player, killer_id, killer_suspect)

        # Speak the reveal narration with music ducking handled by the
        # _announce side effect.
        reveal_text = self.story.get(
            "reveal_narration", "The truth is finally revealed."
        )
        # Bring the lights up gold and warm for the finale.
        await self._set_lights(STATE_REVEAL)
        await self._announce(reveal_text)

        await self._emit(
            EVENT_REVEALED,
            {
                "killer_id": killer_id,
                "killer": killer_suspect,
                "reveal_narration": reveal_text,
                "motive": self.story.get("motive", ""),
                "players": [
                    p.to_public_dict() for p in self._ranked_players()
                ],
            },
        )

        # Mark fully ended a moment later so clients have time to show
        # the reveal animation. We don't actually tear down here — the
        # manager handles end_session for that.
        async with self._lock:
            self.state = STATE_ENDED

    def _score_player(
        self,
        player: Player,
        killer_id: str,
        killer_suspect: dict[str, Any] | None,
    ) -> None:
        """Award final accusation points on top of in-game earnings.

        On-topic question bonuses, per-clue points, and confront points are
        awarded live during the game (so the running scoreboard is real). At
        reveal we add only:

        * Correct accusation: ``POINTS_CORRECT_ACCUSATION``.
        * Evidence bonus: if the player accused correctly AND holds at least
          one clue that genuinely implicates or contradicts the killer —
          proof they followed the real trail rather than guessing.
        """
        if player.accusation == killer_id:
            player.score += POINTS_CORRECT_ACCUSATION

            # Real evidence check: did they unlock a clue pointing at the killer?
            held_killer_clue = any(
                (c.get("implicates") == killer_id or c.get("contradicts") == killer_id)
                for c in self._all_clues()
                if c.get("id") in player.clues_found
            )
            if held_killer_clue:
                player.score += POINTS_EVIDENCE_BONUS

    # --- player actions during the game -----------------------------------

    def _all_clues(self) -> list[dict[str, Any]]:
        """Return the story's clue list (empty if no story yet)."""
        if not self.story:
            return []
        return [c for c in self.story.get("clues", []) if isinstance(c, dict)]

    def clue_details_for(self, player_id: str) -> list[dict[str, Any]]:
        """Full details of the clues a given player has personally unlocked.

        The public clue list is opaque (ids only) so locked clues leak nothing,
        so this is how a player reads back the clues they've earned — title,
        text, and which suspect held it. Returns [] for unknown players.
        """
        player = self.players.get(player_id)
        if player is None or not self.story:
            return []
        found = set(player.clues_found)
        out: list[dict[str, Any]] = []
        for c in self._all_clues():
            if c.get("id") in found:
                out.append({
                    "id": c.get("id"),
                    "title": c.get("title", "A Clue"),
                    "text": c.get("text", ""),
                    "held_by": c.get("held_by"),
                })
        return out

    def _clue_by_id(self, clue_id: str) -> dict[str, Any] | None:
        for c in self._all_clues():
            if c.get("id") == clue_id:
                return c
        return None

    def _clues_held_by(self, role_id: str) -> list[dict[str, Any]]:
        return [c for c in self._all_clues() if c.get("held_by") == role_id]

    async def _unlock_clue(
        self, player: Player, clue_id: str,
    ) -> dict[str, Any] | None:
        """Add a clue to a player's found list and broadcast it.

        Caller must hold ``self._lock``. Returns the clue dict if it was
        newly unlocked, or None if the player already had it / it doesn't
        exist. The public broadcast leaks only the count, never the text.
        """
        clue = self._clue_by_id(clue_id)
        if clue is None or clue_id in player.clues_found:
            return None
        player.clues_found.append(clue_id)
        return clue

    async def discover_clue(
        self, player_id: str, clue_id: str,
    ) -> dict[str, Any]:
        """Look up a clue the player has already unlocked.

        Clues are unlocked through interrogation, not manual examination —
        but the player page lets a player re-open a clue they've found to
        read its full text. This returns the full clue payload IF the player
        has unlocked it; otherwise it refuses (you can't read a locked clue).
        """
        async with self._lock:
            if self.state not in ACT_ORDER:
                raise InvalidStateError("Not in an investigation phase")
            player = self.players.get(player_id)
            if player is None:
                raise InvalidStateError("Unknown player")
            if not self.story:
                raise InvalidStateError("No story yet")
            if clue_id not in player.clues_found:
                raise InvalidStateError("You haven't uncovered that clue yet")
            clue = self._clue_by_id(clue_id)
            if clue is None:
                raise InvalidStateError("Unknown clue")
            return {
                "player_id": player_id,
                "clue_id": clue_id,
                "title": clue.get("title", "A Clue"),
                "clue_text": clue.get("text", ""),
                "implicates": clue.get("implicates"),
                "contradicts": clue.get("contradicts"),
            }

    async def submit_interrogation(
        self, player_id: str, suspect_role_id: str, question: str,
    ) -> dict[str, Any]:
        """Ask a suspect a question. Returns the reply.

        Interrogation is only valid during the investigation acts (1-3),
        and rate-limited per (player, suspect) pair.
        """
        # Defensive sanitisation — accept Unicode, strip control chars.
        question = (question or "").strip()
        if not question:
            raise InvalidStateError("Empty question")
        if len(question) > MAX_INTERROGATION_LENGTH:
            question = question[:MAX_INTERROGATION_LENGTH]

        from .const import INTERROGATION_RATE, INTERROGATION_WINDOW
        now = time.time()

        async with self._lock:
            # Interrogation only during acts 1-3 (not accusation phase —
            # by then suspects have finished their statements).
            # ACT_ORDER[:3] == [act_1, act_2, act_3].
            if self.state not in ACT_ORDER[:3]:
                raise InvalidStateError("Not in an interrogation phase")
            player = self.players.get(player_id)
            if player is None:
                raise InvalidStateError("Unknown player")
            if not self.story:
                raise InvalidStateError("No story yet")
            suspect = next(
                (s for s in self.story.get("suspects", [])
                 if s.get("role_id") == suspect_role_id),
                None,
            )
            if suspect is None:
                raise InvalidStateError("Unknown suspect")
            # Rate limit
            recent = [
                t for t in player._question_times.get(suspect_role_id, [])
                if now - t < INTERROGATION_WINDOW
            ]
            if len(recent) >= INTERROGATION_RATE:
                raise InvalidStateError("Slow down — too many questions")
            recent.append(now)
            player._question_times[suspect_role_id] = recent
            history = list(player.chat_history.get(suspect_role_id, []))
            llm = self._llm

        if llm is None:
            raise InvalidStateError("LLM not initialised")

        # Gather what this suspect can reveal and what the player already has,
        # so the NPC call (or the human killer's evasion options) can score
        # relevance and let a clue slip.
        held = self._clues_held_by(suspect_role_id)
        already = set(player.clues_found)

        # If the suspect being questioned is played by the HUMAN KILLER (and
        # the asker isn't that killer interrogating themselves), hand control
        # to the human: they choose how to deflect from pre-generated options.
        # Otherwise fall back to the normal AI NPC reply. Both paths return the
        # same (reply, on_topic, revealed_clue_id) shape.
        use_human_killer = (
            self.human_killer_id is not None
            and self.human_killer_id != player_id
            and self.human_killer_id in self.players
            and self.players[self.human_killer_id].role_id == suspect_role_id
        )

        if use_human_killer:
            result = await self._human_killer_reply(
                asker_id=player_id,
                suspect=suspect,
                history=history,
                question=question,
                held_clues=held,
                already_revealed=already,
            )
        else:
            # We deliberately make the LLM call OUTSIDE the lock — it can
            # take seconds and we don't want to block other players' actions.
            result = await generate_npc_reply(
                llm=llm,
                suspect=suspect,
                story=self.story,
                history=history,
                question=question,
                is_killer=bool(suspect.get("is_killer", False)),
                held_clues=held,
                already_revealed=already,
            )
        reply = result["reply"]
        on_topic = result["on_topic"]
        revealed_clue_id = result["revealed_clue_id"]

        unlocked_clue: dict[str, Any] | None = None
        awarded_points = 0

        # Re-acquire the lock to append to history and apply scoring.
        async with self._lock:
            player = self.players.get(player_id)
            if player is not None:
                hist = player.chat_history.setdefault(suspect_role_id, [])
                hist.append({"role": "user", "content": question})
                hist.append({"role": "assistant", "content": reply})
                # Cap to last 10 exchanges to keep prompt size reasonable.
                if len(hist) > 20:
                    del hist[: len(hist) - 20]

                # On-topic questioning bonus (capped).
                if on_topic and player.on_topic_questions < MAX_SCORED_QUESTIONS_PER_PLAYER:
                    player.on_topic_questions += 1
                    player.score += POINTS_ON_TOPIC_QUESTION
                    awarded_points += POINTS_ON_TOPIC_QUESTION

                # Clue reveal — unlock for this player if newly disclosed.
                if revealed_clue_id:
                    unlocked_clue = await self._unlock_clue(player, revealed_clue_id)
                    if unlocked_clue is not None:
                        player.score += POINTS_PER_CLUE_DISCOVERED
                        awarded_points += POINTS_PER_CLUE_DISCOVERED

        # Public event so the host sees "Alice questioned the Butler"
        # without leaking the content.
        await self._emit(
            EVENT_INTERROGATION_REPLY,
            {
                "player_id": player_id,
                "name": self.players[player_id].name if player_id in self.players else "",
                "suspect_role_id": suspect_role_id,
            },
        )

        # If a clue was unlocked, broadcast the count bump (no text).
        if unlocked_clue is not None and player_id in self.players:
            await self._emit(
                EVENT_CLUE_UNLOCKED,
                {
                    "player_id": player_id,
                    "name": self.players[player_id].name,
                    "clues_found_count": len(self.players[player_id].clues_found),
                },
            )

        payload: dict[str, Any] = {
            "player_id": player_id,
            "suspect_role_id": suspect_role_id,
            "question": question,
            "reply": reply,
            "on_topic": on_topic,
            "points_awarded": awarded_points,
        }
        if unlocked_clue is not None:
            payload["unlocked_clue"] = {
                "clue_id": unlocked_clue.get("id"),
                "title": unlocked_clue.get("title", "A Clue"),
                "clue_text": unlocked_clue.get("text", ""),
            }

        # A fresh clue unlock may push the case past an act threshold. Flash
        # the lights for the discovery and check whether the story should
        # move itself forward. Both are best-effort and never block the reply.
        if unlocked_clue is not None:
            await self._set_lights("flash")
            await self._maybe_auto_advance()

        return payload

    async def _human_killer_reply(
        self,
        asker_id: str,
        suspect: dict[str, Any],
        history: list[dict[str, str]],
        question: str,
        held_clues: list[dict[str, Any]],
        already_revealed: set[str],
    ) -> dict[str, Any]:
        """Route a question to the human killer and wait for their choice.

        We pre-generate a few in-character evasions (via the LLM, with a stock
        fallback) and push them to the killer as a ``killer_prompt`` event. The
        asker is told (privately) that the suspect is "considering" their words
        via ``awaiting_killer``. When the killer picks an option — or after
        ``KILLER_RESPONSE_TIMEOUT`` — we resolve with the chosen reply.

        The killer is deliberately given options that NEVER volunteer a clue
        (a human killer shouldn't be able to accidentally incriminate
        themselves through the option list), so ``revealed_clue_id`` is always
        None on this path. On-topic scoring still applies, judged cheaply by
        the option generator.
        """
        request_id = secrets.token_urlsafe(8)
        llm = self._llm

        # Build evasion options + an on-topic judgement. Done outside the lock.
        try:
            gen = await generate_killer_options(
                llm=llm,
                suspect=suspect,
                story=self.story or {},
                history=history,
                question=question,
                option_count=KILLER_OPTION_COUNT,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Killer option generation failed; using stock", exc_info=True)
            gen = {"options": [], "on_topic": False}

        options: list[str] = gen.get("options") or []
        if not options:
            options = [
                "I've already told you everything I know. Why do you keep circling back to me?",
                "You're reaching, Detective. I had no quarrel with the victim.",
                "Ask the others — I'm not the only one who was unaccounted for.",
            ][:KILLER_OPTION_COUNT]
        on_topic = bool(gen.get("on_topic", False))

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        async with self._lock:
            self._killer_pending[request_id] = {
                "asker_id": asker_id,
                "suspect_role_id": suspect.get("role_id"),
                "question": question,
                "options": options,
                "on_topic": on_topic,
                "future": future,
            }

        # Tell the killer it's their move (private — forwarded only to them).
        await self._emit(
            EVENT_KILLER_PROMPT,
            {
                "_target_player_id": self.human_killer_id,
                "request_id": request_id,
                "suspect_role_id": suspect.get("role_id"),
                "asker_name": self.players[asker_id].name if asker_id in self.players else "A detective",
                "question": question,
                "options": options,
            },
        )
        # Tell the asker the suspect is weighing their answer.
        await self._emit(
            EVENT_AWAITING_KILLER,
            {
                "_target_player_id": asker_id,
                "suspect_role_id": suspect.get("role_id"),
            },
        )

        # Wait for the killer's pick, or fall back on timeout.
        chosen: str
        try:
            chosen = await asyncio.wait_for(future, timeout=KILLER_RESPONSE_TIMEOUT)
        except asyncio.TimeoutError:
            chosen = secrets.choice(options)
        finally:
            async with self._lock:
                self._killer_pending.pop(request_id, None)

        return {
            "reply": chosen,
            "on_topic": on_topic,
            # The human killer can never leak a clue via their option list.
            "revealed_clue_id": None,
        }

    async def killer_respond(self, player_id: str, request_id: str, choice_index: int) -> dict[str, Any]:
        """Resolve a pending human-killer question with the killer's pick.

        Only the human killer may answer, and only their own pending requests.
        ``choice_index`` selects one of the options sent in the prompt.
        """
        async with self._lock:
            if player_id != self.human_killer_id:
                raise InvalidStateError("You are not the one being questioned")
            pending = self._killer_pending.get(request_id)
            if pending is None:
                raise InvalidStateError("That question has already been answered")
            options = pending["options"]
            if not isinstance(choice_index, int) or not (0 <= choice_index < len(options)):
                raise InvalidStateError("Invalid choice")
            future: asyncio.Future = pending["future"]
            if not future.done():
                future.set_result(options[choice_index])
        return {"ok": True}

    async def confront_suspect(
        self, player_id: str, suspect_role_id: str, clue_id: str,
    ) -> dict[str, Any]:
        """Spend a player's one-shot confrontation against a suspect.

        The player picks a clue they've unlocked and throws it at a suspect.
        It pays off big (``POINTS_CONFRONT_SUCCESS``) when the clue genuinely
        implicates OR contradicts that suspect AND that suspect is the killer.
        A partially-right confront (clue fits the suspect but they're innocent)
        is neutral. A baseless confront (clue has nothing to do with the
        suspect) costs ``POINTS_CONFRONT_WASTED``. Each player gets exactly one.
        """
        question_label = "I know what you did. Explain THIS."
        async with self._lock:
            if self.state not in ACT_ORDER[:3]:
                raise InvalidStateError("Not in an interrogation phase")
            player = self.players.get(player_id)
            if player is None:
                raise InvalidStateError("Unknown player")
            if player.confront_result is not None:
                raise InvalidStateError("You've already used your confrontation")
            if not self.story:
                raise InvalidStateError("No story yet")
            suspect = next(
                (s for s in self.story.get("suspects", [])
                 if s.get("role_id") == suspect_role_id),
                None,
            )
            if suspect is None:
                raise InvalidStateError("Unknown suspect")
            if clue_id not in player.clues_found:
                raise InvalidStateError("You can only confront with a clue you've uncovered")
            clue = self._clue_by_id(clue_id)
            if clue is None:
                raise InvalidStateError("Unknown clue")

            killer_id = self.story.get("killer_id", "")
            fits = (
                clue.get("implicates") == suspect_role_id
                or clue.get("contradicts") == suspect_role_id
            )
            is_killer = suspect_role_id == killer_id

            if fits and is_killer:
                delta = POINTS_CONFRONT_SUCCESS
                outcome = "nailed"
            elif fits:
                delta = 0
                outcome = "plausible"
            else:
                delta = POINTS_CONFRONT_WASTED
                outcome = "baseless"

            player.score += delta
            player.confront_result = {
                "suspect_role_id": suspect_role_id,
                "clue_id": clue_id,
                "outcome": outcome,
                "points": delta,
            }
            history = list(player.chat_history.get(suspect_role_id, []))
            llm = self._llm
            confronting_clue = clue

        # Generate an in-character reaction outside the lock.
        reply = ""
        if llm is not None and self.story:
            try:
                npc = await generate_npc_reply(
                    llm=llm,
                    suspect=suspect,
                    story=self.story,
                    history=history,
                    question=question_label,
                    is_killer=is_killer,
                    held_clues=[],
                    already_revealed=set(player.clues_found),
                    confronting_clue=confronting_clue,
                )
                reply = npc.get("reply", "")
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Confront reaction failed", exc_info=True)
        if not reply:
            reply = (
                "You... you have no idea what you're talking about."
                if outcome == "baseless" else
                "How... how could you possibly know that?"
            )

        await self._emit(
            EVENT_CONFRONT_RESOLVED,
            {
                "player_id": player_id,
                "name": self.players[player_id].name if player_id in self.players else "",
                "suspect_role_id": suspect_role_id,
                "outcome": outcome,
            },
        )
        # A confrontation is a big dramatic beat — flash the lights.
        await self._set_lights("flash")
        return {
            "player_id": player_id,
            "suspect_role_id": suspect_role_id,
            "clue_id": clue_id,
            "outcome": outcome,
            "points_awarded": delta,
            "reply": reply,
        }

    # --- ranking / serialisation -----------------------------------------

    def _ranked_players(self) -> list[Player]:
        return sorted(
            self.players.values(),
            key=lambda p: (-p.score, p.name.lower()),
        )

    def to_dict(self, include_secrets: bool = False) -> dict[str, Any]:
        """Render the session state for clients.

        ``include_secrets=True`` is only used by the admin during the
        reveal — it exposes the killer_id and reveal narration. During
        normal gameplay, this returns a sanitised view that's safe to
        broadcast to every player.
        """
        reveal_phase = include_secrets or self.state in (STATE_REVEAL, STATE_ENDED)

        suspects_public: list[dict[str, Any]] = []
        if self.story:
            # Map role_id -> the human playing it, used ONLY in the reveal /
            # admin views. During play the cast is fully anonymous.
            role_to_player: dict[str, str] = {}
            if reveal_phase:
                for p in self.players.values():
                    if p.role_id:
                        role_to_player[p.role_id] = p.name
            for s in self.story.get("suspects", []):
                pub = {
                    "role_id": s.get("role_id"),
                    "role_name": s.get("role_name"),
                    "role_emoji": s.get("role_emoji"),
                    "role_description": s.get("role_description"),
                    "alibi": s.get("alibi"),
                }
                if reveal_phase:
                    # Only now do we attach who played the suspect, the secret,
                    # and the killer flag.
                    pub["player"] = role_to_player.get(s.get("role_id"))
                    pub["secret"] = s.get("secret")
                    pub["is_killer"] = bool(s.get("is_killer"))
                suspects_public.append(pub)

        story_public: dict[str, Any] = {}
        if self.story:
            story_public = {
                "title": self.story.get("title", ""),
                "victim_name": self.story.get("victim_name", ""),
                "victim_description": self.story.get("victim_description", ""),
                "crime_scene": self.story.get("crime_scene", ""),
                "time_of_death": self.story.get("time_of_death", ""),
                "weapon": self.story.get("weapon", ""),
                "acts": self.story.get("acts", []),
            }
            if reveal_phase:
                story_public["killer_id"] = self.story.get("killer_id", "")
                story_public["motive"] = self.story.get("motive", "")
                story_public["reveal_narration"] = self.story.get(
                    "reveal_narration", ""
                )

        # Clue projection. During play we expose ONLY the clue id and a count —
        # never the title, the held_by suspect, or the "relevance" hint, since
        # those together would tell a player exactly which suspect to grill and
        # roughly what about, short-circuiting the interrogation. A player's own
        # unlocked clues (with full text) ride the private ``you`` projection.
        # At reveal (or for the admin) we expose everything.
        clues_public: list[dict[str, Any]] = []
        for c in self._all_clues():
            if reveal_phase:
                entry = {
                    "id": c.get("id"),
                    "title": c.get("title", "A Clue"),
                    "held_by": c.get("held_by"),
                    "relevance": c.get("relevance", ""),
                    "text": c.get("text", ""),
                    "implicates": c.get("implicates"),
                    "contradicts": c.get("contradicts"),
                }
            else:
                # Opaque placeholder: just enough for the UI to render "N of M
                # clues uncovered" and a row of locked slots.
                entry = {"id": c.get("id"), "locked": True}
            clues_public.append(entry)

        players_proj = (
            [p.to_reveal_dict() for p in self._ranked_players()]
            if reveal_phase
            else [p.to_public_dict() for p in self._ranked_players()]
        )

        return {
            "session_id": self.session_id,
            "join_code": self.join_code,
            "state": self.state,
            "settings": self.settings.to_dict(),
            "players": players_proj,
            "story": story_public,
            "suspects": suspects_public,
            "clues": clues_public,
            "entities": self.entities,
            "created_at": self.created_at,
            # Lets clients render "N of M clues" without leaking which.
            "clue_total": len(self._all_clues()),
        }

    # --- helpers ----------------------------------------------------------

    async def cancel(self) -> None:
        """Cancel the session from outside (manager.end_session)."""
        self._cancelled = True
        async with self._lock:
            if self.state == STATE_ENDED:
                return
            self.state = STATE_ENDED
        if self._music_stop_cb is not None:
            try:
                await self._music_stop_cb()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Music stop callback raised", exc_info=True)
        await self._emit(EVENT_GAME_ENDED, {"reason": "cancelled"})

    def _pick_home_event(self) -> str | None:
        """Return a human-readable description of a recent home event,
        if any of the selected entities are currently in a juicy state."""
        if not self._hass:
            return None
        interesting_domains = ("binary_sensor", "lock", "cover", "switch", "light")
        for e in self.entities:
            entity_id = e.get("entity_id", "")
            if "." not in entity_id:
                continue
            domain = entity_id.split(".", 1)[0]
            if domain not in interesting_domains:
                continue
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            name = state.attributes.get("friendly_name", entity_id)
            return f"the {name} just changed to {state.state}"
        return None

    def _raw_act_narration(self, act_num: int) -> str | None:
        if not self.story:
            return None
        for act in self.story.get("acts", []):
            if act.get("act") == act_num:
                return act.get("narration") or None
        return None

    def _fallback_act_narration(self, act_num: int) -> str:
        return {
            1: "The body has been discovered. The night grows long.",
            2: "The suspects gather. Their stories don't all line up.",
            3: "Evidence surfaces. Someone in this room is lying.",
            4: "The time has come. Name the killer.",
        }.get(act_num, "The investigation deepens.")

    async def _announce(self, message: str) -> None:
        """Speak a TTS announcement and broadcast the narration text.

        Same shape as Quizify's _announce: tts.speak with the TTS engine
        as entity_id and the media_player as media_player_entity_id.
        When TTS isn't configured, we still emit the narration as an
        event so the on-screen text appears for the host and players —
        the original Mortify silently swallowed the narration in that
        case, leaving players staring at an empty box.
        """
        if not message:
            return

        # Try TTS first if it's configured. Failures here are non-fatal:
        # the narration text still goes out as an event below.
        if self._hass is not None and self.settings.tts_entity:
            tts_entity = self.settings.tts_entity
            media_player = self.settings.music_player
            try:
                service_data: dict[str, Any] = {
                    "entity_id": tts_entity,
                    "message": message,
                    "cache": False,
                }
                if media_player:
                    service_data["media_player_entity_id"] = media_player
                # Pass a speaking-rate option when the host slowed/sped the
                # narrator. Different TTS engines name this differently and
                # most ignore unknown option keys, so we send a few common
                # spellings — Piper/Cloud honour the ones they recognise and
                # drop the rest. A speed of exactly 1.0 sends nothing (avoids
                # tripping engines that reject any options at all).
                speed = TTS_SPEEDS.get(self.settings.tts_speed, 1.0)
                if abs(speed - 1.0) > 1e-3:
                    service_data["options"] = {
                        "speed": speed,
                        "rate": speed,
                        "speaking_rate": speed,
                    }
                await self._hass.services.async_call(
                    "tts", "speak", service_data, blocking=False,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning("TTS announcement failed", exc_info=True)

        # Always emit the narration event — clients render this on screen
        # regardless of whether TTS played.
        await self._emit(EVENT_NARRATION, {"message": message})

    @staticmethod
    def _generate_join_code() -> str:
        """Short, human-friendly join code (no ambiguous chars).

        Same alphabet as Quizify: omits 0/O/I/1 so they can't be confused
        when reading from a QR-printed sticker.
        """
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(6))

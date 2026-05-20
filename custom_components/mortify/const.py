"""Constants for the Mortify integration.

The architecture mirrors Quizify: a session-based manager, a custom
React-style HA panel for the admin, and a dedicated unauthenticated
WebSocket for guest players that joins via a short join code.
"""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "mortify"

# Sidebar panel
PANEL_URL: Final = "mortify"  # sidebar url_path (no leading slash)
PANEL_TITLE: Final = "Mortify"
PANEL_ICON: Final = "mdi:knife"
PANEL_COMPONENT: Final = "mortify-panel"  # custom element name; must contain a hyphen

# Public URLs served by the integration.
STATIC_URL: Final = "/mortify-static"
PLAYER_WS_URL: Final = "/api/mortify/player_ws"
# Public guest landing page — players visit with ?code=XXXXXX.
PLAY_URL: Final = "/mortify/play"
JOIN_URL_PREFIX: Final = "/mortify/join"

# Player session token TTL (seconds) — used by the unauthenticated player socket.
PLAYER_TOKEN_TTL: Final = 60 * 60 * 6  # 6 hours

# --- Admin WebSocket commands ----------------------------------------------
# Admin commands ride HA's authenticated websocket; players use the separate
# unauthenticated socket above. Naming follows Quizify's convention.
WS_TYPE_ADMIN_SUBSCRIBE: Final = "mortify/admin/subscribe"
WS_TYPE_GAME_CREATE: Final = "mortify/game/create"
WS_TYPE_GAME_START: Final = "mortify/game/start"
WS_TYPE_GAME_END: Final = "mortify/game/end"
WS_TYPE_GAME_REMATCH: Final = "mortify/game/rematch"
WS_TYPE_GAME_NEXT_ACT: Final = "mortify/game/next_act"
WS_TYPE_GAME_REVEAL: Final = "mortify/game/reveal"
WS_TYPE_LIST_AGENTS: Final = "mortify/agents/list"
WS_TYPE_LIST_SPEAKERS: Final = "mortify/speakers/list"
WS_TYPE_LIST_TTS: Final = "mortify/tts/list"
WS_TYPE_LIST_ENTITIES: Final = "mortify/entities/list"

# --- Event types (server -> client, in subscriptions) ----------------------
EVENT_GAME_UPDATED: Final = "game_updated"
EVENT_GAME_ENDED: Final = "game_ended"
EVENT_PLAYER_JOINED: Final = "player_joined"
EVENT_PLAYER_LEFT: Final = "player_left"
EVENT_ACT_STARTED: Final = "act_started"
EVENT_NARRATION: Final = "narration"
EVENT_CLUE_DISCOVERED: Final = "clue_discovered"
EVENT_INTERROGATION_REPLY: Final = "interrogation_reply"
EVENT_ACCUSATION_SUBMITTED: Final = "accusation_submitted"
EVENT_REVEALED: Final = "revealed"
EVENT_GENERATING: Final = "generating"

# --- Game states -----------------------------------------------------------
# Mirrors Quizify's STATE_* pattern: lobby -> generating -> act_N -> accusation -> reveal -> ended.
STATE_LOBBY: Final = "lobby"
STATE_GENERATING: Final = "generating"
STATE_ACT_1: Final = "act_1"  # Discovery
STATE_ACT_2: Final = "act_2"  # Investigation
STATE_ACT_3: Final = "act_3"  # Shadows
STATE_ACCUSATION: Final = "accusation"
STATE_REVEAL: Final = "reveal"
STATE_ENDED: Final = "ended"

# Act order — used by next_act() to advance the state machine.
ACT_ORDER: Final = [STATE_ACT_1, STATE_ACT_2, STATE_ACT_3, STATE_ACCUSATION]

ACT_TITLES: Final = {
    STATE_ACT_1: "Discovery",
    STATE_ACT_2: "Investigation",
    STATE_ACT_3: "Shadows",
    STATE_ACCUSATION: "Accusation",
}

# --- Cast & weapons --------------------------------------------------------
# Suspect roles for the LLM to assign players into. Kept here (rather than the
# LLM inventing them) so the UI can show emoji + role description even before
# the LLM finishes generating the story.
SUSPECT_ROLES: Final = [
    {"id": "butler", "name": "The Butler", "emoji": "🎩",
     "description": "Impeccably proper, suspiciously loyal."},
    {"id": "doctor", "name": "The Doctor", "emoji": "⚕️",
     "description": "Calm under pressure, knows too much about poisons."},
    {"id": "artist", "name": "The Artist", "emoji": "🎨",
     "description": "Passionate, volatile, deeply in debt."},
    {"id": "lawyer", "name": "The Lawyer", "emoji": "⚖️",
     "description": "Silver-tongued, hides behind contracts."},
    {"id": "cook", "name": "The Cook", "emoji": "🍳",
     "description": "Last to see the victim alive. Or so they claim."},
    {"id": "gardener", "name": "The Gardener", "emoji": "🌿",
     "description": "Quiet, watchful, knows every corner of the estate."},
    {"id": "journalist", "name": "The Journalist", "emoji": "📰",
     "description": "Digging for a story — or burying one."},
    {"id": "widow", "name": "The Widow", "emoji": "🖤",
     "description": "Grief-stricken. Stands to inherit everything."},
]

WEAPONS: Final = [
    "a blunt instrument",
    "poison in the evening drink",
    "a loose stair rail",
    "a gas leak in the study",
    "a malfunctioning smart lock",
    "a tampered smoke detector",
    "a severed ethernet cable",
    "an allergic reaction, arranged",
]

# --- Difficulty / settings -------------------------------------------------
DIFFICULTY_EASY: Final = "easy"
DIFFICULTY_MEDIUM: Final = "medium"
DIFFICULTY_HARD: Final = "hard"
DIFFICULTIES: Final = [DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_HARD]

# Scoring
POINTS_CORRECT_ACCUSATION: Final = 1000
POINTS_PER_CLUE_DISCOVERED: Final = 50
# Bonus if the accuser found their accused suspect's room_clue
POINTS_EVIDENCE_BONUS: Final = 250

# --- Safety / resource limits ----------------------------------------------
MAX_CONCURRENT_SESSIONS: Final = 8
MAX_PLAYERS_PER_SESSION: Final = 12
MAX_PLAYER_NAME_LENGTH: Final = 20
MAX_INTERROGATION_LENGTH: Final = 500  # question chars
MAX_TOKEN_LENGTH: Final = 256
MAX_SESSION_ID_LENGTH: Final = 64

# Per-IP rate limit for the public QR endpoint.
QR_RATE_LIMIT_REQUESTS: Final = 30
QR_RATE_LIMIT_WINDOW: Final = 60.0

# Player WS join timeout — drop idle sockets that never join.
PLAYER_WS_JOIN_TIMEOUT: Final = 30.0
PLAYER_WS_STRICT_ORIGIN: Final = True

# How long we'll wait for an LLM round trip before giving up.
LLM_TIMEOUT_STORY: Final = 90.0   # generation can be long
LLM_TIMEOUT_NARRATION: Final = 30.0
LLM_TIMEOUT_NPC: Final = 30.0

# Rate-limit interrogations per (player, suspect) pair — the LLM is the
# bottleneck. Players can ask a max of this many questions per minute.
INTERROGATION_RATE: Final = 8
INTERROGATION_WINDOW: Final = 60.0

# Config keys
CONF_DEFAULT_AGENT: Final = "default_agent"

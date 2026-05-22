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
WS_TYPE_LIST_LIGHTS: Final = "mortify/lights/list"

# Player WS message type (defined here for documentation; handled in
# http_views): "killer_respond" — the human killer picks an evasion reply.

# --- Event types (server -> client, in subscriptions) ----------------------
EVENT_GAME_UPDATED: Final = "game_updated"
EVENT_GAME_ENDED: Final = "game_ended"
EVENT_PLAYER_JOINED: Final = "player_joined"
EVENT_PLAYER_LEFT: Final = "player_left"
EVENT_ACT_STARTED: Final = "act_started"
EVENT_NARRATION: Final = "narration"
EVENT_CLUE_DISCOVERED: Final = "clue_discovered"
EVENT_INTERROGATION_REPLY: Final = "interrogation_reply"
EVENT_CLUE_UNLOCKED: Final = "clue_unlocked"
EVENT_CONFRONT_RESOLVED: Final = "confront_resolved"
EVENT_ACCUSATION_SUBMITTED: Final = "accusation_submitted"
EVENT_REVEALED: Final = "revealed"
EVENT_GENERATING: Final = "generating"
# Fired when the investigation auto-advances because enough of the case has
# come to light (clue-driven act progression). Carries the new act + a
# dramatic reason line so everyone sees why the story moved on.
EVENT_ACT_AUTO_ADVANCED: Final = "act_auto_advanced"
# Sent privately to the human killer when another player questions them and
# the killer must choose how to deflect. Carries the question + options.
EVENT_KILLER_PROMPT: Final = "killer_prompt"
# Sent privately to the asker while they wait on a human killer to respond.
EVENT_AWAITING_KILLER: Final = "awaiting_killer"

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
# Bonus when the accuser correctly accused the killer AND holds at least one
# clue that genuinely implicates them (i.e. they investigated, not guessed).
POINTS_EVIDENCE_BONUS: Final = 250
# Awarded when a player asks a suspect a question the LLM judges to be
# genuinely relevant to the murder (on-topic interrogation). Small, so it
# rewards good questioning without dwarfing accusation/clue points.
POINTS_ON_TOPIC_QUESTION: Final = 20
# A player gets a single high-stakes "confront" per game. Backed by a
# contradiction clue against the real killer it pays out big; baseless it
# costs points.
POINTS_CONFRONT_SUCCESS: Final = 300
POINTS_CONFRONT_WASTED: Final = -100
# Cap how many on-topic question bonuses a single player can bank, so a
# player can't farm points by asking the same good question repeatedly.
MAX_SCORED_QUESTIONS_PER_PLAYER: Final = 15

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

# --- Clue-driven automatic act progression ---------------------------------
# The host can still "Advance Act" manually, but the story also moves itself
# forward as players collectively uncover the case. We advance when the
# fraction of all clues unlocked (by anyone) crosses these thresholds.
#   act_1 -> act_2 when this fraction of clues is out
#   act_2 -> act_3 when this fraction is out
#   act_3 -> accusation when this fraction is out
# Tuned so a 12-clue case typically moves on around the 3rd / 7th / 10th clue.
AUTO_ADVANCE_THRESHOLDS: Final = {
    STATE_ACT_1: 0.25,   # ~3 of 12
    STATE_ACT_2: 0.55,   # ~7 of 12
    STATE_ACT_3: 0.85,   # ~10 of 12
}
# Never auto-advance into the accusation phase — naming the killer should be
# a deliberate act the host or players trigger. So the act_3 threshold above
# is only used to *suggest* the finale (we still advance to it automatically,
# but the host retains the manual reveal control).
AUTO_ADVANCE_ENABLED: Final = True
# Don't auto-advance more often than this many seconds, so a flurry of
# simultaneous unlocks can't skip two acts at once.
AUTO_ADVANCE_MIN_INTERVAL: Final = 8.0

# --- Human killer ----------------------------------------------------------
# When at least this many players are in the lobby at start, the killer role
# is given to one of the human players (the others, and any NPC suspects, are
# innocent). Below this, the killer may be an NPC so a 2-player game still
# works. The human killer is told they're guilty; everyone else only learns
# at the reveal.
MIN_PLAYERS_FOR_HUMAN_KILLER: Final = 3
# How long the asker waits for a human killer to choose a reply before we
# fall back to an auto-selected evasion (keeps the game moving if the killer
# is slow or has put their phone down).
KILLER_RESPONSE_TIMEOUT: Final = 25.0
# Number of pre-generated evasion options the human killer chooses between.
KILLER_OPTION_COUNT: Final = 3

# --- Dramatic lighting -----------------------------------------------------
# Mortify can pulse the host's real lights on act transitions and big beats
# (clue reveals, confrontations, the final accusation). Colours are warm,
# theatrical, and chosen to read on coloured (Hue) and white-only bulbs alike
# — white-only bulbs ignore rgb_color and just take the brightness/kelvin.
LIGHTS_ENABLED_DEFAULT: Final = True
# Per-act mood: (rgb_color, brightness_pct, color_temp_kelvin).
# rgb is used by colour bulbs; kelvin is the fallback for tunable-white bulbs.
ACT_LIGHT_MOODS: Final = {
    STATE_ACT_1: {"rgb_color": [80, 60, 120], "brightness_pct": 45, "kelvin": 2700},
    STATE_ACT_2: {"rgb_color": [120, 50, 50], "brightness_pct": 40, "kelvin": 2500},
    STATE_ACT_3: {"rgb_color": [150, 30, 30], "brightness_pct": 35, "kelvin": 2200},
    STATE_ACCUSATION: {"rgb_color": [180, 20, 20], "brightness_pct": 30, "kelvin": 2000},
    STATE_REVEAL: {"rgb_color": [200, 160, 60], "brightness_pct": 70, "kelvin": 3000},
}
# A short red "flash" used on confrontations and clue reveals.
LIGHT_FLASH_RGB: Final = [200, 30, 30]
LIGHT_TRANSITION_S: Final = 2  # smooth fade for mood changes

# --- TTS narration speed ---------------------------------------------------
# Some TTS engines talk too fast. We pass these through to tts.speak via the
# "options" dict where supported (Piper/Google/Cloud accept a speed/rate).
# 1.0 is normal; lower is slower. The host picks one of these in setup.
TTS_SPEEDS: Final = {
    "slow": 0.8,
    "normal": 1.0,
    "fast": 1.15,
}
TTS_SPEED_DEFAULT: Final = "normal"

# Config keys
CONF_DEFAULT_AGENT: Final = "default_agent"

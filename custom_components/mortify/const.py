"""Constants for Mortify."""

DOMAIN = "mortify"

# Config keys
CONF_LLM_URL = "llm_url"
CONF_LLM_MODEL = "llm_model"
CONF_TTS_ENTITY = "tts_entity"
CONF_HA_URL = "ha_url"

# Defaults
DEFAULT_LLM_URL = "http://localhost:11434"
DEFAULT_LLM_MODEL = "llama3.2"
DEFAULT_PORT = 8123

# Game states
GAME_STATE_LOBBY = "lobby"
GAME_STATE_BRIEFING = "briefing"
GAME_STATE_INVESTIGATION = "investigation"
GAME_STATE_ACCUSATION = "accusation"
GAME_STATE_REVEAL = "reveal"
GAME_STATE_ENDED = "ended"

# Music tracks (relative paths for local playback via media_player)
MUSIC_LOBBY = "mortify/lobby_ambience.mp3"
MUSIC_BRIEFING = "mortify/dark_intro.mp3"
MUSIC_INVESTIGATION = "mortify/investigation_theme.mp3"
MUSIC_TENSION = "mortify/tension_rising.mp3"
MUSIC_REVEAL = "mortify/dramatic_reveal.mp3"
MUSIC_WINNER = "mortify/winner_fanfare.mp3"

# Suspect roles
SUSPECT_ROLES = [
    {"id": "butler", "name": "The Butler", "emoji": "🎩", "description": "Impeccably proper, suspiciously loyal"},
    {"id": "doctor", "name": "The Doctor", "emoji": "⚕️", "description": "Calm under pressure, knows too much about poisons"},
    {"id": "artist", "name": "The Artist", "emoji": "🎨", "description": "Passionate, volatile, deeply in debt"},
    {"id": "lawyer", "name": "The Lawyer", "emoji": "⚖️", "description": "Silver-tongued, hides behind contracts"},
    {"id": "cook", "name": "The Cook", "emoji": "🍳", "description": "Last to see the victim alive. Or so they claim."},
    {"id": "gardener", "name": "The Gardener", "emoji": "🌿", "description": "Quiet, watchful, knows every corner of the estate"},
    {"id": "journalist", "name": "The Journalist", "emoji": "📰", "description": "Digging for a story — or burying one"},
    {"id": "widow", "name": "The Widow", "emoji": "🖤", "description": "Grief-stricken. Stands to inherit everything."},
]

# Weapons
WEAPONS = [
    "a blunt instrument", "poison in the evening drink", "a loose stair rail",
    "a gas leak in the study", "a malfunctioning smart lock", "a tampered smoke detector",
    "a severed ethernet cable (electrocution)", "an allergic reaction, arranged",
]

# WebSocket commands
WS_TYPE_JOIN = "mortify/join"
WS_TYPE_INTERROGATE = "mortify/interrogate"
WS_TYPE_SUBMIT_CLUE = "mortify/submit_clue"
WS_TYPE_ACCUSE = "mortify/accuse"
WS_TYPE_GET_STATE = "mortify/get_state"
WS_TYPE_ADMIN_START = "mortify/admin/start"
WS_TYPE_ADMIN_NEXT_ACT = "mortify/admin/next_act"
WS_TYPE_ADMIN_CANCEL = "mortify/admin/cancel"
WS_TYPE_ADMIN_GET_ENTITIES = "mortify/admin/get_entities"
WS_TYPE_ADMIN_GET_SPEAKERS = "mortify/admin/get_speakers"

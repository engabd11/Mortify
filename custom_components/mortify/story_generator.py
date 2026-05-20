"""Story generation for Mortify.

The LLM does the creative heavy lifting; this module is the structured
contract around it.

* ``generate_mystery``: ask the LLM for a full mystery (victim, suspects,
  alibis, clues, acts, reveal) as JSON, validate it, and fall back to a
  deterministic stock story if the LLM refuses or produces garbage.
* ``generate_npc_reply``: in-character interrogation response.
* ``generate_act_narration``: enhance the LLM-written narration with a
  live home event when one is available.

Why the validation? LLMs lie about JSON. We must NEVER pass partial /
malformed data through to the game loop, because downstream code assumes
``story["suspects"]`` is a non-empty list with all the expected keys.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from typing import Any

from .const import (
    LLM_TIMEOUT_NARRATION,
    LLM_TIMEOUT_NPC,
    LLM_TIMEOUT_STORY,
    SUSPECT_ROLES,
    WEAPONS,
)
from .llm_client import LLMClient, LLMError, LLMTimeoutError

_LOGGER = logging.getLogger(__name__)

GENERATION_SYSTEM = (
    "You are a murder mystery game master writing an interactive mystery "
    "set inside a smart home. You write atmospheric, clever, darkly "
    "humorous content. Keep descriptions vivid but concise. When asked "
    "for structured data, respond with RAW JSON only — no markdown "
    "fences, no commentary, no preamble."
)

# Required top-level story keys we'll consume downstream. Anything missing
# means we fall through to the deterministic fallback story.
_REQUIRED_STORY_KEYS = {
    "title", "victim_name", "crime_scene", "weapon",
    "killer_id", "opening_narration", "motive",
    "suspects", "acts", "reveal_narration",
}

# Required per-suspect keys.
_REQUIRED_SUSPECT_KEYS = {
    "role_id", "alibi", "secret", "npc_persona", "real_clue", "room_clue",
}


# --- public API -------------------------------------------------------------

async def generate_mystery(
    llm: LLMClient,
    rooms: list[str],
    entities: list[dict[str, Any]],
    player_names: list[str],
    suspect_count: int = 4,
) -> dict[str, Any]:
    """Generate a full mystery, seeded with the host's real home data.

    Falls back to a deterministic stock story if the LLM is unavailable
    or returns unusable JSON. Either way the return value is the same
    shape so the game loop never has to special-case the fallback.
    """
    # Cap how many roles we'll assign — we want at minimum 3 suspects so
    # a guess is non-trivial, and at most the catalogue size.
    suspect_count = max(3, min(suspect_count, len(SUSPECT_ROLES)))

    chosen_roles = random.sample(SUSPECT_ROLES, suspect_count)
    weapon = random.choice(WEAPONS)
    crime_room = random.choice(rooms) if rooms else "the study"
    killer_role = random.choice(chosen_roles)

    # Build cast assignments BEFORE asking the LLM, so we have an
    # authoritative truth table for who plays whom even if the LLM
    # forgets to echo it back. The LLM gets told who the killer is —
    # this is intentional, it needs to weave clues toward them.
    cast = _build_cast(chosen_roles, killer_role, player_names)

    entity_lines = _format_entity_lines(entities[:20])
    rooms_str = ", ".join(rooms) if rooms else "Living Room, Kitchen, Bedroom, Study"
    suspects_text = "\n".join(
        f"- {c['role_name']} (played by {c['player']})"
        f"{' ← THE KILLER' if c['is_killer'] else ''}"
        for c in cast
    )
    suspect_schema = _suspect_schema_lines(chosen_roles)

    prompt = f"""Generate a murder mystery for a smart home party game.

HOME DATA:
Rooms: {rooms_str}
Smart devices and sensors:
{entity_lines}

CAST (use these exact role_id values in your output):
{suspects_text}

WEAPON: {weapon}
CRIME SCENE: {crime_room}

Respond with raw JSON matching this exact structure:
{{
  "title": "The [Adjective] [Noun] of [Place/Name]",
  "victim_name": "a dramatic victim name (NOT a player name)",
  "victim_description": "one sentence about the victim",
  "crime_scene": "{crime_room}",
  "weapon": "{weapon}",
  "killer_id": "{killer_role['id']}",
  "time_of_death": "a specific time tonight e.g. 10:47 PM",
  "opening_narration": "3-4 sentence atmospheric opening read aloud by the narrator. Second person, dark, theatrical. Reference real rooms and devices.",
  "motive": "the killer's secret motive in 2 sentences",
  "suspects": [
{suspect_schema}
  ],
  "acts": [
    {{"act": 1, "title": "The Discovery",     "narration": "2-3 sentences. The body is found. Atmospheric. Read aloud."}},
    {{"act": 2, "title": "Gathering Shadows", "narration": "2-3 sentences. Suspects are assembled. Tensions rise."}},
    {{"act": 3, "title": "Hidden Truths",     "narration": "2-3 sentences. Key evidence surfaces. Someone is lying."}},
    {{"act": 4, "title": "The Accusation",    "narration": "1-2 sentences. Time to name the killer."}}
  ],
  "reveal_narration": "4-5 sentence dramatic reveal. Explain how the murder happened, the motive, and how clues pointed to the killer. Cinematic and satisfying.",
  "red_herrings": ["short description of red herring 1", "short description of red herring 2"]
}}"""

    raw = ""
    parsed: dict[str, Any] | None = None
    try:
        raw = await llm.complete(GENERATION_SYSTEM, prompt, timeout=LLM_TIMEOUT_STORY)
        parsed = _parse_story_json(raw)
    except LLMTimeoutError:
        _LOGGER.warning("Mystery generation timed out; using fallback story")
    except LLMError as err:
        _LOGGER.warning("Mystery generation failed (%s); using fallback story", err)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Mystery generation crashed; using fallback story")

    if parsed is None or not _validate_story(parsed, chosen_roles, killer_role):
        if raw:
            _LOGGER.debug("LLM raw output (truncated): %s", raw[:500])
        parsed = _fallback_story(chosen_roles, killer_role, crime_room, weapon, rooms)

    # Stamp authoritative cast info onto each suspect. We trust the LLM for
    # creative fields (alibi, secret, persona, clues), but role_name, emoji,
    # player assignment and is_killer come from OUR cast table.
    _enrich_suspects(parsed, cast)

    parsed["_cast"] = cast
    parsed["generated_at"] = datetime.now(tz=None).isoformat(timespec="seconds")
    return parsed


async def generate_npc_reply(
    llm: LLMClient,
    suspect: dict[str, Any],
    story: dict[str, Any],
    history: list[dict[str, str]],
    question: str,
    is_killer: bool,
) -> str:
    """Generate an in-character interrogation reply.

    On failure (timeout, LLM error, empty response) we return a generic
    in-character deflection so the player never sees a raw error.
    """
    system = f"""You are playing {suspect.get('role_name', 'a suspect')} in a murder mystery.

CHARACTER NOTES: {suspect.get('npc_persona', 'Nervous, evasive.')}
YOUR ALIBI (stick to this): {suspect.get('alibi', 'I was alone all evening.')}
YOUR SECRET (hide this unless pressed very hard): {suspect.get('secret', 'Nothing.')}
{'YOU ARE THE KILLER. Deflect cleverly. NEVER confess directly.' if is_killer else 'YOU ARE INNOCENT but hiding your secret.'}
THE MURDER: victim {story.get('victim_name', 'the victim')} killed with {story.get('weapon', 'an unknown weapon')} in {story.get('crime_scene', 'the house')} at {story.get('time_of_death', 'late evening')}.

Rules:
- Stay in character ALWAYS. Never break the fourth wall.
- Be dramatic, theatrical, slightly Victorian in speech.
- Reply in 2-4 sentences. NEVER monologue.
- If innocent: you may accidentally hint at your secret if pressed 3+ times on the same topic.
- If guilty: you are a skilled liar. Plant subtle doubt on others."""

    try:
        return await llm.chat(
            system=system,
            history=history,
            user_message=question,
            timeout=LLM_TIMEOUT_NPC,
            user_label="Detective",
            assistant_label=suspect.get("role_name", "Suspect"),
        )
    except LLMTimeoutError:
        _LOGGER.info("NPC reply timed out for %s", suspect.get("role_id"))
        return _stock_npc_deflection(is_killer)
    except LLMError as err:
        _LOGGER.info("NPC reply failed for %s: %s", suspect.get("role_id"), err)
        return _stock_npc_deflection(is_killer)


async def generate_act_narration(
    llm: LLMClient,
    story: dict[str, Any],
    act_num: int,
    home_event: str | None = None,
) -> str:
    """Return the act's narration, optionally enhanced with a live home event.

    If no home event is available, the canned narration from the story is
    returned as-is — no LLM round trip needed.
    """
    base = ""
    for act in story.get("acts", []):
        if act.get("act") == act_num:
            base = act.get("narration", "")
            break
    base = base or f"The investigation continues. The truth feels closer."

    if not home_event:
        return base

    prompt = (
        f"Rewrite this murder mystery narration to naturally weave in a real "
        f"event from the smart home. Keep it under 3 sentences and maintain "
        f"the dramatic tone.\n\n"
        f"Original: \"{base}\"\n"
        f"Real home event: \"{home_event}\"\n\n"
        f"Return ONLY the rewritten narration, no quotes, no commentary."
    )
    try:
        enhanced = await llm.complete(
            GENERATION_SYSTEM, prompt, timeout=LLM_TIMEOUT_NARRATION,
        )
        # Defensive: if the LLM ignored "no quotes", strip surrounding ones.
        enhanced = enhanced.strip().strip('"').strip("'")
        return enhanced or base
    except (LLMTimeoutError, LLMError) as err:
        _LOGGER.debug("Narration enhancement failed (%s); using base", err)
        return base


# --- helpers ----------------------------------------------------------------

def _build_cast(
    roles: list[dict[str, Any]],
    killer_role: dict[str, Any],
    player_names: list[str],
) -> list[dict[str, Any]]:
    """Pair each role with a player (or NPC if more roles than players)."""
    cast: list[dict[str, Any]] = []
    for i, role in enumerate(roles):
        player = player_names[i] if i < len(player_names) else None
        cast.append({
            "role_id": role["id"],
            "role_name": role["name"],
            "role_emoji": role["emoji"],
            "role_description": role["description"],
            "player": player,           # None means NPC
            "is_killer": role["id"] == killer_role["id"],
        })
    return cast


def _format_entity_lines(entities: list[dict[str, Any]]) -> str:
    if not entities:
        return "- No sensors available"
    return "\n".join(
        f"- {e.get('name', e.get('entity_id', '?'))} ({e.get('domain', '?')}) "
        f"in {e.get('area', 'unknown room')}: state={e.get('state', '?')}"
        for e in entities
    )


def _suspect_schema_lines(roles: list[dict[str, Any]]) -> str:
    """Render the JSON schema block for suspects, one per requested role.

    We inline the role_id values so the LLM is more likely to echo them
    correctly (rather than inventing new ones).
    """
    items = []
    for r in roles:
        items.append(
            "    {\n"
            f'      "role_id": "{r["id"]}",\n'
            '      "alibi": "their claimed alibi for the time of death — plausible but with one flaw",\n'
            '      "secret": "a secret they\'re hiding (not the murder) that makes them look suspicious",\n'
            '      "npc_persona": "2-sentence acting note for the AI playing this character",\n'
            '      "real_clue": "one genuine clue. If this suspect IS the killer, the clue points to them. Otherwise it is a red herring.",\n'
            '      "room_clue": "a physical clue hidden somewhere in the house, anchored to one of the rooms or a device"\n'
            "    }"
        )
    return ",\n".join(items)


def _parse_story_json(raw: str) -> dict[str, Any] | None:
    """Parse raw LLM output as JSON, tolerating common malformations.

    LLMs often wrap JSON in ```json fences, append commentary, or emit a
    trailing comma. We try a few cleanups before giving up.
    """
    raw = raw.strip()
    # Strip markdown fence
    if raw.startswith("```"):
        # Drop opening fence and optional "json" tag
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        # Drop trailing fence
        raw = re.sub(r"\s*```\s*$", "", raw)
        raw = raw.strip()
    # First try as-is
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try extracting the first {...} block — handles models that prefix
    # the JSON with prose like "Here is your mystery: {...}".
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _validate_story(
    story: dict[str, Any],
    roles: list[dict[str, Any]],
    killer_role: dict[str, Any],
) -> bool:
    """Reject obviously bad LLM output.

    We check shape (required keys, suspect count, killer_id matches a known
    role) before letting the story through. If anything fails, the caller
    falls back to the deterministic stock story.
    """
    if not isinstance(story, dict):
        return False
    missing = _REQUIRED_STORY_KEYS - set(story.keys())
    if missing:
        _LOGGER.debug("Story missing keys: %s", missing)
        return False
    suspects = story.get("suspects")
    if not isinstance(suspects, list) or len(suspects) != len(roles):
        _LOGGER.debug(
            "Suspect count mismatch (got %s, want %d)",
            "n/a" if not isinstance(suspects, list) else len(suspects),
            len(roles),
        )
        return False
    role_ids = {r["id"] for r in roles}
    seen_ids: set[str] = set()
    for s in suspects:
        if not isinstance(s, dict):
            return False
        if _REQUIRED_SUSPECT_KEYS - set(s.keys()):
            return False
        rid = s.get("role_id")
        if rid not in role_ids:
            _LOGGER.debug("Suspect has unknown role_id %r", rid)
            return False
        if rid in seen_ids:
            return False  # duplicate role
        seen_ids.add(rid)
    if story.get("killer_id") != killer_role["id"]:
        _LOGGER.debug(
            "killer_id mismatch (got %r, want %r)",
            story.get("killer_id"), killer_role["id"],
        )
        return False
    acts = story.get("acts")
    if not isinstance(acts, list) or len(acts) < 4:
        return False
    return True


def _enrich_suspects(
    story: dict[str, Any],
    cast: list[dict[str, Any]],
) -> None:
    """Stamp authoritative cast info onto each suspect entry."""
    cast_by_id = {c["role_id"]: c for c in cast}
    for s in story.get("suspects", []):
        meta = cast_by_id.get(s.get("role_id"))
        if not meta:
            continue
        s["role_name"] = meta["role_name"]
        s["role_emoji"] = meta["role_emoji"]
        s["role_description"] = meta["role_description"]
        s["player"] = meta["player"]
        s["is_killer"] = meta["is_killer"]


def _fallback_story(
    roles: list[dict[str, Any]],
    killer_role: dict[str, Any],
    crime_room: str,
    weapon: str,
    rooms: list[str],
) -> dict[str, Any]:
    """Deterministic story used when the LLM is unavailable.

    The contents are intentionally generic — the LLM is meant to be the
    creative engine. This is just enough to keep the game playable so
    the user can confirm everything else is wired up.
    """
    pool_rooms = rooms or ["the hallway", "the kitchen", "the study"]
    suspects: list[dict[str, Any]] = []
    for r in roles:
        is_killer = r["id"] == killer_role["id"]
        suspects.append({
            "role_id": r["id"],
            "alibi": (
                f"I was in {random.choice(pool_rooms)} reading the whole evening. "
                f"Anyone could vouch for me. Probably."
            ),
            "secret": (
                "I had been quietly meeting with the victim earlier this week "
                "to discuss money."
            ),
            "npc_persona": (
                "Nervous, defensive, speaks quickly. Eyes dart to the door."
            ),
            "real_clue": (
                "A glove was found near the scene — and one of mine is missing."
                if is_killer else
                "A torn receipt with the wrong date. Suspicious until you check the calendar."
            ),
            "room_clue": (
                f"A crumpled note tucked under the lamp in {random.choice(pool_rooms)}."
            ),
        })
    return {
        "title": "The Silent House",
        "victim_name": "Lord Edmund Blackwell",
        "victim_description": "A wealthy recluse with many enemies.",
        "crime_scene": crime_room,
        "weapon": weapon,
        "killer_id": killer_role["id"],
        "time_of_death": "11:15 PM",
        "opening_narration": (
            f"The night began like any other. Then the lights in {crime_room} "
            f"flickered, and went dark. When they came back on, Lord Blackwell "
            f"was dead. You are all in this house. None of you may leave."
        ),
        "motive": (
            "A bitter inheritance dispute, decades in the making. The killer "
            "stood to lose everything tonight."
        ),
        "suspects": suspects,
        "acts": [
            {"act": 1, "title": "The Discovery",
             "narration": "A terrible deed has been done. The victim lies still."},
            {"act": 2, "title": "Gathering Shadows",
             "narration": "The suspects are assembled. No one may leave."},
            {"act": 3, "title": "Hidden Truths",
             "narration": "Evidence mounts. Someone in this room is lying."},
            {"act": 4, "title": "The Accusation",
             "narration": "The time has come. Name the killer."},
        ],
        "reveal_narration": (
            "The truth, at last. The killer's nerve broke when the clues "
            "lined up — the missing glove, the timing, the motive that had "
            "been hiding in plain sight. Justice, however imperfect, is served."
        ),
        "red_herrings": [
            "A suspicious stain that turned out to be wine",
            "A torn page from the victim's diary",
        ],
    }


_STOCK_DEFLECTIONS_INNOCENT = [
    "I... I have already told you everything. Why must you press me so?",
    "You ask the wrong questions, Detective. Look elsewhere.",
    "I'd really rather not speak of it. Not here. Not now.",
    "Forgive me — I find this entire evening unbearable. I have nothing to add.",
]
_STOCK_DEFLECTIONS_GUILTY = [
    "An interesting theory, Detective. Have you considered the Butler? No? You should.",
    "I assure you, my conscience is perfectly clear. Yours, perhaps, less so.",
    "I find it telling that you ask ME and not the others. Most telling.",
    "If you must know — I was nowhere near the scene. Ask anyone. Ask everyone.",
]


def _stock_npc_deflection(is_killer: bool) -> str:
    pool = _STOCK_DEFLECTIONS_GUILTY if is_killer else _STOCK_DEFLECTIONS_INNOCENT
    return random.choice(pool)

"""Story generation for Mortify using local LLM."""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from typing import Any

from .llm_client import LocalLLMClient
from .const import SUSPECT_ROLES, WEAPONS

_LOGGER = logging.getLogger(__name__)

GENERATION_SYSTEM = """You are a murder mystery game master writing an interactive mystery set in a smart home.
You write atmospheric, clever, darkly humorous content. Keep descriptions vivid but concise.
Always respond with valid JSON when asked for structured data. No markdown fences, just raw JSON."""


async def generate_mystery(
    llm: LocalLLMClient,
    rooms: list[str],
    entities: list[dict],
    player_names: list[str],
    suspect_count: int = 4,
) -> dict[str, Any]:
    """Generate a complete murder mystery from the home's real data."""

    # Pick suspects — assign players + NPCs
    shuffled_roles = random.sample(SUSPECT_ROLES, min(suspect_count, len(SUSPECT_ROLES)))
    weapon = random.choice(WEAPONS)
    killer_role = random.choice(shuffled_roles)
    crime_room = random.choice(rooms) if rooms else "the study"

    # Build entity context for the LLM
    entity_lines = []
    for e in entities[:20]:  # cap to keep prompt small
        entity_lines.append(f"- {e['name']} ({e['domain']}) in {e.get('area', 'unknown room')}: state={e['state']}")
    entity_context = "\n".join(entity_lines) if entity_lines else "- No sensors available"

    # Format player/suspect assignments
    suspect_assignments = []
    for i, role in enumerate(shuffled_roles):
        player = player_names[i] if i < len(player_names) else f"Guest {i+1}"
        suspect_assignments.append({"role": role, "player": player, "is_killer": role["id"] == killer_role["id"]})

    suspects_text = "\n".join(
        f"- {s['role']['name']} (played by {s['player']}{'  ← THE KILLER' if s['is_killer'] else ''})"
        for s in suspect_assignments
    )

    prompt = f"""Generate a murder mystery for a smart home party game. 

HOME DATA:
Rooms: {', '.join(rooms) if rooms else 'Living Room, Kitchen, Bedroom, Study'}
Smart devices and sensors:
{entity_context}

CAST:
{suspects_text}

WEAPON: {weapon}
CRIME SCENE: {crime_room}

Generate a mystery with this JSON structure (respond ONLY with JSON):
{{
  "title": "The [Adjective] [Noun] of [Place/Name]",
  "victim_name": "string — a dramatic victim name (not a player)",
  "victim_description": "one sentence about the victim",
  "crime_scene": "{crime_room}",
  "weapon": "{weapon}",
  "killer_id": "{killer_role['id']}",
  "time_of_death": "a specific time tonight e.g. 10:47 PM",
  "opening_narration": "3-4 sentence atmospheric opening read aloud by narrator. Second person, dark, theatrical. Reference real rooms and devices.",
  "motive": "the killer's secret motive in 2 sentences",
  "suspects": [
    {{
      "role_id": "role id from cast",
      "alibi": "their claimed alibi for the time of death — plausible but with one flaw",
      "secret": "a secret they're hiding (not the murder) that makes them look suspicious",
      "npc_persona": "2-sentence acting note for the AI when playing this character in interrogation",
      "real_clue": "one genuine clue that points to the killer if this suspect IS the killer, else a red herring",
      "room_clue": "a physical clue hidden in one of the home's rooms or found on a device"
    }}
  ],
  "acts": [
    {{
      "act": 1,
      "title": "The Discovery",
      "narration": "2-3 sentences. The body is found. Atmospheric. Read aloud.",
      "music_mood": "eerie"
    }},
    {{
      "act": 2, 
      "title": "Gathering Shadows",
      "narration": "2-3 sentences. Suspects are assembled. Tensions rise.",
      "music_mood": "tense"
    }},
    {{
      "act": 3,
      "title": "Hidden Truths",
      "narration": "2-3 sentences. Key evidence surfaces. Someone is lying.",
      "music_mood": "urgent"
    }},
    {{
      "act": 4,
      "title": "The Accusation",
      "narration": "1-2 sentences. Time to name the killer.",
      "music_mood": "dramatic"
    }}
  ],
  "reveal_narration": "4-5 sentence dramatic reveal. Explain how the murder happened, the motive, and how the clues pointed to the killer. Cinematic and satisfying.",
  "red_herrings": ["short description of red herring 1", "short description of red herring 2"]
}}"""

    _LOGGER.debug("Generating mystery story with LLM")
    raw = await llm.complete(GENERATION_SYSTEM, prompt, max_tokens=3000)

    # Parse JSON — strip any accidental markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        story = json.loads(raw)
    except json.JSONDecodeError as e:
        _LOGGER.error("Failed to parse mystery JSON: %s\nRaw: %s", e, raw[:500])
        story = _fallback_story(shuffled_roles, killer_role, crime_room, weapon, rooms, player_names)

    # Merge player assignments into story suspects
    for s_data in story.get("suspects", []):
        match = next((sa for sa in suspect_assignments if sa["role"]["id"] == s_data.get("role_id")), None)
        if match:
            s_data["player"] = match["player"]
            s_data["role_name"] = match["role"]["name"]
            s_data["role_emoji"] = match["role"]["emoji"]
            s_data["is_killer"] = match["is_killer"]

    story["_suspect_assignments"] = suspect_assignments
    story["generated_at"] = datetime.now().isoformat()

    return story


async def generate_npc_response(
    llm: LocalLLMClient,
    suspect_data: dict,
    story: dict,
    conversation_history: list[dict],
    player_question: str,
    is_killer: bool,
) -> str:
    """Generate an in-character NPC response for interrogation."""
    system = f"""You are playing {suspect_data['role_name']} in a murder mystery game.

YOUR CHARACTER: {suspect_data.get('npc_persona', 'Nervous and evasive')}
YOUR ALIBI (stick to this): {suspect_data.get('alibi', 'I was alone all evening')}
YOUR SECRET (you hide this unless pressed very hard): {suspect_data.get('secret', 'Nothing')}
ARE YOU THE KILLER: {'YES — you are guilty. Deflect cleverly but never confess directly.' if is_killer else 'No. You are innocent but hiding your secret.'}
THE MURDER: victim killed with {story.get('weapon','unknown means')} in {story.get('crime_scene','unknown room')} at {story.get('time_of_death','unknown time')}

Rules:
- Stay in character ALWAYS. Never break the fourth wall.
- Be dramatic, theatrical, slightly Victorian in speech.
- Give 2-4 sentence responses. Never monologue.
- If innocent: you may accidentally reveal your secret if pressed 3+ times on the same topic.
- If guilty: you are a skilled liar. Plant subtle doubt on others."""

    messages = conversation_history + [{"role": "user", "content": player_question}]
    return await llm.chat(messages, system=system, max_tokens=200)


async def generate_act_narration(
    llm: LocalLLMClient,
    story: dict,
    act_num: int,
    home_event: str | None = None,
) -> str:
    """Generate or enhance act narration, optionally weaving in a live home event."""
    base = ""
    for act in story.get("acts", []):
        if act["act"] == act_num:
            base = act["narration"]
            break

    if not home_event:
        return base

    # Weave the live event into the narration
    prompt = f"""Take this murder mystery narration and naturally weave in a real event that just happened in the smart home. Keep it under 3 sentences total and maintain the dramatic tone.

Original narration: "{base}"
Real home event to incorporate: "{home_event}"

Return ONLY the enhanced narration text, no quotes."""

    enhanced = await llm.complete(GENERATION_SYSTEM, prompt, max_tokens=150)
    return enhanced if enhanced else base


def _fallback_story(
    roles: list, killer_role: dict, crime_room: str, weapon: str, rooms: list, players: list
) -> dict:
    """Minimal fallback story if LLM fails."""
    return {
        "title": "The Mystery of the Silent House",
        "victim_name": "Lord Edmund Blackwell",
        "victim_description": "A wealthy recluse with many enemies",
        "crime_scene": crime_room,
        "weapon": weapon,
        "killer_id": killer_role["id"],
        "time_of_death": "11:15 PM",
        "opening_narration": f"The night began like any other. Then the lights in the {crime_room} went dark. When they came back on — Lord Blackwell was dead. You are all suspects.",
        "motive": "A bitter inheritance dispute. The killer stood to lose everything.",
        "suspects": [
            {
                "role_id": r["id"],
                "alibi": f"I was in the {random.choice(rooms) if rooms else 'kitchen'} the whole time",
                "secret": "I had been secretly meeting with the victim earlier",
                "npc_persona": "Nervous, defensive, speaks quickly",
                "real_clue": "A discarded glove near the scene",
                "room_clue": f"A crumpled note found in the {random.choice(rooms) if rooms else 'hallway'}",
                "player": players[i] if i < len(players) else f"Guest {i+1}",
                "role_name": r["name"],
                "role_emoji": r["emoji"],
                "is_killer": r["id"] == killer_role["id"],
            }
            for i, r in enumerate(roles)
        ],
        "acts": [
            {"act": 1, "title": "The Discovery", "narration": "A terrible deed has been done. The victim lies still.", "music_mood": "eerie"},
            {"act": 2, "title": "Gathering Shadows", "narration": "The suspects are assembled. No one may leave.", "music_mood": "tense"},
            {"act": 3, "title": "Hidden Truths", "narration": "Evidence mounts. Someone in this room is lying.", "music_mood": "urgent"},
            {"act": 4, "title": "The Accusation", "narration": "The time has come. Name the killer.", "music_mood": "dramatic"},
        ],
        "reveal_narration": "The truth is finally revealed. Justice, however imperfect, is served.",
        "red_herrings": ["A suspicious stain that turned out to be wine", "A torn page from a diary"],
    }

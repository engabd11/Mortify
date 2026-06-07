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

# ---------------------------------------------------------------------------
# Story-variety pools.
#
# The single fixed prompt the original used produced same-feeling mysteries
# (always a dinner party, always inheritance). We now sample a "scenario seed"
# — a setting, era, occasion, structural twist, and motive family — and inject
# it into the prompt. This pushes the LLM toward genuinely different stories
# run-to-run without needing a bigger model, and gives the deterministic
# fallback real variety too.
# ---------------------------------------------------------------------------

SCENARIO_SETTINGS: list[str] = [
    "a fog-bound country manor cut off by a storm",
    "a luxury yacht anchored offshore with no way back to land",
    "a snowed-in ski chalet high in the mountains",
    "a sleek modern smart-home penthouse during a citywide blackout",
    "a remote desert observatory the night of a rare eclipse",
    "a restored Art Deco hotel hosting a reunion",
    "a vineyard estate at the close of harvest season",
    "a private island research station during a comms outage",
    "a grand old theatre after the final curtain of a sold-out run",
    "a long-distance sleeper train crossing the night",
]

SCENARIO_OCCASIONS: list[str] = [
    "a milestone birthday celebration",
    "the reading of a contested will",
    "a high-stakes business merger dinner",
    "a wedding rehearsal the night before the ceremony",
    "a reunion of estranged old friends",
    "an exclusive charity gala",
    "the unveiling of a priceless acquisition",
    "a retirement send-off for a feared patriarch",
    "a séance arranged by the host as a parlour game",
    "an awards night for a tight-knit profession",
]

SCENARIO_ERAS: list[str] = [
    "the present day, dripping with quiet money",
    "the roaring 1920s",
    "a noir-tinged 1940s",
    "the glamorous, paranoid 1960s",
    "a near-future where the house itself watches everyone",
]

# Structural twists nudge the SHAPE of the mystery so the deduction feels
# fresh even when the cast archetypes repeat.
SCENARIO_TWISTS: list[str] = [
    "The victim knew they were in danger and left a half-finished warning.",
    "Two suspects are secretly protecting each other with a shared false alibi.",
    "The murder was meant for someone else — the victim died by mistake.",
    "A theft and the murder are tangled together; one hides the other.",
    "The killer and the victim were secretly the closest of allies.",
    "Everyone assumed an accident until one small detail refuses to fit.",
    "The victim had just discovered a long-buried secret about the killer.",
    "An old crime, thought closed, has come back to collect its debt.",
]

MOTIVE_FAMILIES: list[str] = [
    "a buried inheritance long promised and then revoked",
    "blackmail over a ruinous secret",
    "professional sabotage and stolen credit",
    "a love betrayed years ago and never forgiven",
    "covering up an earlier death the killer caused",
    "desperate, mounting debt the victim was about to expose",
    "revenge for a sibling or child the victim destroyed",
    "fear of being replaced, ousted, or written out for good",
]


def _build_scenario_seed(rooms: list[str]) -> dict[str, str]:
    """Sample one coherent scenario seed to flavour a unique mystery."""
    return {
        "setting": random.choice(SCENARIO_SETTINGS),
        "occasion": random.choice(SCENARIO_OCCASIONS),
        "era": random.choice(SCENARIO_ERAS),
        "twist": random.choice(SCENARIO_TWISTS),
        "motive_family": random.choice(MOTIVE_FAMILIES),
    }


# Required top-level story keys we'll consume downstream. Anything missing
# means we fall through to the deterministic fallback story.
_REQUIRED_STORY_KEYS = {
    "title", "victim_name", "crime_scene", "weapon",
    "killer_id", "opening_narration", "motive",
    "suspects", "acts", "reveal_narration", "clues",
}

# Required per-suspect keys.
_REQUIRED_SUSPECT_KEYS = {
    "role_id", "alibi", "secret", "npc_persona",
}

# Required per-clue keys. Clues now live in the story as first-class objects
# rather than being free-text strings hashed onto entities.
_REQUIRED_CLUE_KEYS = {
    "id", "title", "text", "held_by", "implicates",
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

    # Sample a unique scenario seed for genuine run-to-run variety.
    seed = _build_scenario_seed(rooms)

    # Build cast assignments BEFORE asking the LLM, so we have an
    # authoritative truth table for who plays whom even if the LLM
    # forgets to echo it back. The LLM gets told who the killer is —
    # this is intentional, it needs to weave clues toward them.
    cast = _build_cast(chosen_roles, killer_role, player_names)

    entity_lines = _format_entity_lines(entities[:20])
    rooms_str = ", ".join(rooms) if rooms else "the Drawing Room, the Kitchen, the Library, the Study"
    # NOTE: we still tell the LLM which role is the killer, but we do NOT tell
    # it which human plays which suspect — assignment is now decided after
    # generation and kept secret. So the cast list is anonymous archetypes.
    suspects_text = "\n".join(
        f"- {c['role_name']} (role_id: {c['role_id']})"
        f"{' ← THE KILLER' if c['is_killer'] else ''}"
        for c in cast
    )
    suspect_schema = _suspect_schema_lines(chosen_roles)
    role_id_list = ", ".join(r["id"] for r in chosen_roles)
    # Aim for ~2 clues per suspect so every character has something to show.
    clue_target = max(6, suspect_count * 2)

    prompt = f"""Generate a murder mystery for a party game. The mystery is entirely \
self-contained in the story you write — do NOT depend on real-world sensors. \
The smart-home devices below are OPTIONAL atmospheric flavour you may name in \
narration, nothing more.

SCENARIO TO BUILD ON (commit to these — they make this mystery unique):
- Setting: {seed['setting']}
- Occasion: {seed['occasion']}
- Era / tone: {seed['era']}
- Central twist to weave in: {seed['twist']}
- The killer's motive should grow from: {seed['motive_family']}
Lean into this scenario so the story feels specific, not generic. The title, \
victim, narration, and clues should clearly belong to THIS setting and occasion.

ATMOSPHERE (optional flavour only):
Rooms: {rooms_str}
Devices for ambience: {entity_lines}

CAST (use these exact role_id values: {role_id_list}):
{suspects_text}

WEAPON: {weapon}
CRIME SCENE: {crime_room}

DESIGN RULES — read carefully, the game is unsolvable if you ignore these:
- Write {clue_target} clues. Every suspect must "hold" at least one clue \
(via held_by) so each character has something to reveal under questioning.
- Each clue's "implicates" names the role_id the clue points toward. Clues \
that implicate the killer ({killer_role['id']}) are the TRUE trail. Clues that \
implicate anyone else are red herrings. Include at least two genuine clues \
that implicate the killer.
- At least one clue must have "contradicts" set to a suspect's role_id whose \
alibi it disproves. Make at least one contradiction clue point at the killer.
- A clue is "held_by" the suspect who will reveal it when a player questions \
them well. It does NOT have to implicate that same suspect — a suspect can \
hold a clue that incriminates someone else (or themselves).
- The clues, taken together, must make {killer_role['id']} the logically \
best answer for an attentive player, without being obvious.
- Reflect the central twist above somewhere in the clues or alibis.

Respond with raw JSON matching this exact structure:
{{
  "title": "The [Adjective] [Noun] of [Place/Name]",
  "victim_name": "a dramatic victim name (NOT a player name)",
  "victim_description": "one sentence about the victim",
  "crime_scene": "{crime_room}",
  "weapon": "{weapon}",
  "killer_id": "{killer_role['id']}",
  "time_of_death": "a specific time tonight e.g. 10:47 PM",
  "opening_narration": "3-4 sentence atmospheric opening read aloud by the narrator. Second person, dark, theatrical.",
  "motive": "the killer's secret motive in 2 sentences",
  "suspects": [
{suspect_schema}
  ],
  "clues": [
    {{
      "id": "clue_1",
      "title": "short evocative clue name e.g. 'The Torn Glove'",
      "text": "2-3 sentences describing the clue and what it suggests",
      "held_by": "role_id of the suspect who reveals this when questioned well",
      "implicates": "role_id this clue points toward (the killer for true clues, someone else for red herrings)",
      "contradicts": "role_id whose alibi this clue disproves, or null",
      "relevance": "a 4-8 word hint shown in the locked clue list, e.g. 'Something about the missing key'"
    }}
  ],
  "acts": [
    {{"act": 1, "title": "The Discovery",     "narration": "2-3 sentences. The body is found. Atmospheric. Read aloud."}},
    {{"act": 2, "title": "Gathering Shadows", "narration": "2-3 sentences. Suspects are assembled. Tensions rise."}},
    {{"act": 3, "title": "Hidden Truths",     "narration": "2-3 sentences. Key evidence surfaces. Someone is lying."}},
    {{"act": 4, "title": "The Accusation",    "narration": "1-2 sentences. Time to name the killer."}}
  ],
  "reveal_narration": "4-5 sentence dramatic reveal. Explain how the murder happened, the motive, and how the clues pointed to the killer. Cinematic and satisfying."
}}

Output exactly {clue_target} clue objects in the clues array."""

    raw = ""
    parsed: dict[str, Any] | None = None
    if llm is not None:
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
        parsed = _fallback_story(chosen_roles, killer_role, crime_room, weapon, rooms, seed)

    # Stamp authoritative cast info onto each suspect. We trust the LLM for
    # creative fields (alibi, secret, persona, clues), but role_name, emoji,
    # player assignment and is_killer come from OUR cast table.
    _enrich_suspects(parsed, cast)

    parsed["_cast"] = cast
    parsed["generated_at"] = datetime.now(tz=None).isoformat(timespec="seconds")

    # Log the story for post-game debugging.
    try:
        from pathlib import Path as _Path
        log_dir = _Path(__file__).parent / "story_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        title = parsed.get("title", "untitled")
        safe = "".join(c for c in title if c.isalnum() or c in " _-")[:40].rstrip()
        log_path = log_dir / f"{ts}_{safe}.json"
        with log_path.open("w", encoding="utf-8") as fh:
            json.dump(parsed, fh, indent=2, ensure_ascii=False)
        _LOGGER.info("Story logged: %s", log_path.name)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Story log write failed (non-fatal)", exc_info=True)

    return parsed


async def generate_npc_reply(
    llm: LLMClient,
    suspect: dict[str, Any],
    story: dict[str, Any],
    history: list[dict[str, str]],
    question: str,
    is_killer: bool,
    held_clues: list[dict[str, Any]] | None = None,
    already_revealed: set[str] | None = None,
    confronting_clue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate an in-character interrogation reply with scoring metadata.

    Returns a dict::

        {
            "reply": str,            # what the player sees
            "on_topic": bool,        # was the question about the murder?
            "revealed_clue_id": str | None,  # a clue this answer let slip
        }

    The relevance judgment and clue-reveal decision are folded into the SAME
    LLM call (the model appends a hidden ``<<<META {...}>>>`` line we parse
    and strip) so each question stays one round-trip — important for the
    local models Mortify targets. On any failure we degrade gracefully to a
    stock deflection that scores as off-topic and reveals nothing.
    """
    held_clues = held_clues or []
    already_revealed = already_revealed or set()
    # Only clues this suspect holds and that the player hasn't already
    # unlocked are eligible to be let slip.
    revealable = [
        c for c in held_clues if c.get("id") not in already_revealed
    ]
    revealable_block = ""
    if revealable:
        lines = "\n".join(
            f'  - id="{c.get("id")}": {c.get("text", "")}'
            for c in revealable
        )
        revealable_block = (
            "\nCLUES YOU KNOW (reveal AT MOST ONE, and only if the detective's "
            "question is relevant enough to earn it; innocent characters share "
            "more readily, the guilty resist):\n" + lines
        )

    confront_block = ""
    if confronting_clue:
        confront_block = (
            "\nThe detective is confronting you with hard evidence: "
            f"\"{confronting_clue.get('text', '')}\". You cannot simply deny it. "
            "React truthfully to being caught out — fluster, partial admission, "
            "or a desperate redirection if you are the killer."
        )

    system = f"""You are playing {suspect.get('role_name', 'a suspect')} in a murder mystery.

CHARACTER NOTES: {suspect.get('npc_persona', 'Nervous, evasive.')}
YOUR ALIBI (stick to this): {suspect.get('alibi', 'I was alone all evening.')}
YOUR SECRET (hide this unless pressed very hard): {suspect.get('secret', 'Nothing.')}
{'YOU ARE THE KILLER. Deflect cleverly. NEVER confess directly unless cornered by hard evidence.' if is_killer else 'YOU ARE INNOCENT but hiding your secret.'}
THE MURDER: victim {story.get('victim_name', 'the victim')} killed with {story.get('weapon', 'an unknown weapon')} in {story.get('crime_scene', 'the house')} at {story.get('time_of_death', 'late evening')}.{revealable_block}{confront_block}

Rules:
- Stay in character ALWAYS. Never break the fourth wall.
- Be dramatic, theatrical, slightly Victorian in speech.
- Reply in 2-4 sentences. NEVER monologue.

After your in-character reply, append on a NEW LINE a metadata tag in this EXACT format and nothing after it:
<<<META {{"on_topic": true|false, "revealed_clue": "clue_id or null"}}>>>
- on_topic is true only if the detective's question is genuinely about the \
murder, the victim, the suspects, the weapon, the timeline, motives, or \
evidence. Small talk, insults, and nonsense are false.
- revealed_clue is the id of a clue from "CLUES YOU KNOW" that your reply just \
disclosed, or null. Only set it if your reply actually contains that clue's \
substance."""

    try:
        raw = await llm.chat(
            system=system,
            history=history,
            user_message=question,
            timeout=LLM_TIMEOUT_NPC,
            user_label="Detective",
            assistant_label=suspect.get("role_name", "Suspect"),
        )
    except LLMTimeoutError:
        _LOGGER.info("NPC reply timed out for %s", suspect.get("role_id"))
        return {
            "reply": _stock_npc_deflection(is_killer),
            "on_topic": False,
            "revealed_clue_id": None,
        }
    except LLMError as err:
        _LOGGER.info("NPC reply failed for %s: %s", suspect.get("role_id"), err)
        return {
            "reply": _stock_npc_deflection(is_killer),
            "on_topic": False,
            "revealed_clue_id": None,
        }

    reply, meta = _split_npc_meta(raw)
    revealed_id = meta.get("revealed_clue")
    # Guard: the model may hallucinate a clue id the suspect doesn't hold.
    valid_ids = {c.get("id") for c in revealable}
    if revealed_id not in valid_ids:
        revealed_id = None
    return {
        "reply": reply or _stock_npc_deflection(is_killer),
        "on_topic": bool(meta.get("on_topic", False)),
        "revealed_clue_id": revealed_id,
    }


_META_RE = re.compile(r"<<<META\s*(\{.*?\})\s*>>>", re.DOTALL)


async def generate_killer_options(
    llm: LLMClient | None,
    suspect: dict[str, Any],
    story: dict[str, Any],
    history: list[dict[str, str]],
    question: str,
    option_count: int = 3,
) -> dict[str, Any]:
    """Pre-generate evasive reply options for a HUMAN killer to choose from.

    Unlike ``generate_npc_reply`` (which writes one reply and may leak a clue),
    this returns several short, in-character deflections so a human player who
    secretly is the killer can pick how to wriggle out of a question. The
    options must NEVER confess or volunteer a clue — the whole point is evasion
    — so we tell the model so explicitly and validate length.

    Returns ``{"options": [str, ...], "on_topic": bool}``. Degrades to stock
    deflections (and a heuristic on-topic guess) on any LLM failure, so the
    game keeps moving even with no/slow LLM.
    """
    n = max(2, min(option_count, 4))
    system = f"""You are scripting dialogue options for the GUILTY party in a murder \
mystery party game. The player controlling {suspect.get('role_name', 'this suspect')} \
secretly committed the murder and is being interrogated by another player. Your job \
is to offer them {n} DISTINCT ways to deflect the question below — WITHOUT ever \
confessing and WITHOUT revealing any real evidence.

CHARACTER: {suspect.get('npc_persona', 'Composed, careful, watchful.')}
THEIR ALIBI (they lean on this): {suspect.get('alibi', 'I was alone all evening.')}
THE MURDER: {story.get('victim_name', 'the victim')} was killed with \
{story.get('weapon', 'an unknown weapon')} in {story.get('crime_scene', 'the house')}.

Each option must:
- Be 1-2 sentences, in character, theatrical and slightly Victorian.
- Deflect, redirect, charm, or feign offence — never admit guilt.
- Be meaningfully different from the others (e.g. one indignant, one redirecting \
suspicion elsewhere, one coolly dismissive).

Respond with RAW JSON only, no commentary:
{{"options": [{', '.join(['"option text"'] * n)}], "on_topic": true|false}}
on_topic is true if the detective's question genuinely concerns the murder, the \
victim, the timeline, the weapon, motives, or evidence."""

    if llm is not None:
        try:
            raw = await llm.chat(
                system=system,
                history=history,
                user_message=question,
                timeout=LLM_TIMEOUT_NPC,
                user_label="Detective",
                assistant_label="Director",
            )
            parsed = _parse_story_json(raw)
            if isinstance(parsed, dict):
                opts = parsed.get("options")
                if isinstance(opts, list):
                    clean = [
                        str(o).strip()
                        for o in opts
                        if isinstance(o, (str, int, float)) and str(o).strip()
                    ][:n]
                    if len(clean) >= 2:
                        return {
                            "options": clean,
                            "on_topic": bool(parsed.get("on_topic", _guess_on_topic(question))),
                        }
        except (LLMTimeoutError, LLMError):
            _LOGGER.info("Killer option generation failed for %s", suspect.get("role_id"))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Killer option generation crashed", exc_info=True)

    # Stock fallback — a small bank of generic guilty deflections.
    pool = list(_STOCK_DEFLECTIONS_GUILTY)
    random.shuffle(pool)
    return {
        "options": pool[:n],
        "on_topic": _guess_on_topic(question),
    }


_ON_TOPIC_KEYWORDS = (
    "kill", "murder", "die", "died", "death", "weapon", "knife", "poison",
    "where", "when", "time", "alibi", "motive", "blood", "victim", "night",
    "saw", "see", "hear", "heard", "money", "inherit", "argue", "argument",
    "lie", "lying", "secret", "evidence", "scene", "room", "clue",
)


def _guess_on_topic(question: str) -> bool:
    """Cheap on-topic heuristic for when the LLM can't judge it for us."""
    q = (question or "").lower()
    return any(k in q for k in _ON_TOPIC_KEYWORDS)





def _split_npc_meta(raw: str) -> tuple[str, dict[str, Any]]:
    """Separate the player-visible reply from the hidden META tag.

    Tolerant of models that forget the tag, malform the JSON, or use a
    null/None token. Returns (clean_reply, meta_dict). On any parse
    failure the reply is the whole string (minus a partial tag) and meta
    defaults to off-topic / no reveal.
    """
    meta: dict[str, Any] = {"on_topic": False, "revealed_clue": None}
    m = _META_RE.search(raw)
    if not m:
        # Strip any dangling "<<<META" the model started but didn't close.
        reply = re.split(r"<<<META", raw, maxsplit=1)[0].strip()
        return reply, meta
    reply = raw[: m.start()].strip()
    blob = m.group(1)
    # Normalise common non-JSON tokens.
    blob = blob.replace("None", "null").replace("True", "true").replace("False", "false")
    try:
        parsed = json.loads(blob)
        if isinstance(parsed, dict):
            rc = parsed.get("revealed_clue")
            if isinstance(rc, str) and rc.strip().lower() in ("null", "none", ""):
                rc = None
            meta = {
                "on_topic": bool(parsed.get("on_topic", False)),
                "revealed_clue": rc,
            }
    except (json.JSONDecodeError, TypeError):
        _LOGGER.debug("Could not parse NPC META blob: %r", blob)
    return reply, meta


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
    correctly (rather than inventing new ones). Clues are no longer nested
    here — they live in the top-level ``clues`` array and reference suspects
    by ``held_by``.
    """
    items = []
    for r in roles:
        items.append(
            "    {\n"
            f'      "role_id": "{r["id"]}",\n'
            '      "alibi": "their claimed alibi for the time of death — plausible but with one flaw",\n'
            '      "secret": "a secret they\'re hiding (not necessarily the murder) that makes them look suspicious",\n'
            '      "npc_persona": "2-sentence acting note for the AI playing this character"\n'
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

    # --- clue validation ---------------------------------------------------
    clues = story.get("clues")
    if not isinstance(clues, list) or len(clues) < 3:
        _LOGGER.debug("Clue list missing or too small")
        return False
    clue_ids: set[str] = set()
    held_by_roles: set[str] = set()
    implicates_killer = 0
    for c in clues:
        if not isinstance(c, dict):
            return False
        if _REQUIRED_CLUE_KEYS - set(c.keys()):
            _LOGGER.debug("Clue missing keys: %s", _REQUIRED_CLUE_KEYS - set(c.keys()))
            return False
        cid = c.get("id")
        if not cid or cid in clue_ids:
            return False
        clue_ids.add(cid)
        held = c.get("held_by")
        if held not in role_ids:
            _LOGGER.debug("Clue %r held_by unknown role %r", cid, held)
            return False
        held_by_roles.add(held)
        if c.get("implicates") not in role_ids:
            _LOGGER.debug("Clue %r implicates unknown role", cid)
            return False
        # contradicts is optional but if present must be a known role.
        contra = c.get("contradicts")
        if contra not in (None, "", *role_ids):
            return False
        if c.get("implicates") == killer_role["id"]:
            implicates_killer += 1
    # Need at least one true clue pointing at the killer, or the case is
    # literally unsolvable.
    if implicates_killer < 1:
        _LOGGER.debug("No clue implicates the killer; rejecting story")
        return False
    return True


def _enrich_suspects(
    story: dict[str, Any],
    cast: list[dict[str, Any]],
) -> None:
    """Stamp authoritative cast info onto each suspect entry.

    Note: we deliberately do NOT stamp a player name here. Who plays each
    suspect is decided after generation (in game._assign_roles) and kept
    secret until the reveal, so the suspect objects stay anonymous.
    """
    cast_by_id = {c["role_id"]: c for c in cast}
    for s in story.get("suspects", []):
        meta = cast_by_id.get(s.get("role_id"))
        if not meta:
            continue
        s["role_name"] = meta["role_name"]
        s["role_emoji"] = meta["role_emoji"]
        s["role_description"] = meta["role_description"]
        s["is_killer"] = meta["is_killer"]


def _fallback_story(
    roles: list[dict[str, Any]],
    killer_role: dict[str, Any],
    crime_room: str,
    weapon: str,
    rooms: list[str],
    seed: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Deterministic story used when the LLM is unavailable.

    Still varies meaningfully between games: it weaves the sampled scenario
    seed (setting / occasion / era / twist / motive) into the title, victim,
    narration, and motive so a no-LLM or weak-LLM host doesn't get the exact
    same "Silent House" every time. The clue trail is fully wired so the
    deduction loop works end-to-end.
    """
    seed = seed or _build_scenario_seed(rooms)
    pool_rooms = rooms or ["the hallway", "the kitchen", "the study"]
    suspects: list[dict[str, Any]] = []
    for r in roles:
        is_killer = r["id"] == killer_role["id"]
        suspects.append({
            "role_id": r["id"],
            "alibi": (
                f"I was in {random.choice(pool_rooms)} the whole evening. "
                f"Anyone could vouch for me. Probably."
            ),
            "secret": (
                "I had been quietly meeting with the victim earlier this week "
                "to discuss money."
            ),
            "npc_persona": (
                "Nervous, defensive, speaks quickly. Eyes dart to the door."
            ),
        })

    # Build a clue trail. Each suspect holds exactly one clue (so every
    # character has something to reveal). We guarantee the real trail
    # regardless of where the killer falls in the role list:
    #   * the first innocent's clue IMPLICATES the killer
    #   * the second innocent's clue CONTRADICTS the killer's alibi
    #   * if there is only one innocent, that single clue does BOTH
    #   * any remaining innocents hold red-herring clues
    #   * the killer holds a clue clumsily blaming someone else
    # The counter ``innocent_idx`` tracks position AMONG INNOCENTS, not the
    # full role list — the previous version keyed off the full-list index,
    # which silently dropped the contradiction clue when the killer sat
    # early in the list.
    killer_id = killer_role["id"]
    other_ids = [r["id"] for r in roles if r["id"] != killer_id]
    innocent_count = len(other_ids)
    clues: list[dict[str, Any]] = []
    innocent_idx = 0
    for n, r in enumerate(roles, start=1):
        clue_id = f"clue_{n}"
        if r["id"] == killer_id:
            # The killer holds a clue that (clumsily) implicates someone else.
            target = random.choice(other_ids) if other_ids else killer_id
            clues.append({
                "id": clue_id,
                "title": "A Hastily Offered Name",
                "text": (
                    "They were quick — too quick — to suggest someone else had "
                    "a reason to want the victim gone."
                ),
                "held_by": r["id"],
                "implicates": target,
                "contradicts": None,
                "relevance": "Who they tried to blame",
            })
            continue

        innocent_idx += 1
        only_one_innocent = innocent_count == 1
        is_first = innocent_idx == 1
        is_second = innocent_idx == 2

        if is_first:
            # Implicates the killer. Also contradicts them if it's the only
            # innocent clue available to carry the contradiction.
            clues.append({
                "id": clue_id,
                "title": "The Missing Glove",
                "text": (
                    "A single glove was found near the scene, its pair "
                    "belonging to someone who claimed to be elsewhere."
                ),
                "held_by": r["id"],
                "implicates": killer_id,
                "contradicts": killer_id if only_one_innocent else None,
                "relevance": "Something near the scene",
            })
        elif is_second:
            # The contradiction clue against the killer's alibi.
            clues.append({
                "id": clue_id,
                "title": "Footsteps After Hours",
                "text": (
                    "Footsteps were heard near the scene at the very hour "
                    "the killer swore they were across the house."
                ),
                "held_by": r["id"],
                "implicates": killer_id,
                "contradicts": killer_id,
                "relevance": "Who was really where",
            })
        else:
            # Remaining innocents hold red herrings pointing at other innocents.
            herring_pool = [oid for oid in other_ids if oid != r["id"]] or other_ids
            target = random.choice(herring_pool)
            clues.append({
                "id": clue_id,
                "title": "A Suspicious Stain",
                "text": (
                    "A dark stain on the carpet looked damning — until you "
                    "remember the wine spilled earlier that night."
                ),
                "held_by": r["id"],
                "implicates": target,
                "contradicts": None,
                "relevance": "A stain that misleads",
            })

    # Pick varied surface text from small banks, themed by the seed.
    victim_name = random.choice([
        "Lord Edmund Blackwell", "Vivienne Marchetti", "Director Caine Holloway",
        "Dr. Aurelia Voss", "Captain Reginald Frost", "Madame Celeste Dubois",
        "Sir Anthony Pemberton", "Magnate Ira Sterling",
    ])
    title_noun = random.choice([
        "Last Toast", "Final Curtain", "Broken Vow", "Silent Guest",
        "Quiet Hour", "Spilled Secret", "Cold Inheritance", "Unlit Candle",
    ])
    title = f"The {title_noun}"
    setting = seed.get("setting", "a house cut off from the world")
    occasion = seed.get("occasion", "a gathering")
    motive_family = seed.get("motive_family", "an old, bitter grudge")
    twist = seed.get("twist", "")

    return {
        "title": title,
        "victim_name": victim_name,
        "victim_description": f"The reason everyone had gathered for {occasion}.",
        "crime_scene": crime_room,
        "weapon": weapon,
        "killer_id": killer_role["id"],
        "time_of_death": random.choice([
            "10:47 PM", "11:15 PM", "11:52 PM", "12:08 AM", "9:33 PM",
        ]),
        "opening_narration": (
            f"It was meant to be {occasion}, held at {setting}. "
            f"Then the lights in {crime_room} guttered and died. When they "
            f"returned, {victim_name} lay still. No one came, and no one left. "
            f"The killer is among you."
        ),
        "motive": (
            f"It traces back to {motive_family}. {twist} "
            f"By tonight, the killer had run out of other options."
        ).strip(),
        "suspects": suspects,
        "clues": clues,
        "acts": [
            {"act": 1, "title": "The Discovery",
             "narration": f"A terrible deed has been done at {setting}. The victim lies still."},
            {"act": 2, "title": "Gathering Shadows",
             "narration": "The suspects are assembled. No one may leave."},
            {"act": 3, "title": "Hidden Truths",
             "narration": "Evidence mounts. Someone in this room is lying."},
            {"act": 4, "title": "The Accusation",
             "narration": "The time has come. Name the killer."},
        ],
        "reveal_narration": (
            f"The truth, at last. The clues lined up — the timing, the lie in "
            f"the alibi, and a motive rooted in {motive_family} that had been "
            f"hiding in plain sight. Justice, however imperfect, is served."
        ),
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

# Mortify 🔪

> **AI-powered murder mystery party game for Home Assistant** — scan a QR code, interrogate AI-powered suspects, accuse the killer. Architected after [Quizify](https://github.com/engabd11/Quizify); inspired by [Beatify](https://github.com/mholzi/beatify).

[![Home Assistant 2024.1+](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-41BDF5?style=flat-square&logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![Local AI](https://img.shields.io/badge/AI-100%25%20Local-2d6b4a?style=flat-square)](https://ollama.com/)
[![License MIT](https://img.shields.io/badge/License-MIT-gold?style=flat-square)](LICENSE)

---

## What Is Mortify?

Mortify turns your smart home into a live murder mystery. A local AI — any conversation agent you've configured in Home Assistant (Ollama, LocalAI, OpenAI, etc.) — generates a unique, self-contained mystery, optionally flavoured with your real room names and devices for atmosphere. Players scan a QR code, receive a secret character role, interrogate AI-powered suspects via chat to draw out the clues each one is hiding, confront a suspect with the evidence, and vote on the killer — all coordinated in real-time over WebSockets, with no apps to install and no HA accounts to create.

**100% local. No cloud. No subscription. Runs entirely on your network.**

---

## Highlights

- **🎯 Multi-session** — Run multiple games at once (e.g. one per room at a party)
- **🤖 LLM-powered story generation** — Each mystery is unique, seeded with your home data
- **🕵️ AI NPC interrogation** — Players chat with suspects; the AI stays in character
- **📱 QR-code join** — No accounts, no app install, just scan and play
- **🔐 HMAC-signed player tokens** — Players keep their identity across reloads
- **🔊 Optional TTS narration** — Dramatic act announcements through your speakers
- **🌐 Fully local** — No CDNs, no Google Fonts, no telemetry, nothing leaves your network
- **⚡ Real-time multiplayer** — WebSocket-driven, instant updates for every player

---

## Requirements

- **Home Assistant** 2024.1+
- A configured **conversation agent** (Ollama, LocalAI, OpenAI, OpenWebUI, etc.) accessible via `conversation.process`. Set this up in *Settings → Voice Assistants*.
- (Optional) A `media_player` entity for music and a `tts.*` entity for narration

---

## Install via HACS

1. Open HACS → ⋮ Menu → **Custom Repositories**
2. URL: `https://github.com/yourusername/mortify`
3. Category: **Integration**
4. Install **Mortify**, then restart Home Assistant
5. *Settings → Devices & Services → Add Integration → Mortify*
6. Open **Mortify** from the sidebar and start a game

### Manual install

```bash
cd /config/custom_components
git clone https://github.com/yourusername/mortify.git mortify
# Restart Home Assistant
```

---

## How to play

### As the host

1. Open **Mortify** from the HA sidebar
2. Pick your AI agent, scene speaker, optional TTS narrator
3. (Optional) Select a few atmosphere devices (motion sensors, lights, locks…) to flavour the narration
4. Pick difficulty and number of suspects (3–8)
5. Hit **Begin the Mystery** — the AI writes a unique story (30–60 s on local models)
6. Share the QR code with your guests
7. Use **Advance Act** to move through the four acts
8. Hit **Reveal the Killer** when ready

### As a player

1. Scan the QR code (or open `http://YOUR-HA-IP:8123/mortify/play?code=CODE`)
2. Enter your name — you'll receive a secret character role
3. **Interrogate suspects** — tap a character, ask anything; the AI answers in character. Ask questions that are genuinely about the murder and you'll score for good detective work — and a suspect may *let slip* a clue they're holding.
4. **Track clues** — the Clues tab lists every clue in the case. Locked clues show a hint about what they concern and which suspect holds them; question that suspect well to unlock the full text.
5. **Confront** — once per game you can throw a clue you've uncovered in a suspect's face. Land it on the real killer with a clue that implicates or contradicts them and you score big; a baseless confrontation costs you.
6. When the host moves to the accusation phase, **accuse a suspect**
7. Watch the dramatic reveal

The player page works without a Home Assistant account; the join code is the only credential needed. A short-lived HMAC-signed token in `localStorage` keeps your identity across reloads.

---

## Scoring

| Element | Effect |
| --- | --- |
| Correct accusation | 1000 points |
| Each clue unlocked (via interrogation) | 50 points |
| On-topic interrogation question | 20 points (capped, so you can't farm it) |
| Correct accusation + held a clue implicating/contradicting the killer | +250 bonus (followed the real trail, not a guess) |
| Confront the real killer with a fitting clue | +300 points |
| Baseless confrontation | −100 points |
| Wrong accusation | Keeps any clue / questioning points earned |

Clue points, question points, and confront points are awarded live during the
game, so the scoreboard reflects real detective work as it happens. The
accusation and evidence bonus land at the reveal.

---

## Architecture

```
Home Assistant
└── Mortify Integration
    ├── Manager (sessions, music, agent/entity/speaker discovery, HMAC tokens)
    ├── GameSession × N (state machine: lobby → generating → act_1/2/3 → accusation → reveal)
    │   └── async lock around state transitions
    ├── Admin WebSocket API (rides HA's authenticated socket)
    ├── Player WebSocket (dedicated, unauthenticated, rate-limited, raw aiohttp)
    ├── HTTP Views (QR code PNG, public guest page, static assets)
    └── Frontend (custom HA panel for the host + guest page bundle)
```

- Single HA port — no extra services
- Local-first: no cloud, no telemetry, no analytics, no web fonts
- Custom-element panel (not iframe) — works with HA's auth out of the box

### Story generation

On game start, Mortify:

1. Optionally reads your selected atmosphere devices and their rooms for narration flavour
2. Sends a structured prompt to your configured conversation agent with player names, the chosen difficulty, and the cast
3. Asks for a full mystery including a **clue graph** — each clue is owned by a suspect, points toward (implicates) a suspect, and may contradict a suspect's alibi
4. Parses and validates the JSON story, rejecting any case where no clue points at the killer (it would be unsolvable) and falling back if the model emits garbage
5. Assigns player roles and begins Act 1

If the LLM is unavailable or returns garbage, Mortify falls back to a deterministic stock mystery — complete with a valid clue trail (clues that implicate *and* contradict the killer) so the deduction loop still works end-to-end. You'll see a "fallback" indicator in the host UI.

### NPC interrogation

When a player asks a suspect a question:

- The conversation history for that (player, suspect) pair is maintained server-side
- A character-specific system prompt (alibi, secret, guilty/innocent flag, and the clues that suspect is holding) is prepended
- Guilty suspects deflect cleverly; innocent suspects guard their secret
- In the *same* LLM round-trip, the model appends a hidden metadata tag judging whether the question was on-topic and whether the reply let a clue slip — the server strips this tag, awards points for good questions, and unlocks any revealed clue for that player. Folding scoring into the one call keeps latency low on local models.
- Calls are rate-limited per (player, suspect) so a runaway client can't DoS the LLM

---

## Recommended models

Any HA-configured conversation agent works. For local Ollama models:

| Model | Quality | Speed | Notes |
|-------|---------|-------|-------|
| `llama3.2` | ⭐⭐⭐ | Fast | Solid default, works on 8 GB VRAM |
| `mistral` | ⭐⭐⭐⭐ | Medium | Better story quality |
| `qwen2.5:7b` | ⭐⭐⭐⭐ | Medium | Excellent JSON adherence |
| `llama3.1:70b` | ⭐⭐⭐⭐⭐ | Slow | Best quality, needs 24 GB+ VRAM |

---

## FAQ

**Does it need internet?**
No. Everything runs locally — the conversation agent, the QR rendering, the assets. The integration ships its own fonts and QR generator.

**How many players?**
Tested with 2–10. Players beyond the suspect count become observers — they can still interrogate and accuse, but don't have a role.

**Can guests join mid-game?**
Yes. If a role is still free they get assigned; otherwise they join as observers.

**Can I run more than one game at a time?**
Yes — each game has its own session ID and join code. Useful for a multi-room party.

**What happens if a player reloads the join page?**
They keep their identity. A short-lived HMAC-signed token in `localStorage` lets the server recognise them on reconnect.

**Why a custom panel instead of an iframe?**
Iframes can't see HA's auth session from the parent shell, which broke the original Mortify in many setups. The custom panel runs inside HA's authenticated frame and talks to the server over the same WebSocket the rest of HA uses.

---

## License

[MIT](LICENSE) — fork it, ship it, throw a party.

Made with dark intent for the Home Assistant community 🕯️

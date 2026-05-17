# Mortify 🔪

### **AI-Powered Murder Mystery Game for Home Assistant**

Your smart home is the crime scene. The AI writes the murder. Your guests solve it.

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1+-41BDF5?style=for-the-badge&logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![Local AI](https://img.shields.io/badge/AI-100%25%20Local-2d6b4a?style=for-the-badge)](https://ollama.com/)
[![License](https://img.shields.io/badge/License-MIT-gold?style=for-the-badge)](LICENSE)

---

## What Is Mortify?

Mortify turns your smart home into a live murder mystery. A local AI (Ollama / LM Studio) generates a unique mystery using your **real room names, real devices, and real sensor states** as the setting and clues. Players scan a QR code, receive a secret character role on their phones, interrogate AI-powered suspects via chat, discover clues by examining your real HA entities, and vote on the killer — all while atmospheric background music and dramatic TTS narration play through your speakers.

**100% local. No cloud. No subscription. Runs entirely on your network.**

---

## Features

- 🏠 **Your home as the crime scene** — real rooms, real devices, real sensor states become clues
- 🤖 **AI story generation** — unique mystery every game, seeded with your home data
- 🕵️ **AI NPC interrogation** — players chat with suspects; the AI stays in character
- 🔊 **Atmospheric music + TTS narration** — background score, ducked for dramatic announcements
- 📱 **QR code join** — guests scan and play, no app download needed
- 🎭 **Character roles** — each player gets a secret identity with alibi and hidden secret
- 🕯️ **Four acts** — Discovery → Investigation → Shadows → Accusation → Reveal
- ⚖️ **Scoring** — points for correct accusation and clues discovered
- 🔒 **Fully local** — Ollama or LM Studio, everything stays on your network

---

## Requirements

- **Home Assistant** 2024.1+
- **[Ollama](https://ollama.com/)** or **[LM Studio](https://lmstudio.ai/)** running locally
- A recommended model: `llama3.2`, `mistral`, or `qwen2.5` (7B+ recommended for story quality)
- At least one `media_player` entity for music/TTS
- **HACS** (recommended) or manual install

---

## Setup

### Step 1 — Install Ollama and pull a model

```bash
# Install Ollama (https://ollama.com)
ollama pull llama3.2
# or for better quality:
ollama pull mistral
```

### Step 2 — Install Mortify via HACS

```
HACS → ⋮ Menu → Custom Repositories
→ URL: https://github.com/yourusername/mortify
→ Category: Integration
→ Install "Mortify"
→ Restart Home Assistant
```

**Manual install:**
```bash
cd /config/custom_components
git clone https://github.com/yourusername/mortify.git mortify
# Restart Home Assistant
```

### Step 3 — Add background music (optional but recommended)

Copy the included MP3 files to your HA `www` folder:
```
/config/www/mortify/lobby_ambience.mp3
/config/www/mortify/dark_intro.mp3
/config/www/mortify/investigation_theme.mp3
/config/www/mortify/tension_rising.mp3
/config/www/mortify/dramatic_reveal.mp3
/config/www/mortify/winner_fanfare.mp3
```
Any royalty-free ambient/horror tracks work. Rename to match.

### Step 4 — Configure

```
Settings → Devices & Services → Add Integration → "Mortify"
```

Fill in:
- **Local LLM URL** — e.g. `http://localhost:11434` (Ollama) or `http://localhost:1234` (LM Studio)
- **Model name** — e.g. `llama3.2`
- **TTS entity** — optional, e.g. `tts.home_assistant_cloud` or leave blank
- **HA URL** — your HA address for QR codes, e.g. `http://192.168.1.100:8123`

---

## Playing a Game

### For the Host

1. Open Home Assistant → click **Mortify** in the sidebar
2. **Select a speaker** — music and narration play here
3. **Choose your narrator** (TTS entity) — or use the same speaker
4. **Pick clue entities** — motion sensors, locks, lights, temperature sensors — at least 4
5. Display the **QR code** for guests to scan
6. Click **Begin the Mystery** — the AI generates your unique murder
7. Use **▶ Advance Act** to progress through the four acts
8. Hit **⚖️ Reveal the Killer** to end the game dramatically

### For Players

1. Scan the QR code (or go to `http://YOUR-HA-IP:8123/mortify/play`)
2. Enter your name
3. Receive your secret character role
4. **Interrogate suspects** — tap a character and ask them anything
5. **Examine clues** — tap entities to discover evidence
6. Take **notes** in the Notes tab
7. When prompted, **accuse your killer**
8. Watch the dramatic reveal on the host screen

---

## Architecture

```
Home Assistant
    └── Mortify Integration
            ├── Game Manager (state machine)
            ├── Story Generator (local LLM)
            ├── LLM Client (Ollama / LM Studio)
            ├── WebSocket API (real-time sync)
            ├── HTTP Views (Admin + Player UI)
            └── Media Services (TTS + music)
```

**All inference runs locally.** The LLM client auto-detects Ollama vs OpenAI-compatible backends.

### Story Generation

On game start, Mortify:
1. Reads your entity registry and area assignments
2. Pulls recent interesting state changes from your home
3. Sends a structured prompt to your local LLM with rooms, devices, player names
4. Parses the JSON story: victim, suspects, alibis, clues, acts, reveal
5. Assigns player roles and begins Act 1

### NPC Interrogation

Each suspect is an AI persona. When a player asks a question:
- The conversation history for that player ↔ suspect pair is maintained
- A character-specific system prompt keeps the NPC in character
- Guilty suspects deflect cleverly; innocent suspects hide their own secrets
- Responses arrive within ~2 seconds on a modern GPU

---

## Recommended Models

| Model | Quality | Speed | Notes |
|-------|---------|-------|-------|
| `llama3.2` | ⭐⭐⭐ | Fast | Good default, works on 8GB VRAM |
| `mistral` | ⭐⭐⭐⭐ | Medium | Better story quality |
| `qwen2.5:7b` | ⭐⭐⭐⭐ | Medium | Excellent JSON adherence |
| `llama3.1:70b` | ⭐⭐⭐⭐⭐ | Slow | Best quality, needs 24GB+ VRAM |

---

## FAQ

**Does it need internet?**
No. Everything runs locally. The only external request is the Google Fonts CSS (for typography) — you can self-host fonts if you want fully offline operation.

**How many players?**
Tested with 2–8 players. Roles are assigned to players first; extra characters become NPCs.

**What if the LLM is slow?**
Story generation takes 30–60 seconds on a 7B model. NPC responses take 2–5 seconds. The loading screen covers generation time.

**Can I run it without a local LLM?**
The integration requires a local LLM for story generation and NPC chat. Without it, a fallback static mystery is used (limited but functional).

**Can guests join mid-game?**
Yes — they join as observers and can interrogate suspects but don't receive a role.

---

## Contributing

PRs welcome! Ideas for expansion:
- More suspect roles and weapons
- Difficulty modes (fewer clues = harder)
- Custom mystery templates (JSON)
- Persistent game history
- TV dashboard view for the big screen

---

## License

MIT — see [LICENSE](LICENSE)

---

*Made with dark intent for the Home Assistant community* 🕯️

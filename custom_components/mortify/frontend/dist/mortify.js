/* ─────────────────────────────────────────────────────────────────────────
   Mortify frontend — single bundle for both contexts.

   1. As an HA custom panel: the HA frontend imports this module while
      authenticated, finds the <mortify-panel> custom element, and hands
      it the hass object. We render the admin UI inside a shadow root so
      our CSS can't leak into HA (and vice versa).
   2. As the script tag on the public guest page (/mortify/play?code=XXXX):
      we look for <div id="mortify-root" data-view="player"> and render
      the player UI into it.

   No build step. No React. Just a single vanilla module written to be
   readable as a maintenance target — mirrors Quizify's index.jsx in
   architecture but uses plain DOM APIs.
   ───────────────────────────────────────────────────────────────────── */

const PLAYER_WS_PATH = "/api/mortify/player_ws";
const STATIC_PREFIX = "/mortify-static";

// ── shared helpers ──────────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (v === false || v == null) {
      // skip
    } else if (v === true) {
      node.setAttribute(k, "");
    } else {
      node.setAttribute(k, String(v));
    }
  }
  for (const child of [].concat(children || [])) {
    if (child == null || child === false) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function showToast(root, text) {
  let toast = root.querySelector(".mty-toast");
  if (!toast) {
    toast = el("div", { class: "mty-toast" });
    root.appendChild(toast);
  }
  toast.textContent = text;
  toast.classList.add("mty-toast-show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toast.classList.remove("mty-toast-show"), 3500);
}

// ───────────────────────────────────────────────────────────────────────
// Admin custom element
// ───────────────────────────────────────────────────────────────────────

class MortifyPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._unsub = null;
    this._unsubAgents = null;

    // UI state
    this._state = {
      view: "setup",           // setup | game | accusation | reveal
      agents: [],
      speakers: [],
      ttsEntities: [],
      entities: [],
      lights: [],
      selectedAgent: "",
      selectedSpeaker: "",
      selectedTts: "",
      selectedEntities: new Set(),
      selectedLights: new Set(),
      ttsSpeed: "normal",
      lightsEnabled: true,
      difficulty: "medium",
      suspectCount: 4,
      session: null,           // {session_id, join_code, game}
      generating: false,
    };

    // Restore selections from localStorage on construction. localStorage
    // is shared across iframes/contexts at the same origin, which is fine
    // here — the admin panel and player page use different keys.
    try {
      const saved = JSON.parse(localStorage.getItem("mortify_admin") || "{}");
      if (saved.agent) this._state.selectedAgent = saved.agent;
      if (saved.speaker) this._state.selectedSpeaker = saved.speaker;
      if (saved.tts) this._state.selectedTts = saved.tts;
      if (Array.isArray(saved.entities)) {
        this._state.selectedEntities = new Set(saved.entities);
      }
      if (saved.difficulty) this._state.difficulty = saved.difficulty;
      if (saved.suspectCount) this._state.suspectCount = saved.suspectCount;
      if (Array.isArray(saved.lights)) {
        this._state.selectedLights = new Set(saved.lights);
      }
      if (saved.ttsSpeed) this._state.ttsSpeed = saved.ttsSpeed;
      if (typeof saved.lightsEnabled === "boolean") this._state.lightsEnabled = saved.lightsEnabled;
    } catch (e) {
      // ignore
    }
  }

  set hass(value) {
    const first = this._hass == null;
    this._hass = value;
    if (first) {
      this._afterFirstHass();
    }
  }
  get hass() { return this._hass; }

  // Standard HA panel props we don't use:
  set narrow(_) {}
  set route(_) {}
  set panel(_) {}

  connectedCallback() {
    if (!this.shadowRoot) {
      const shadow = this.attachShadow({ mode: "open" });
      // Style imported from disk — bundled into static. The shadow DOM
      // means HA's global stylesheet can't override our look.
      const linkCss = document.createElement("link");
      linkCss.rel = "stylesheet";
      linkCss.href = `${STATIC_PREFIX}/mortify.css`;
      shadow.appendChild(linkCss);
      this._root = document.createElement("div");
      this._root.className = "mty-admin mty-shadow-host";
      shadow.appendChild(this._root);
    }
    this._render();
  }

  disconnectedCallback() {
    if (this._unsub) {
      this._unsub();
      this._unsub = null;
    }
  }

  _saveSettings() {
    try {
      localStorage.setItem("mortify_admin", JSON.stringify({
        agent: this._state.selectedAgent,
        speaker: this._state.selectedSpeaker,
        tts: this._state.selectedTts,
        entities: [...this._state.selectedEntities],
        lights: [...this._state.selectedLights],
        ttsSpeed: this._state.ttsSpeed,
        lightsEnabled: this._state.lightsEnabled,
        difficulty: this._state.difficulty,
        suspectCount: this._state.suspectCount,
      }));
    } catch (e) { /* private mode etc */ }
  }

  async _afterFirstHass() {
    // Fetch the picker lists in parallel. Errors here just leave the
    // pickers empty — the UI gives the host a useful "nothing found"
    // message rather than blowing up.
    await Promise.all([
      this._loadAgents(),
      this._loadSpeakers(),
      this._loadTts(),
      this._loadEntities(),
      this._loadLights(),
    ]);
    this._render();
  }

  async _ws(type, extra = {}) {
    if (!this._hass) throw new Error("hass not ready");
    return await this._hass.callWS({ type, ...extra });
  }

  async _loadAgents() {
    try {
      const r = await this._ws("mortify/agents/list");
      this._state.agents = r.agents || [];
    } catch (e) {
      console.warn("Mortify: failed to load agents", e);
      this._state.agents = [];
    }
  }
  async _loadSpeakers() {
    try {
      const r = await this._ws("mortify/speakers/list");
      this._state.speakers = r.speakers || [];
    } catch (e) {
      this._state.speakers = [];
    }
  }
  async _loadTts() {
    try {
      const r = await this._ws("mortify/tts/list");
      this._state.ttsEntities = r.tts_entities || [];
    } catch (e) {
      this._state.ttsEntities = [];
    }
  }
  async _loadEntities() {
    try {
      const r = await this._ws("mortify/entities/list");
      this._state.entities = r.entities || [];
    } catch (e) {
      this._state.entities = [];
    }
  }
  async _loadLights() {
    try {
      const r = await this._ws("mortify/lights/list");
      this._state.lights = r.lights || [];
    } catch (e) {
      this._state.lights = [];
    }
  }

  async _createAndStart() {
    const s = this._state;
    if (!s.selectedAgent) { showToast(this._root, "Pick an AI agent first"); return; }
    s.generating = true;
    this._render();
    try {
      const created = await this._ws("mortify/game/create", {
        agent_entity_id: s.selectedAgent,
        entity_ids: [...s.selectedEntities],
        music_player: s.selectedSpeaker || null,
        tts_entity: s.selectedTts || null,
        difficulty: s.difficulty,
        suspect_count: s.suspectCount,
        tts_speed: s.ttsSpeed,
        light_entity_ids: [...s.selectedLights],
        lights_enabled: s.lightsEnabled,
      });
      s.session = created;
      this._saveSettings();
      // Subscribe BEFORE start so we don't miss the first act_started event.
      await this._subscribe(created.session_id);
      // Now actually kick off the game (LLM call).
      await this._ws("mortify/game/start", { session_id: created.session_id });
    } catch (err) {
      console.error("Mortify: failed to create/start game", err);
      showToast(this._root, "Could not start game: " + (err?.message || err));
      s.generating = false;
      s.session = null;
    }
    this._render();
  }

  async _subscribe(sessionId) {
    if (this._unsub) {
      try { this._unsub(); } catch (e) {}
      this._unsub = null;
    }
    this._unsub = await this._hass.connection.subscribeMessage(
      (msg) => this._onEvent(msg),
      { type: "mortify/admin/subscribe", session_id: sessionId },
    );
  }

  _onEvent(msg) {
    const s = this._state;
    // Update local session snapshot on every event.
    if (msg.game) {
      s.session = { ...(s.session || {}), game: msg.game };
    }
    const ev = msg.event;
    if (ev === "act_started" || ev === "narration" || ev === "snapshot") {
      // routine update — re-render
    } else if (ev === "revealed") {
      s.view = "reveal";
    } else if (ev === "game_ended") {
      // Game cancelled. Drop back to setup.
      s.session = null;
      s.view = "setup";
      s.generating = false;
    }
    if (msg.game?.state === "accusation") s.view = "accusation";
    else if (msg.game?.state === "reveal" || msg.game?.state === "ended") s.view = "reveal";
    else if (msg.game?.state?.startsWith("act_")) s.view = "game";
    else if (msg.game?.state === "lobby" || msg.game?.state === "generating") s.view = "game";

    s.generating = msg.game?.state === "generating";
    this._render();
  }

  async _nextAct() {
    const s = this._state;
    if (!s.session) return;
    try {
      await this._ws("mortify/game/next_act", { session_id: s.session.session_id });
    } catch (e) {
      showToast(this._root, e.message || "Could not advance act");
    }
  }

  async _revealKiller() {
    const s = this._state;
    if (!s.session) return;
    try {
      await this._ws("mortify/game/reveal", { session_id: s.session.session_id });
    } catch (e) {
      showToast(this._root, e.message || "Could not reveal");
    }
  }

  async _endGame() {
    const s = this._state;
    if (!s.session) return;
    if (!confirm("End this mystery? It cannot be resumed.")) return;
    try {
      await this._ws("mortify/game/end", { session_id: s.session.session_id });
    } catch (e) {
      showToast(this._root, e.message || "Could not end");
    }
    s.session = null;
    s.view = "setup";
    this._render();
  }

  async _rematch() {
    const s = this._state;
    if (!s.session) return;
    try {
      const created = await this._ws("mortify/game/rematch", {
        session_id: s.session.session_id,
      });
      s.session = created;
      await this._subscribe(created.session_id);
      await this._ws("mortify/game/start", { session_id: created.session_id });
      s.view = "game";
    } catch (e) {
      showToast(this._root, e.message || "Could not start rematch");
    }
    this._render();
  }

  // ── rendering ─────────────────────────────────────────────────────────

  _render() {
    if (!this._root) return;
    const s = this._state;
    this._root.innerHTML = "";

    const header = el("div", { class: "mty-admin-header" }, [
      el("div", { class: "mty-title", html: "🔪 <span>Mortify</span>" }),
      el("div", { class: "mty-status-bar" }, [
        el("span", { class: "mty-status-dot " + (s.session ? "mty-pulse" : "") }),
        document.createTextNode(this._statusText()),
      ]),
    ]);
    this._root.appendChild(header);

    const body = el("div", { class: "mty-admin-body" });
    this._root.appendChild(body);

    if (!s.session) {
      body.appendChild(this._renderSetup());
    } else if (s.view === "reveal") {
      body.appendChild(this._renderReveal());
    } else if (s.view === "accusation") {
      body.appendChild(this._renderAccusation());
    } else {
      body.appendChild(this._renderGame());
    }

    if (s.generating) {
      this._root.appendChild(this._renderLoading());
    }
  }

  _statusText() {
    const s = this._state;
    if (!s.session) return "LOBBY";
    const g = s.session.game || {};
    return (g.state || "lobby").toUpperCase();
  }

  _renderSetup() {
    const s = this._state;
    const wrap = el("div", { class: "mty-admin-main" });

    // QR code is shown once a session exists; before that, the host needs
    // to create one. So in setup, we don't show QR — just the pickers.

    // Section 1: AI Agent
    wrap.appendChild(this._sectionCard(1, "AI Agent",
      "Pick the conversation entity that will write the mystery and play the suspects. " +
      "Configured in Settings → Voice Assistants.",
      (() => {
        const grid = el("div", { class: "mty-pick-grid" });
        if (!s.agents.length) {
          grid.appendChild(el("div", { class: "mty-empty" },
            "No conversation agents found. Add Ollama or another agent in Settings → Voice Assistants."));
        } else {
          for (const a of s.agents) {
            const card = el("div", {
              class: "mty-pick-card" + (s.selectedAgent === a.entity_id ? " mty-pick-card-selected" : "") + (!a.available ? " mty-pick-card-disabled" : ""),
              onClick: () => {
                if (!a.available) return;
                s.selectedAgent = a.entity_id;
                this._saveSettings();
                this._render();
              },
            }, [
              el("div", { class: "mty-pick-card-name" }, a.name),
              el("div", { class: "mty-pick-card-id" }, a.entity_id),
              el("div", { class: "mty-pick-card-badge" }, a.available ? "ready" : "unavailable"),
            ]);
            grid.appendChild(card);
          }
        }
        return grid;
      })(),
    ));

    // Section 2: Speaker
    wrap.appendChild(this._sectionCard(2, "Scene Speaker",
      "Background music and TTS narration play here.",
      (() => {
        const grid = el("div", { class: "mty-pick-grid" });
        if (!s.speakers.length) {
          grid.appendChild(el("div", { class: "mty-empty" }, "No media_player entities found."));
        } else {
          for (const sp of s.speakers) {
            const card = el("div", {
              class: "mty-pick-card" + (s.selectedSpeaker === sp.entity_id ? " mty-pick-card-selected" : ""),
              onClick: () => {
                s.selectedSpeaker = (s.selectedSpeaker === sp.entity_id) ? "" : sp.entity_id;
                this._saveSettings();
                this._render();
              },
            }, [
              el("div", { class: "mty-pick-card-name" }, sp.name),
              el("div", { class: "mty-pick-card-id" }, sp.entity_id),
              el("div", { class: "mty-pick-card-badge" }, sp.state),
            ]);
            grid.appendChild(card);
          }
        }
        return grid;
      })(),
    ));

    // Section 3: TTS
    wrap.appendChild(this._sectionCard(3, "Narrator Voice (optional)",
      "Separate TTS engine for dramatic narration. Leave blank if you don't have TTS set up.",
      (() => {
        const col = el("div");
        const sel = el("select", { class: "mty-input" });
        sel.appendChild(el("option", { value: "" }, "— No TTS narration —"));
        for (const t of s.ttsEntities) {
          const opt = el("option", { value: t.entity_id }, t.name);
          if (t.entity_id === s.selectedTts) opt.selected = true;
          sel.appendChild(opt);
        }
        sel.addEventListener("change", (ev) => {
          s.selectedTts = ev.target.value;
          this._saveSettings();
          this._render();
        });
        col.appendChild(sel);
        // Speaking speed — only meaningful when a TTS engine is selected.
        if (s.selectedTts) {
          col.appendChild(el("div", { class: "mty-label", style: "margin-top:10px" }, "Narration speed"));
          const speedSel = el("select", { class: "mty-input" });
          for (const [val, label] of [["slow", "Slow & deliberate"], ["normal", "Normal"], ["fast", "Brisk"]]) {
            const opt = el("option", { value: val }, label);
            if (val === s.ttsSpeed) opt.selected = true;
            speedSel.appendChild(opt);
          }
          speedSel.addEventListener("change", (ev) => {
            s.ttsSpeed = ev.target.value;
            this._saveSettings();
          });
          col.appendChild(speedSel);
          col.appendChild(el("div", { class: "mty-section-hint", style: "margin-top:4px" },
            "If the voice rushes, choose Slow. (Support varies by TTS engine.)"));
        }
        return col;
      })(),
    ));

    // Section 4: Entities
    wrap.appendChild(this._sectionCard(4, "Atmosphere Devices",
      "Optionally pick a few devices — motion sensors, lights, locks. " +
      "They're woven into the narration for flavour. The mystery and its " +
      "clues are written entirely by the AI; these just set the scene.",
      this._renderEntityPicker(),
    ));

    // Section 5: Dramatic Lighting
    wrap.appendChild(this._sectionCard(5, "Dramatic Lighting (optional)",
      "Pick lights to set the mood — they shift colour with each act and " +
      "flash on big moments. A light group works fine (e.g. your Hue room); " +
      "so do individual bulbs.",
      this._renderLightPicker(),
    ));

    // Section 6: Game settings
    wrap.appendChild(this._sectionCard(6, "Game Settings", "",
      el("div", { class: "mty-section-row" }, [
        el("div", { class: "mty-section-col" }, [
          el("div", { class: "mty-label" }, "Difficulty"),
          (() => {
            const sel = el("select", { class: "mty-input" });
            for (const d of ["easy", "medium", "hard"]) {
              const opt = el("option", { value: d }, d.charAt(0).toUpperCase() + d.slice(1));
              if (d === s.difficulty) opt.selected = true;
              sel.appendChild(opt);
            }
            sel.addEventListener("change", (ev) => {
              s.difficulty = ev.target.value;
              this._saveSettings();
            });
            return sel;
          })(),
        ]),
        el("div", { class: "mty-section-col" }, [
          el("div", { class: "mty-label" }, "Suspects"),
          (() => {
            const sel = el("select", { class: "mty-input" });
            for (let i = 3; i <= 8; i++) {
              const opt = el("option", { value: String(i) }, `${i} suspects`);
              if (i === s.suspectCount) opt.selected = true;
              sel.appendChild(opt);
            }
            sel.addEventListener("change", (ev) => {
              s.suspectCount = Number(ev.target.value);
              this._saveSettings();
            });
            return sel;
          })(),
        ]),
      ]),
    ));

    // Begin button
    const ready = !!s.selectedAgent;
    const launchRow = el("div", { class: "mty-section-launch" }, [
      el("button", {
        class: "mty-btn mty-btn-primary",
        disabled: !ready,
        onClick: () => this._createAndStart(),
      }, "🔪  Begin the Mystery"),
      el("div", { class: "mty-hint" },
        !s.selectedAgent ? "Choose an AI agent" :
        `Ready — ${s.suspectCount} suspects` +
        (s.selectedEntities.size ? `, ${s.selectedEntities.size} atmosphere devices` : "") +
        (s.lightsEnabled && s.selectedLights.size ? `, ${s.selectedLights.size} lights` : "")),
    ]);
    wrap.appendChild(launchRow);

    return wrap;
  }

  _renderEntityPicker() {
    const s = this._state;
    const wrap = el("div");

    // Search + toolbar
    const search = el("input", {
      type: "text", class: "mty-entity-search",
      placeholder: "Search entities…",
    });
    let filter = "";
    search.addEventListener("input", () => {
      filter = search.value.toLowerCase();
      renderList();
    });
    wrap.appendChild(search);

    const toolbar = el("div", { class: "mty-entity-toolbar" }, [
      el("button", {
        class: "mty-btn mty-btn-ghost",
        onClick: () => {
          for (const e of s.entities) if (e.domain === "binary_sensor") s.selectedEntities.add(e.entity_id);
          this._saveSettings();
          renderList();
        },
      }, "Motion sensors"),
      el("button", {
        class: "mty-btn mty-btn-ghost",
        onClick: () => {
          for (const e of s.entities) if (e.domain === "light") s.selectedEntities.add(e.entity_id);
          this._saveSettings();
          renderList();
        },
      }, "Lights"),
      el("button", {
        class: "mty-btn mty-btn-ghost",
        onClick: () => {
          for (const e of s.entities) if (e.domain === "lock") s.selectedEntities.add(e.entity_id);
          this._saveSettings();
          renderList();
        },
      }, "Locks"),
      el("button", {
        class: "mty-btn mty-btn-ghost",
        onClick: () => {
          s.selectedEntities.clear();
          this._saveSettings();
          renderList();
        },
      }, "Clear all"),
    ]);
    wrap.appendChild(toolbar);

    const count = el("div", { class: "mty-entity-count" });
    wrap.appendChild(count);

    const listHost = el("div");
    wrap.appendChild(listHost);

    const renderList = () => {
      count.textContent = `${s.selectedEntities.size} entities selected`;
      listHost.innerHTML = "";
      const filtered = filter
        ? s.entities.filter(e =>
            e.name.toLowerCase().includes(filter) || e.entity_id.includes(filter))
        : s.entities;
      if (!filtered.length) {
        listHost.appendChild(el("div", { class: "mty-empty" }, "No matching entities."));
        return;
      }
      const byArea = {};
      for (const e of filtered) {
        const a = e.area || "Unknown Room";
        (byArea[a] || (byArea[a] = [])).push(e);
      }
      for (const [area, list] of Object.entries(byArea)) {
        const areaBlock = el("div", { class: "mty-entity-area" }, [
          el("div", { class: "mty-entity-area-name" }, area),
          el("div", { class: "mty-entity-list" },
            list.map(e => {
              const checked = s.selectedEntities.has(e.entity_id);
              const row = el("div", {
                class: "mty-entity-row" + (checked ? " mty-entity-row-selected" : ""),
                onClick: () => {
                  if (s.selectedEntities.has(e.entity_id)) s.selectedEntities.delete(e.entity_id);
                  else s.selectedEntities.add(e.entity_id);
                  this._saveSettings();
                  renderList();
                },
              }, [
                el("div", { class: "mty-entity-check" }, checked ? "✓" : ""),
                el("div", { class: "mty-entity-row-body" }, [
                  el("div", { class: "mty-entity-row-name" }, e.name),
                  el("div", { class: "mty-entity-row-meta" }, `${e.domain} · ${e.state}${e.unit ? " " + e.unit : ""}`),
                ]),
              ]);
              return row;
            }),
          ),
        ]);
        listHost.appendChild(areaBlock);
      }
    };
    renderList();
    return wrap;
  }

  _renderLightPicker() {
    const s = this._state;
    const wrap = el("div");

    // Master enable toggle.
    const toggleRow = el("div", { class: "mty-light-toggle" }, [
      el("label", { class: "mty-light-toggle-label" }, [
        (() => {
          const cb = el("input", { type: "checkbox" });
          cb.checked = !!s.lightsEnabled;
          cb.addEventListener("change", () => {
            s.lightsEnabled = cb.checked;
            this._saveSettings();
            renderList();
          });
          return cb;
        })(),
        document.createTextNode(" Enable lighting effects"),
      ]),
    ]);
    wrap.appendChild(toggleRow);

    const listHost = el("div");
    wrap.appendChild(listHost);

    const renderList = () => {
      listHost.innerHTML = "";
      if (!s.lightsEnabled) {
        listHost.appendChild(el("div", { class: "mty-section-hint" }, "Lighting effects are off."));
        return;
      }
      if (!s.lights.length) {
        listHost.appendChild(el("div", { class: "mty-empty" }, "No light entities found."));
        return;
      }
      listHost.appendChild(el("div", { class: "mty-entity-count" },
        `${s.selectedLights.size} lights selected`));
      const byArea = {};
      for (const l of s.lights) {
        const a = l.area || "Unknown Room";
        (byArea[a] || (byArea[a] = [])).push(l);
      }
      for (const [area, list] of Object.entries(byArea)) {
        listHost.appendChild(el("div", { class: "mty-entity-area" }, [
          el("div", { class: "mty-entity-area-name" }, area),
          el("div", { class: "mty-entity-list" },
            list.map(l => {
              const checked = s.selectedLights.has(l.entity_id);
              return el("div", {
                class: "mty-entity-row" + (checked ? " mty-entity-row-selected" : ""),
                onClick: () => {
                  if (s.selectedLights.has(l.entity_id)) s.selectedLights.delete(l.entity_id);
                  else s.selectedLights.add(l.entity_id);
                  this._saveSettings();
                  renderList();
                },
              }, [
                el("div", { class: "mty-entity-check" }, checked ? "✓" : ""),
                el("div", { class: "mty-entity-row-body" }, [
                  el("div", { class: "mty-entity-row-name" },
                    l.name + (l.is_group ? "  ·  group" : "")),
                  el("div", { class: "mty-entity-row-meta" }, `light · ${l.state}`),
                ]),
              ]);
            }),
          ),
        ]));
      }
    };
    renderList();
    return wrap;
  }

  _sectionCard(num, title, hint, content) {
    return el("div", { class: "mty-section-card" }, [
      el("div", { class: "mty-section-header" }, [
        el("div", { class: "mty-section-num" }, String(num)),
        el("div", {}, [
          el("div", { class: "mty-section-title" }, title),
          hint ? el("div", { class: "mty-section-hint" }, hint) : null,
        ]),
      ]),
      content,
    ]);
  }

  _renderGame() {
    const s = this._state;
    const game = s.session?.game || {};
    const story = game.story || {};
    const suspects = game.suspects || [];
    const players = game.players || [];

    const wrap = el("div", { class: "mty-admin-main" });

    // QR + join code
    wrap.appendChild(this._renderQrCard(s.session?.join_code));

    // Mystery header
    if (story.title) {
      wrap.appendChild(el("div", { class: "mty-card mty-card-hero" }, [
        el("div", { class: "mty-card-title" }, story.title),
        el("div", { class: "mty-card-meta" }, [
          el("span", {}, `Victim: ${story.victim_name || "—"}`),
          el("span", {}, `Scene: ${story.crime_scene || "—"}`),
          el("span", {}, `Time: ${story.time_of_death || "—"}`),
        ]),
      ]));
    }

    // Act track
    wrap.appendChild(this._renderActTrack(game.state));

    // Narration
    const narration = this._lastNarration;
    if (narration) {
      wrap.appendChild(el("div", { class: "mty-narration" }, narration));
    }

    // Players + suspects side-by-side
    const grid = el("div", { class: "mty-game-grid" }, [
      el("div", { class: "mty-card" }, [
        el("div", { class: "mty-card-title" }, `Players (${players.length})`),
        el("div", { class: "mty-player-list" }, players.length ?
          players.map(p => el("div", { class: "mty-player-row" }, [
            el("div", { class: "mty-player-avatar" }, (p.name || "?")[0].toUpperCase()),
            el("div", {}, [
              el("div", { class: "mty-player-row-name" }, p.name || "—"),
              p.role
                ? el("div", { class: "mty-player-row-role" }, `${p.role.emoji || ""} ${p.role.name || ""}`)
                : el("div", { class: "mty-player-row-role" }, "Observer"),
              p.has_accused ? el("div", { class: "mty-badge mty-badge-gold" }, "Accused") : null,
            ]),
          ]))
          : [el("div", { class: "mty-empty" }, "Waiting for players to scan the QR…")]
        ),
      ]),
      el("div", { class: "mty-card" }, [
        el("div", { class: "mty-card-title" }, "Suspects"),
        el("div", { class: "mty-suspect-list" }, suspects.length ?
          suspects.map(sp => el("div", { class: "mty-suspect-row" }, [
            el("div", { class: "mty-suspect-emoji" }, sp.role_emoji || "🎭"),
            el("div", {}, [
              el("div", { class: "mty-suspect-row-name" }, sp.role_name || sp.role_id),
              el("div", { class: "mty-suspect-row-player" }, sp.player ? `played by ${sp.player}` : "NPC"),
            ]),
          ]))
          : [el("div", { class: "mty-empty" }, "Story still generating…")]
        ),
      ]),
    ]);
    wrap.appendChild(grid);

    // Controls
    wrap.appendChild(el("div", { class: "mty-section-launch" }, [
      el("button", {
        class: "mty-btn mty-btn-primary",
        onClick: () => this._nextAct(),
      }, "▶  Advance Act"),
      el("button", {
        class: "mty-btn mty-btn-danger",
        onClick: () => this._endGame(),
      }, "✕  End"),
    ]));
    return wrap;
  }

  _renderAccusation() {
    const s = this._state;
    const game = s.session?.game || {};
    const players = game.players || [];
    const wrap = el("div", { class: "mty-admin-main" });

    wrap.appendChild(el("div", { class: "mty-card mty-card-hero" }, [
      el("div", { class: "mty-card-title" }, "The Accusation"),
      el("div", { class: "mty-card-meta" }, [el("span", {}, "Waiting for players to name the killer…")]),
    ]));

    wrap.appendChild(this._renderActTrack(game.state));
    if (this._lastNarration) {
      wrap.appendChild(el("div", { class: "mty-narration" }, this._lastNarration));
    }

    wrap.appendChild(el("div", { class: "mty-card" }, [
      el("div", { class: "mty-card-title" }, "Accusation Status"),
      el("div", { class: "mty-player-list" }, players.length ?
        players.map(p => el("div", { class: "mty-player-row" }, [
          el("div", { class: "mty-player-avatar" }, (p.name || "?")[0].toUpperCase()),
          el("div", {}, [
            el("div", { class: "mty-player-row-name" }, p.name || "—"),
            p.has_accused
              ? el("div", { class: "mty-badge mty-badge-gold" }, "✓ Accused")
              : el("div", { class: "mty-badge" }, "Pending"),
          ]),
        ]))
        : [el("div", { class: "mty-empty" }, "No players in this game.")]
      ),
    ]));

    wrap.appendChild(el("div", { class: "mty-section-launch" }, [
      el("button", {
        class: "mty-btn mty-btn-primary",
        onClick: () => this._revealKiller(),
      }, "⚖️  Reveal the Killer"),
    ]));
    return wrap;
  }

  _renderReveal() {
    const s = this._state;
    const game = s.session?.game || {};
    const story = game.story || {};
    const suspects = game.suspects || [];
    const players = (game.players || []).slice().sort((a, b) => (b.score || 0) - (a.score || 0));
    const killerId = story.killer_id;
    const killer = suspects.find(sp => sp.role_id === killerId);

    const wrap = el("div", { class: "mty-admin-main mty-reveal" });
    wrap.appendChild(el("div", { class: "mty-card mty-card-hero" }, [
      el("div", { class: "mty-card-title" }, "The Truth Revealed"),
    ]));

    if (story.reveal_narration) {
      wrap.appendChild(el("div", { class: "mty-narration" }, story.reveal_narration));
    }

    if (killer) {
      wrap.appendChild(el("div", { class: "mty-card mty-card-killer" }, [
        el("div", { class: "mty-killer-emoji" }, killer.role_emoji || "🎭"),
        el("div", { class: "mty-killer-name" }, killer.role_name || ""),
        el("div", { class: "mty-killer-player" }, killer.player ? `played by ${killer.player}` : ""),
        story.motive ? el("div", { class: "mty-killer-motive" }, story.motive) : null,
      ]));
    }

    wrap.appendChild(el("div", { class: "mty-card" }, [
      el("div", { class: "mty-card-title" }, "Final Scores"),
      el("div", { class: "mty-player-list" },
        players.map((p, i) => el("div", { class: "mty-player-row" }, [
          el("div", { class: "mty-player-avatar" }, ["🥇","🥈","🥉"][i] || String(i + 1)),
          el("div", {}, [
            el("div", { class: "mty-player-row-name" }, p.name || "—"),
            p.is_correct ? el("div", { class: "mty-badge mty-badge-gold" }, "Correct!") : null,
          ]),
          el("div", { class: "mty-player-row-score" }, `${p.score || 0} pts`),
        ])),
      ),
    ]));

    wrap.appendChild(el("div", { class: "mty-section-launch" }, [
      el("button", {
        class: "mty-btn mty-btn-primary",
        onClick: () => this._rematch(),
      }, "🔄  New Mystery"),
      el("button", {
        class: "mty-btn mty-btn-ghost",
        onClick: () => this._endGame(),
      }, "Done"),
    ]));
    return wrap;
  }

  _renderQrCard(joinCode) {
    if (!joinCode) return el("div");
    const joinUrl = `${window.location.protocol}//${window.location.host}/mortify/play?code=${joinCode}`;
    // Request the PNG at roughly the rendered size (capped) so it's crisp on a
    // big screen but never a giant download — and the CSS caps the box itself.
    const px = Math.min(360, Math.round((window.innerWidth || 400) * 0.5));
    const img = el("img", {
      class: "mty-qr-code-img",
      src: `/api/mortify/qr?data=${encodeURIComponent(joinUrl)}&size=${px}`,
      alt: "QR code to join the game",
    });
    return el("div", { class: "mty-card" }, [
      el("div", { class: "mty-card-title" }, "Join Code"),
      el("div", { class: "mty-card-meta" }, [el("span", {}, `Players scan this QR or visit the URL.`)]),
      el("div", { class: "mty-qr-box" }, [img]),
      el("div", { class: "mty-qr-url" }, [
        el("div", { class: "mty-qr-url-code" }, joinCode),
        el("div", {}, joinUrl),
      ]),
    ]);
  }

  _renderActTrack(state) {
    const stages = [
      { state: "act_1", label: "I · Discovery" },
      { state: "act_2", label: "II · Investigation" },
      { state: "act_3", label: "III · Shadows" },
      { state: "accusation", label: "IV · Accusation" },
      { state: "reveal", label: "V · Reveal" },
    ];
    return el("div", { class: "mty-act-track" },
      stages.map(stg => {
        const order = stages.map(s => s.state);
        const currentIdx = order.indexOf(state);
        const myIdx = order.indexOf(stg.state);
        let cls = "mty-act-step";
        if (myIdx < currentIdx) cls += " mty-act-step-done";
        else if (myIdx === currentIdx) cls += " mty-act-step-active";
        return el("div", { class: cls }, stg.label);
      }),
    );
  }

  _renderLoading() {
    return el("div", { class: "mty-loading" }, [
      el("div", { class: "mty-loading-inner" }, "🕯️"),
      el("div", { class: "mty-loading-sub" }, "Crafting your murder mystery…"),
    ]);
  }

  // We update _lastNarration whenever a narration event arrives. The
  // setter pattern makes it survive re-renders cheaply.
  get _lastNarration() { return this._narration || ""; }
  set _lastNarration(v) { this._narration = v; }
}

// Override _onEvent to capture narration text in a field.
const _origOnEvent = MortifyPanel.prototype._onEvent;
MortifyPanel.prototype._onEvent = function(msg) {
  if (msg.event === "narration" && typeof msg.message === "string") {
    this._lastNarration = msg.message;
  } else if (msg.event === "act_started" && typeof msg.narration === "string") {
    this._lastNarration = msg.narration;
  }
  return _origOnEvent.call(this, msg);
};

if (!customElements.get("mortify-panel")) {
  customElements.define("mortify-panel", MortifyPanel);
}

// ───────────────────────────────────────────────────────────────────────
// Player page boot
// ───────────────────────────────────────────────────────────────────────

function readJoinCodeFromUrl() {
  try {
    const params = new URLSearchParams(window.location.search);
    const code = (params.get("code") || "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 6);
    if (code) return code;
  } catch (e) {}
  return "";
}

class PlayerClient {
  constructor(root, initialJoinCode) {
    this.root = root;
    this.ws = null;
    this.state = {
      view: "join",          // join | lobby | game | accusation | reveal
      joinCode: initialJoinCode || "",
      name: "",
      playerId: null,
      sessionId: null,
      token: null,
      game: null,
      you: null,
      activeSuspectId: null,  // for the chat screen
      chatMessages: {},        // role_id -> [{from, text}]
      error: null,
      sending: false,
      // When this player is secretly the killer and another player questions
      // them, the server sends a prompt with evasion options to choose from.
      killerPrompt: null,      // {request_id, asker_name, question, options}
      killerBanner: null,      // transient "you are the killer" reminder
    };
    // Try to resume from localStorage
    try {
      const saved = JSON.parse(localStorage.getItem("mortify_player") || "{}");
      if (saved.session_id && saved.player_token) {
        this.state.sessionId = saved.session_id;
        this.state.token = saved.player_token;
        this.state.name = saved.name || "";
      }
    } catch (e) {}
    this.connect();
    this.render();
  }

  connect() {
    if (this.ws) {
      try { this.ws.close(); } catch (e) {}
    }
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    this.ws = new WebSocket(`${proto}//${window.location.host}${PLAYER_WS_PATH}`);
    this.ws.addEventListener("open", () => {
      // If we have a saved token, try to resume.
      if (this.state.token && this.state.sessionId) {
        this.send({
          type: "resume",
          session_id: this.state.sessionId,
          player_token: this.state.token,
        });
      }
    });
    this.ws.addEventListener("message", (ev) => this._onMessage(ev));
    this.ws.addEventListener("close", () => {
      // Reconnect after a short delay.
      setTimeout(() => this.connect(), 2000);
    });
    this.ws.addEventListener("error", () => {
      // Let the close handler do the reconnect.
    });
  }

  send(payload) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try {
      this.ws.send(JSON.stringify(payload));
      return true;
    } catch (e) {
      return false;
    }
  }

  _onMessage(ev) {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    const event = data.event;

    if (event === "joined" || event === "resumed") {
      this.state.playerId = data.player_id;
      this.state.sessionId = data.session_id;
      this.state.token = data.player_token;
      this.state.name = data.name;
      this.state.game = data.game;
      this.state.you = data.you;
      this.state.error = null;
      // Persist for cross-reload identity
      try {
        localStorage.setItem("mortify_player", JSON.stringify({
          session_id: data.session_id,
          player_token: data.player_token,
          name: data.name,
        }));
      } catch (e) {}
      this._updateView();
      this.render();
      return;
    }

    if (event === "error") {
      // If we can't resume, clear the stale token and go back to join.
      if (data.code === "invalid_token" || data.code === "not_found") {
        try { localStorage.removeItem("mortify_player"); } catch (e) {}
        this.state.token = null;
        this.state.sessionId = null;
        this.state.playerId = null;
      }
      this.state.error = data.message || "Something went wrong.";
      this.state.sending = false;
      this.render();
      return;
    }

    // Public broadcast events all carry game/you snapshots.
    if (data.game) this.state.game = data.game;
    if (data.you) this.state.you = data.you;
    this.state.error = null;

    if (event === "clue_result") {
      // Player re-opened a clue they've unlocked — show its full text.
      showToast(this.root, `${data.title || "Clue"}: ${data.clue_text || "(unknown)"}`);
    } else if (event === "interrogation_result") {
      const rid = data.suspect_role_id;
      if (!this.state.chatMessages[rid]) this.state.chatMessages[rid] = [];
      this.state.chatMessages[rid].push({ from: "you", text: data.question });
      this.state.chatMessages[rid].push({ from: "suspect", text: data.reply });
      if (data.unlocked_clue) {
        this.state.chatMessages[rid].push({
          from: "clue",
          text: `🔍 New clue uncovered — ${data.unlocked_clue.title}: ${data.unlocked_clue.clue_text}`,
        });
        showToast(this.root, `Clue uncovered: ${data.unlocked_clue.title}`);
      } else if (data.points_awarded > 0) {
        showToast(this.root, `Good question (+${data.points_awarded})`);
      }
      this.state.sending = false;
    } else if (event === "confront_result") {
      const rid = data.suspect_role_id;
      if (!this.state.chatMessages[rid]) this.state.chatMessages[rid] = [];
      this.state.chatMessages[rid].push({ from: "you", text: "⚡ I confront you with the evidence." });
      this.state.chatMessages[rid].push({ from: "suspect", text: data.reply });
      const verdict = data.outcome === "nailed"
        ? `You broke them! (+${data.points_awarded})`
        : data.outcome === "plausible"
        ? "They squirm, but hold."
        : `A baseless accusation (${data.points_awarded}).`;
      this.state.chatMessages[rid].push({ from: "clue", text: `⚡ ${verdict}` });
      showToast(this.root, verdict);
      this.state.sending = false;
    } else if (event === "accuse_result") {
      this.state.sending = false;
    } else if (event === "killer_prompt") {
      // This player IS the killer and is being questioned. Surface the
      // evasion options as a modal-style overlay so they can pick a reply.
      this.state.killerPrompt = {
        request_id: data.request_id,
        asker_name: data.asker_name || "A detective",
        question: data.question || "",
        options: data.options || [],
        suspect_role_id: data.suspect_role_id,
      };
    } else if (event === "killer_respond_result") {
      // Our pick was accepted — clear the prompt.
      this.state.killerPrompt = null;
    } else if (event === "awaiting_killer") {
      // We questioned a human-played suspect; let us know they're "thinking".
      const rid = data.suspect_role_id;
      if (rid && this.state.activeSuspectId === rid) {
        showToast(this.root, "The suspect considers your question…");
      }
    } else if (event === "act_auto_advanced") {
      // The investigation moved itself forward. Make it feel like an event.
      if (data.reason) showToast(this.root, `📢 ${data.reason}`);
    } else if (event === "revealed") {
      // Force the view to reveal.
      this.state.view = "reveal";
      this.state.killerPrompt = null;
    }

    this._updateView();
    this.render();
  }

  _updateView() {
    const g = this.state.game;
    if (!g) {
      if (this.state.sessionId && this.state.playerId) this.state.view = "lobby";
      else this.state.view = "join";
      return;
    }
    const st = g.state;
    if (st === "lobby" || st === "generating") this.state.view = "lobby";
    else if (st === "accusation") this.state.view = "accusation";
    else if (st === "reveal" || st === "ended") this.state.view = "reveal";
    else if (st && st.startsWith("act_")) this.state.view = "game";
  }

  // ── actions ────────────────────────────────────────────────────────

  doJoin(name) {
    const trimmed = (name || "").trim();
    if (!trimmed) {
      this.state.error = "Pick a name first.";
      this.render();
      return;
    }
    if (!this.state.joinCode) {
      this.state.error = "Missing join code — open the QR link.";
      this.render();
      return;
    }
    this.send({ type: "join", join_code: this.state.joinCode, name: trimmed });
  }

  doDiscover(clueId) {
    this.send({ type: "discover_clue", clue_id: clueId });
  }

  doInterrogate(suspectId, question) {
    if (!question.trim()) return;
    this.state.sending = true;
    this.render();
    this.send({
      type: "interrogate",
      suspect_role_id: suspectId,
      question: question.trim(),
    });
  }

  doConfront(suspectId, clueId) {
    if (!confirm("Confront with this clue? You only get ONE confrontation all game — make it count.")) return;
    this.state.sending = true;
    this.render();
    this.send({
      type: "confront",
      suspect_role_id: suspectId,
      clue_id: clueId,
    });
  }

  doAccuse(roleId) {
    if (!confirm("Accuse this suspect? You cannot change your mind.")) return;
    this.state.sending = true;
    this.send({ type: "accuse", accused_role_id: roleId });
  }

  // ── rendering ──────────────────────────────────────────────────────

  render() {
    // Incremental path: if we're sitting in an active chat and the chat DOM
    // is already mounted for the same suspect, DON'T tear the page down — a
    // full rebuild on every WebSocket broadcast was wiping whatever the
    // player had half-typed (and dropping their keyboard focus). Instead we
    // surgically update just the message log and the dynamic controls,
    // leaving the <input> (and its value + caret) untouched.
    const inChat = this.state.view === "game" && this.state.activeSuspectId;
    if (
      inChat &&
      !this.state.killerPrompt &&
      this._chatDom &&
      this._chatDom.suspectId === this.state.activeSuspectId &&
      this.root.contains(this._chatDom.screen)
    ) {
      this._updateChat();
      this._renderError();
      this._renderKillerOverlay();
      return;
    }

    // Full rebuild for everything else (or first entry into a chat).
    this._chatDom = null;
    this.root.innerHTML = "";
    const wrap = el("div", { class: "mty-player" });
    this.root.appendChild(wrap);

    if (this.state.view === "join") {
      wrap.appendChild(this._renderJoin());
    } else if (this.state.view === "lobby") {
      wrap.appendChild(this._renderLobby());
    } else if (this.state.view === "accusation") {
      wrap.appendChild(this._renderAccusation());
    } else if (this.state.view === "reveal") {
      wrap.appendChild(this._renderReveal());
    } else {
      wrap.appendChild(this._renderGame());
    }

    this._renderError();
    this._renderKillerOverlay();
  }

  _renderKillerOverlay() {
    // Remove any existing overlay first.
    const existing = this.root.querySelector(".mty-killer-overlay");
    if (existing) existing.remove();
    const kp = this.state.killerPrompt;
    if (!kp) return;
    const overlay = el("div", { class: "mty-killer-overlay" }, [
      el("div", { class: "mty-killer-modal" }, [
        el("div", { class: "mty-killer-tag" }, "🔪 You are the killer"),
        el("div", { class: "mty-killer-q-label" }, `${kp.asker_name} asks:`),
        el("div", { class: "mty-killer-q" }, `“${kp.question}”`),
        el("div", { class: "mty-killer-hint" }, "Choose how to deflect — don't get caught."),
        el("div", { class: "mty-killer-options" },
          (kp.options || []).map((opt, i) =>
            el("button", {
              class: "mty-killer-option",
              onClick: () => this.doKillerRespond(kp.request_id, i),
            }, opt),
          ),
        ),
      ]),
    ]);
    this.root.appendChild(overlay);
  }

  doKillerRespond(requestId, choiceIndex) {
    this.send({ type: "killer_respond", request_id: requestId, choice_index: choiceIndex });
    // Optimistically clear so the overlay doesn't linger if the broadcast lags.
    this.state.killerPrompt = null;
    this.render();
  }

  _renderError() {
    if (!this.state.error) return;
    const wrap = this.root.querySelector(".mty-player") || this.root;
    const errBox = el("div", { class: "mty-toast mty-toast-error mty-toast-show" }, this.state.error);
    wrap.appendChild(errBox);
    setTimeout(() => {
      if (this.state.error) {
        this.state.error = null;
        this.render();
      }
    }, 4000);
  }

  _renderJoin() {
    const wrap = el("div", { class: "mty-join-form" });
    wrap.appendChild(el("div", { class: "mty-player-logo" }, "🔪 Mortify"));
    wrap.appendChild(el("div", { class: "mty-player-tagline" }, "Someone is dead. You are all suspects."));

    wrap.appendChild(el("div", { class: "mty-label" }, "Join code"));
    const codeInput = el("input", {
      class: "mty-input", maxlength: 6,
      placeholder: "ABCDEF",
      autocomplete: "off", autocorrect: "off", autocapitalize: "characters",
      value: this.state.joinCode,
    });
    codeInput.addEventListener("input", () => {
      this.state.joinCode = codeInput.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 6);
      codeInput.value = this.state.joinCode;
    });
    wrap.appendChild(codeInput);

    wrap.appendChild(el("div", { class: "mty-label" }, "Your name"));
    const nameInput = el("input", {
      class: "mty-input", maxlength: 20,
      placeholder: "Detective…",
      autocomplete: "off", autocorrect: "off",
      value: this.state.name,
    });
    wrap.appendChild(nameInput);

    wrap.appendChild(el("button", {
      class: "mty-btn mty-btn-primary",
      onClick: () => this.doJoin(nameInput.value),
    }, "Enter the Scene →"));

    return wrap;
  }

  _renderLobby() {
    const game = this.state.game || {};
    const you = this.state.you || {};
    const isGenerating = game.state === "generating";
    const wrap = el("div");

    wrap.appendChild(this._renderPlayerHeader());

    wrap.appendChild(el("div", { class: "mty-card mty-card-hero" }, [
      el("div", { class: "mty-card-title" }, isGenerating ? "Crafting your mystery…" : "The Lobby"),
      el("div", { class: "mty-card-meta" }, [
        el("span", {}, isGenerating
          ? "Wait while the AI writes a unique mystery for tonight."
          : "Wait for the host to start the game."),
      ]),
    ]));

    if (isGenerating) {
      wrap.appendChild(el("div", { class: "mty-narration mty-pulse" },
        "The detective takes off her coat. The candles are lit. Someone is about to die…"));
    }

    const players = game.players || [];
    wrap.appendChild(el("div", { class: "mty-card" }, [
      el("div", { class: "mty-card-title" }, `Players (${players.length})`),
      el("div", { class: "mty-player-list" }, players.length ?
        players.map(p => el("div", { class: "mty-player-row" }, [
          el("div", { class: "mty-player-avatar" }, (p.name || "?")[0].toUpperCase()),
          el("div", {}, [
            el("div", { class: "mty-player-row-name" }, p.name + (p.player_id === you.player_id ? " (you)" : "")),
          ]),
        ]))
        : [el("div", { class: "mty-empty" }, "You're the first to join.")]
      ),
    ]));

    return wrap;
  }

  _renderGame() {
    if (this.state.activeSuspectId) return this._renderChat();
    const game = this.state.game || {};
    const you = this.state.you || {};
    const wrap = el("div");

    wrap.appendChild(this._renderPlayerHeader());

    // Your role card
    if (you.role) {
      const roleCard = el("div", { class: "mty-role-card" }, [
        el("div", { class: "mty-role-card-emoji" }, you.role.emoji || "🎭"),
        el("div", {}, [
          el("div", { class: "mty-label" }, "Your character"),
          el("div", { class: "mty-role-card-name" }, you.role.name || ""),
          el("div", { class: "mty-role-card-desc" }, you.role.description || ""),
        ]),
      ]);
      wrap.appendChild(roleCard);
      // Secret reminder for the human killer (only they ever see this).
      if (you.is_killer) {
        wrap.appendChild(el("div", { class: "mty-killer-banner" },
          "🔪 Secret: YOU are the killer. When others question you, you'll choose how to deflect. Don't get caught."));
      }
    } else {
      wrap.appendChild(el("div", { class: "mty-role-card" }, [
        el("div", { class: "mty-role-card-emoji" }, "🕵️"),
        el("div", {}, [
          el("div", { class: "mty-label" }, "Your role"),
          el("div", { class: "mty-role-card-name" }, "Observer"),
          el("div", { class: "mty-role-card-desc" }, "No assigned role. Investigate freely."),
        ]),
      ]));
    }

    // Story summary
    if (game.story?.title) {
      wrap.appendChild(el("div", { class: "mty-card mty-card-hero" }, [
        el("div", { class: "mty-card-title" }, game.story.title),
        el("div", { class: "mty-card-meta" }, [
          el("span", {}, `Victim: ${game.story.victim_name || "—"}`),
          el("span", {}, `Scene: ${game.story.crime_scene || "—"}`),
        ]),
      ]));
    }

    // Tabs: Suspects | Clues
    const tab = this._tab || "suspects";
    wrap.appendChild(el("div", { class: "mty-tab-bar" }, [
      el("div", {
        class: "mty-tab" + (tab === "suspects" ? " mty-tab-active" : ""),
        onClick: () => { this._tab = "suspects"; this.render(); },
      }, "Interrogate"),
      el("div", {
        class: "mty-tab" + (tab === "clues" ? " mty-tab-active" : ""),
        onClick: () => { this._tab = "clues"; this.render(); },
      }, "Clues"),
    ]));

    if (tab === "suspects") {
      const suspects = game.suspects || [];
      wrap.appendChild(el("div", { class: "mty-suspect-list" },
        suspects.map(sp => el("div", {
          class: "mty-suspect-row mty-suspect-row-tap",
          onClick: () => { this.state.activeSuspectId = sp.role_id; this.render(); },
        }, [
          el("div", { class: "mty-suspect-emoji" }, sp.role_emoji || "🎭"),
          el("div", {}, [
            el("div", { class: "mty-suspect-row-name" }, sp.role_name || sp.role_id),
            el("div", { class: "mty-suspect-row-player" }, sp.alibi ? `Alibi: ${sp.alibi}` : ""),
          ]),
          el("div", { class: "mty-suspect-row-cta" }, "→"),
        ])),
      ));
    } else {
      // Clues tab. The server sends an opaque clue list during play (ids +
      // {locked:true}) so locked clues reveal NOTHING — no title, no "ask the
      // Butler" hint that would short-circuit the deduction. The full text of
      // clues THIS player has unlocked rides on you.unlocked_clue_details.
      const total = game.clue_total ?? (game.clues || []).length;
      const mine = you.unlocked_clue_details || [];
      const mineById = new Map(mine.map(c => [c.id, c]));
      const lockedCount = Math.max(0, total - mine.length);
      const suspects = game.suspects || [];
      const suspectName = (rid) => {
        const s = suspects.find(x => x.role_id === rid);
        return s ? `${s.role_emoji || "🎭"} ${s.role_name || rid}` : "a suspect";
      };
      if (!total) {
        wrap.appendChild(el("div", { class: "mty-empty" }, "No clues yet — the mystery is still being written."));
      } else {
        wrap.appendChild(el("div", { class: "mty-clue-hint" },
          `${mine.length} of ${total} clues uncovered. Question the suspects to draw them out.`));
        const list = el("div", { class: "mty-clue-list" });
        // Unlocked clues first, each tappable to expand the full text.
        for (const c of mine) {
          const expanded = this._expandedClue === c.id;
          const row = el("div", {
            class: "mty-clue-row mty-clue-row-found" + (expanded ? " mty-clue-row-open" : ""),
            onClick: () => {
              this._expandedClue = expanded ? null : c.id;
              this.render();
            },
          }, [
            el("div", { class: "mty-clue-row-icon" }, "🔓"),
            el("div", { class: "mty-clue-row-body" }, [
              el("div", { class: "mty-clue-row-name" }, c.title || "A clue"),
              el("div", { class: "mty-clue-row-area" },
                `Drawn out of ${suspectName(c.held_by)}`),
              expanded
                ? el("div", { class: "mty-clue-row-text" }, c.text || "")
                : el("div", { class: "mty-clue-row-tap-hint" }, "Tap to read"),
            ]),
          ]);
          list.appendChild(row);
        }
        // Then anonymous locked slots — just a count of what's still out there.
        for (let i = 0; i < lockedCount; i++) {
          list.appendChild(el("div", { class: "mty-clue-row mty-clue-row-locked" }, [
            el("div", { class: "mty-clue-row-icon" }, "🔒"),
            el("div", { class: "mty-clue-row-body" }, [
              el("div", { class: "mty-clue-row-name" }, "Undiscovered clue"),
              el("div", { class: "mty-clue-row-area" }, "Interrogate the suspects to uncover it."),
            ]),
          ]));
        }
        wrap.appendChild(list);
      }
    }
    return wrap;
  }

  _renderChat() {
    const game = this.state.game || {};
    const sid = this.state.activeSuspectId;
    const suspect = (game.suspects || []).find(s => s.role_id === sid);
    if (!suspect) {
      this.state.activeSuspectId = null;
      return this._renderGame();
    }

    const screen = el("div", { class: "mty-chat-screen" });
    screen.appendChild(el("div", { class: "mty-chat-suspect" }, [
      el("button", {
        class: "mty-chat-back",
        onClick: () => {
          this.state.activeSuspectId = null;
          this._chatDom = null;
          this.render();
        },
      }, "← Back"),
      el("div", { class: "mty-suspect-emoji" }, suspect.role_emoji || "🎭"),
      el("div", {}, [
        el("div", { class: "mty-suspect-row-name" }, suspect.role_name || sid),
        // NOTE: deliberately NOT showing who plays this suspect — the cast is
        // anonymous during play so detectives (and the killer) stay hidden.
        el("div", { class: "mty-suspect-row-player" },
          suspect.alibi ? `“${suspect.alibi}”` : ""),
      ]),
    ]));

    // The message log — rebuilt incrementally by _updateChat so we never
    // disturb the input below it.
    const log = el("div", { class: "mty-chat-log" });
    screen.appendChild(log);

    // The input. Created ONCE and preserved across WebSocket broadcasts so a
    // half-typed question (and the keyboard focus) survives a re-render.
    const input = el("input", {
      class: "mty-chat-input",
      type: "text",
      placeholder: "Ask the suspect…",
      maxlength: 500,
    });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        const v = input.value;
        if (!v.trim()) return;
        input.value = "";
        this.doInterrogate(sid, v);
      }
    });
    screen.appendChild(input);

    // A dynamic controls host (confront bar / spent notice) — also refreshed
    // by _updateChat without touching the input.
    const controls = el("div", { class: "mty-chat-controls" });
    screen.appendChild(controls);

    this._chatDom = { suspectId: sid, screen, log, input, controls };
    this._updateChat();
    setTimeout(() => { try { input.focus(); } catch (e) {} }, 50);
    return screen;
  }

  _updateChat() {
    const dom = this._chatDom;
    if (!dom) return;
    const sid = dom.suspectId;
    const messages = this.state.chatMessages[sid] || [];

    // Rebuild the log.
    dom.log.innerHTML = "";
    if (!messages.length) {
      dom.log.appendChild(el("div", { class: "mty-empty" },
        "Ask them anything. Press them. Watch their eyes."));
    } else {
      for (const m of messages) {
        let cls = "mty-msg-suspect";
        if (m.from === "you") cls = "mty-msg-you";
        else if (m.from === "clue") cls = "mty-msg-clue";
        dom.log.appendChild(el("div", { class: "mty-msg " + cls }, m.text));
      }
    }
    if (this.state.sending) {
      dom.log.appendChild(el("div", { class: "mty-msg mty-msg-suspect mty-pulse" }, "…thinking…"));
    }

    // Input enabled/disabled tracks the sending flag without recreating it.
    dom.input.disabled = !!this.state.sending;

    // Rebuild the confront controls. The public clue list is opaque now, so
    // we offer the player's OWN unlocked clues (with real titles) to throw.
    dom.controls.innerHTML = "";
    const you = this.state.you || {};
    const mine = you.unlocked_clue_details || [];
    const alreadyConfronted = you.has_confronted;
    if (mine.length && !alreadyConfronted) {
      const bar = el("div", { class: "mty-confront-bar" });
      bar.appendChild(el("div", { class: "mty-confront-label" },
        "⚡ Confront with a clue (one shot all game):"));
      const select = el("select", { class: "mty-confront-select" });
      select.appendChild(el("option", { value: "" }, "Choose a clue…"));
      for (const c of mine) {
        select.appendChild(el("option", { value: c.id }, c.title || c.id));
      }
      const btn = el("button", {
        class: "mty-confront-btn",
        disabled: !!this.state.sending,
        onClick: () => {
          if (!select.value) { showToast(this.root, "Pick a clue first."); return; }
          this.doConfront(dom.suspectId, select.value);
        },
      }, "Confront");
      bar.appendChild(select);
      bar.appendChild(btn);
      dom.controls.appendChild(bar);
    } else if (alreadyConfronted) {
      dom.controls.appendChild(el("div", { class: "mty-confront-spent" },
        "You've used your one confrontation."));
    }

    setTimeout(() => { dom.log.scrollTop = dom.log.scrollHeight; }, 0);
  }

  _renderAccusation() {
    const game = this.state.game || {};
    const you = this.state.you || {};
    const wrap = el("div");
    wrap.appendChild(this._renderPlayerHeader());

    wrap.appendChild(el("div", { class: "mty-card mty-card-hero" }, [
      el("div", { class: "mty-card-title" }, "The Accusation"),
      el("div", { class: "mty-card-meta" }, [
        el("span", {}, you.accusation
          ? "Your accusation has been submitted. Awaiting the others…"
          : "Name the killer. You cannot change your mind."),
      ]),
    ]));

    if (!you.accusation) {
      wrap.appendChild(el("div", { class: "mty-accuse-list" },
        (game.suspects || []).map(sp => el("div", {
          class: "mty-accuse-row",
          onClick: () => this.doAccuse(sp.role_id),
        }, [
          el("div", { class: "mty-suspect-emoji" }, sp.role_emoji || "🎭"),
          el("div", {}, [
            el("div", { class: "mty-suspect-row-name" }, sp.role_name || sp.role_id),
            el("div", { class: "mty-suspect-row-player" }, sp.player ? `played by ${sp.player}` : "NPC"),
          ]),
          el("div", { class: "mty-suspect-row-cta" }, "Accuse →"),
        ])),
      ));
    } else {
      const accused = (game.suspects || []).find(s => s.role_id === you.accusation);
      wrap.appendChild(el("div", { class: "mty-card" }, [
        el("div", { class: "mty-card-title" }, "Your accusation"),
        accused ? el("div", { class: "mty-suspect-row" }, [
          el("div", { class: "mty-suspect-emoji" }, accused.role_emoji || "🎭"),
          el("div", {}, [
            el("div", { class: "mty-suspect-row-name" }, accused.role_name || you.accusation),
          ]),
        ]) : null,
      ]));
    }

    return wrap;
  }

  _renderReveal() {
    const game = this.state.game || {};
    const story = game.story || {};
    const suspects = game.suspects || [];
    const players = (game.players || []).slice().sort((a, b) => (b.score || 0) - (a.score || 0));
    const you = this.state.you || {};
    const killer = suspects.find(sp => sp.role_id === story.killer_id);

    const wrap = el("div");
    wrap.appendChild(this._renderPlayerHeader());

    wrap.appendChild(el("div", { class: "mty-card mty-card-hero" }, [
      el("div", { class: "mty-card-title" },
        you.is_correct ? "You named the killer!" : "Justice is served"),
      el("div", { class: "mty-card-meta" }, [
        el("span", {}, you.is_correct ? "Correct accusation." : "The truth was harder than it looked."),
      ]),
    ]));

    if (story.reveal_narration) {
      wrap.appendChild(el("div", { class: "mty-narration" }, story.reveal_narration));
    }

    if (killer) {
      wrap.appendChild(el("div", { class: "mty-card mty-card-killer" }, [
        el("div", { class: "mty-killer-emoji" }, killer.role_emoji || "🎭"),
        el("div", { class: "mty-killer-name" }, killer.role_name || ""),
        el("div", { class: "mty-killer-player" }, killer.player ? `played by ${killer.player}` : ""),
        story.motive ? el("div", { class: "mty-killer-motive" }, story.motive) : null,
      ]));
    }

    wrap.appendChild(el("div", { class: "mty-card" }, [
      el("div", { class: "mty-card-title" }, "Final Scores"),
      el("div", { class: "mty-player-list" },
        players.map((p, i) => el("div", { class: "mty-player-row" }, [
          el("div", { class: "mty-player-avatar" }, ["🥇","🥈","🥉"][i] || String(i + 1)),
          el("div", {}, [
            el("div", { class: "mty-player-row-name" }, p.name + (p.player_id === you.player_id ? " (you)" : "")),
            p.is_correct ? el("div", { class: "mty-badge mty-badge-gold" }, "Correct!") : null,
          ]),
          el("div", { class: "mty-player-row-score" }, `${p.score || 0} pts`),
        ])),
      ),
    ]));

    return wrap;
  }

  _renderPlayerHeader() {
    const you = this.state.you || {};
    const score = you.score || 0;
    const cluesCount = (you.clues_found || []).length;
    return el("div", { class: "mty-player-header" }, [
      el("div", { class: "mty-player-logo" }, "🔪 Mortify"),
      el("div", { class: "mty-status-bar" }, [
        el("span", { class: "mty-badge" }, `${cluesCount} clues`),
        el("span", { class: "mty-badge" }, `${score} pts`),
      ]),
    ]);
  }
}

function bootPlayerPage() {
  const root = document.getElementById("mortify-root");
  if (!root || root.dataset.view !== "player") return;
  // CSS is loaded by player.html via <link>.
  new PlayerClient(root, readJoinCodeFromUrl());
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootPlayerPage);
  } else {
    bootPlayerPage();
  }
}

"""LLM client for Mortify — uses HA's built-in conversation agents (Ollama, etc.)

Instead of making raw HTTP calls to Ollama, we call conversation.process on
whichever conversation entity the user selects. This means:
- No URL/port config needed — HA already handles that
- Works with ALL configured conversation agents (any Ollama model, OpenWebUI, etc.)
- Automatic auth, connection pooling, and error handling by HA
- Model selection = picking a conversation entity
"""
from __future__ import annotations

import json
import logging
import re
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class LocalLLMClient:
    """Calls HA conversation agents via conversation.process service."""

    def __init__(self, hass: HomeAssistant, agent_entity_id: str) -> None:
        self.hass = hass
        self.agent_entity_id = agent_entity_id

    async def complete(self, system: str, prompt: str, max_tokens: int = 2000) -> str:
        """Single-turn completion via HA conversation agent."""
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        return await self._call_conversation(full_prompt)

    async def chat(self, messages: list[dict], system: str = "", max_tokens: int = 500) -> str:
        """Multi-turn chat via HA conversation agent.

        HA conversation.process is single-turn, so we flatten the history
        into the prompt for NPC interrogation context.
        """
        lines = []
        if system:
            lines.append(system)
            lines.append("")
        for m in messages:
            role = "Detective" if m["role"] == "user" else "Suspect"
            lines.append(f"{role}: {m['content']}")
        lines.append("Suspect:")
        return await self._call_conversation("\n".join(lines))

    async def _call_conversation(self, text: str) -> str:
        """Call conversation.process and extract the response text."""
        try:
            result = await self.hass.services.async_call(
                "conversation",
                "process",
                {
                    "agent_id": self.agent_entity_id,
                    "text": text,
                },
                blocking=True,
                return_response=True,
            )
            # HA returns: {"response": {"speech": {"plain": {"speech": "..."}}, ...}}
            if result:
                speech = (
                    result
                    .get("response", {})
                    .get("speech", {})
                    .get("plain", {})
                    .get("speech", "")
                )
                if speech:
                    return speech.strip()

            _LOGGER.warning("Unexpected conversation.process response structure: %s", result)
            return ""

        except Exception as e:
            _LOGGER.error("conversation.process failed for agent %s: %s", self.agent_entity_id, e)
            return ""


async def get_conversation_agents(hass: HomeAssistant) -> list[dict]:
    """Return all available conversation agent entities, excluding the built-in HA one."""
    agents = []
    for state in hass.states.async_all("conversation"):
        entity_id = state.entity_id
        # Skip the built-in Home Assistant agent — it doesn't do LLM generation
        if entity_id == "conversation.home_assistant":
            continue
        friendly_name = state.attributes.get("friendly_name", entity_id)
        agents.append({
            "entity_id": entity_id,
            "name": friendly_name,
            "state": state.state,
            "available": state.state != "unavailable",
        })
    return sorted(agents, key=lambda x: x["name"])


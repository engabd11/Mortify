"""LLM client for Mortify.

Mortify reuses Home Assistant's built-in conversation agents (Ollama,
LocalAI, OpenAI, OpenWebUI, whatever the user has configured) rather than
making its own HTTP calls. The "model" the host picks at game-creation
time is really a ``conversation.*`` entity, and we hand it text via
``conversation.process``.

The original Mortify called this service with ``blocking=True`` and no
timeout, which meant a slow Ollama could stall the HTTP request layer
for arbitrarily long. We add an ``asyncio.wait_for`` wrapper and let the
caller pick the timeout.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class LLMTimeoutError(Exception):
    """Raised when an LLM round-trip exceeds the configured timeout."""


class LLMError(Exception):
    """Raised when conversation.process returns nothing usable."""


class LLMClient:
    """Thin wrapper around HA's ``conversation.process`` service.

    The conversation API is single-turn, so for multi-turn NPC chat we
    flatten the conversation history into the prompt instead of relying
    on the agent's own context. This is identical to the original
    Mortify behaviour — the difference is the timeout and error
    handling.
    """

    def __init__(self, hass: HomeAssistant, agent_entity_id: str) -> None:
        self.hass = hass
        self.agent_entity_id = agent_entity_id

    async def complete(
        self,
        system: str,
        prompt: str,
        timeout: float,
    ) -> str:
        """Single-turn completion.

        Raises:
            LLMTimeoutError: agent didn't respond inside ``timeout``.
            LLMError: agent returned an unparseable / empty response.
        """
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        return await self._call(full_prompt, timeout)

    async def chat(
        self,
        system: str,
        history: list[dict[str, str]],
        user_message: str,
        timeout: float,
        user_label: str = "Detective",
        assistant_label: str = "Suspect",
    ) -> str:
        """Multi-turn chat, flattened into a single prompt.

        ``history`` is a list of ``{"role": "user"|"assistant", "content": str}``
        dicts.
        """
        lines: list[str] = []
        if system:
            lines.append(system)
            lines.append("")
        for m in history:
            label = user_label if m.get("role") == "user" else assistant_label
            lines.append(f"{label}: {m.get('content', '')}")
        lines.append(f"{user_label}: {user_message}")
        lines.append(f"{assistant_label}:")
        return await self._call("\n".join(lines), timeout)

    async def _call(self, text: str, timeout: float) -> str:
        try:
            result = await asyncio.wait_for(
                self.hass.services.async_call(
                    "conversation",
                    "process",
                    {
                        "agent_id": self.agent_entity_id,
                        "text": text,
                    },
                    blocking=True,
                    return_response=True,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as err:
            raise LLMTimeoutError(
                f"conversation.process timed out after {timeout}s"
            ) from err
        except Exception as err:  # noqa: BLE001
            # HA can raise ServiceNotFound, ServiceValidationError, or whatever
            # the underlying integration emits. Treat anything as LLMError so
            # callers don't need to know the full taxonomy.
            raise LLMError(f"conversation.process failed: {err}") from err

        if not result:
            raise LLMError("conversation.process returned no result")

        # HA shape: {"response": {"speech": {"plain": {"speech": "..."}}, ...}}
        try:
            speech = (
                result
                .get("response", {})
                .get("speech", {})
                .get("plain", {})
                .get("speech", "")
            )
        except AttributeError as err:
            raise LLMError(
                f"conversation.process returned unexpected shape: {result!r}"
            ) from err

        if not speech:
            raise LLMError("conversation.process returned empty speech")
        return speech.strip()


def list_conversation_agents(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Enumerate available conversation agents for the admin picker.

    The built-in ``conversation.home_assistant`` agent is excluded — it
    handles intents, not freeform generation.
    """
    agents: list[dict[str, Any]] = []
    for state in hass.states.async_all("conversation"):
        entity_id = state.entity_id
        if entity_id == "conversation.home_assistant":
            continue
        agents.append({
            "entity_id": entity_id,
            "name": state.attributes.get("friendly_name", entity_id),
            "state": state.state,
            "available": state.state not in ("unavailable", "unknown"),
        })
    agents.sort(key=lambda a: a["name"].lower())
    return agents

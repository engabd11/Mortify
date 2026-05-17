"""Local LLM client — works with Ollama and LM Studio (OpenAI-compatible)."""
from __future__ import annotations

import json
import logging
import aiohttp
from typing import AsyncIterator

_LOGGER = logging.getLogger(__name__)


class LocalLLMClient:
    """Async client for local LLM inference. Supports Ollama and LM Studio."""

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._is_ollama = True  # detected on first call

    async def _detect_backend(self, session: aiohttp.ClientSession) -> None:
        """Detect whether we're talking to Ollama or an OpenAI-compatible server."""
        try:
            async with session.get(f"{self.base_url}/api/tags", timeout=aiohttp.ClientTimeout(total=3)) as r:
                self._is_ollama = r.status == 200
        except Exception:
            self._is_ollama = False

    async def complete(self, system: str, prompt: str, max_tokens: int = 2000) -> str:
        """Get a single completion from the local LLM."""
        async with aiohttp.ClientSession() as session:
            await self._detect_backend(session)
            if self._is_ollama:
                return await self._ollama_complete(session, system, prompt, max_tokens)
            else:
                return await self._openai_complete(session, system, prompt, max_tokens)

    async def _ollama_complete(
        self, session: aiohttp.ClientSession, system: str, prompt: str, max_tokens: int
    ) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": 0.85},
        }
        try:
            async with session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                data = await resp.json()
                return data.get("response", "").strip()
        except Exception as e:
            _LOGGER.error("Ollama completion failed: %s", e)
            return ""

    async def _openai_complete(
        self, session: aiohttp.ClientSession, system: str, prompt: str, max_tokens: int
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.85,
        }
        try:
            async with session.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            _LOGGER.error("OpenAI-compat completion failed: %s", e)
            return ""

    async def chat(self, messages: list[dict], system: str = "", max_tokens: int = 500) -> str:
        """Multi-turn chat completion (for NPC interrogation)."""
        async with aiohttp.ClientSession() as session:
            await self._detect_backend(session)
            if self._is_ollama:
                # Build prompt from messages
                prompt = "\n".join(
                    f"{'Detective' if m['role']=='user' else 'Suspect'}: {m['content']}"
                    for m in messages
                )
                prompt += "\nSuspect:"
                return await self._ollama_complete(session, system, prompt, max_tokens)
            else:
                payload = {
                    "model": self.model,
                    "messages": [{"role": "system", "content": system}] + messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.9,
                }
                try:
                    async with session.post(
                        f"{self.base_url}/v1/chat/completions",
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"].strip()
                except Exception as e:
                    _LOGGER.error("Chat completion failed: %s", e)
                    return "I have nothing more to say to you."

"""Config flow for Mortify.

Like Quizify, Mortify is a single-instance integration with no required
config. The conversation agent, speaker, and TTS engine are all chosen
per-game in the admin panel, so there's nothing to configure up front.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN

# ConfigFlowResult landed in HA 2024.3; fall back to the generic FlowResult
# alias so we still import cleanly on older HA.
try:
    from homeassistant.config_entries import ConfigFlowResult
except ImportError:
    from homeassistant.data_entry_flow import FlowResult as ConfigFlowResult  # type: ignore[assignment]


class MortifyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the (trivial) Mortify setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Single-step flow that creates one entry without input."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Mortify 🔪", data={})

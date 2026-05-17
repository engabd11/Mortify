"""Config flow for Mortify."""
from __future__ import annotations

import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_LLM_URL,
    CONF_LLM_MODEL,
    CONF_TTS_ENTITY,
    CONF_HA_URL,
    DEFAULT_LLM_URL,
    DEFAULT_LLM_MODEL,
)

_LOGGER = logging.getLogger(__name__)


def _get_ha_url(hass: HomeAssistant) -> str:
    """Best-effort get the HA external/internal URL."""
    try:
        from homeassistant.helpers.network import get_url
        return get_url(hass)
    except Exception:
        pass
    try:
        return hass.config.internal_url or hass.config.external_url or "http://homeassistant.local:8123"
    except Exception:
        return "http://homeassistant.local:8123"


class MortifyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Mortify config flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            llm_ok = await self._test_llm(user_input[CONF_LLM_URL])
            if not llm_ok:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Mortify 🔪",
                    data=user_input,
                )

        ha_url = _get_ha_url(self.hass)

        schema = vol.Schema(
            {
                vol.Required(CONF_LLM_URL, default=DEFAULT_LLM_URL): str,
                vol.Required(CONF_LLM_MODEL, default=DEFAULT_LLM_MODEL): str,
                vol.Optional(CONF_TTS_ENTITY, default=""): str,
                vol.Required(CONF_HA_URL, default=ha_url): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def _test_llm(self, url: str) -> bool:
        """Test connectivity to the local LLM."""
        import aiohttp
        clean_url = url.rstrip("/")
        for path in ["/api/tags", "/v1/models"]:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{clean_url}{path}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status in (200, 401):
                            return True
            except Exception:
                continue
        return False

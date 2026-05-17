"""Config flow for Mortify."""
from __future__ import annotations

import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .const import DOMAIN, CONF_TTS_ENTITY, CONF_HA_URL

_LOGGER = logging.getLogger(__name__)


def _get_ha_url(hass: HomeAssistant) -> str:
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
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Mortify 🔪", data=user_input)

        ha_url = _get_ha_url(self.hass)

        schema = vol.Schema({
            vol.Optional(CONF_TTS_ENTITY, default=""): str,
            vol.Required(CONF_HA_URL, default=ha_url): str,
        })

        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    def async_get_options_flow(config_entry):
        return MortifyOptionsFlow(config_entry)


class MortifyOptionsFlow(config_entries.OptionsFlow):
    """Change TTS entity and HA URL after setup."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        current = self.config_entry.data
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**current, **user_input}
            )
            return self.async_create_entry(title="", data={})

        schema = vol.Schema({
            vol.Optional(CONF_TTS_ENTITY, default=current.get(CONF_TTS_ENTITY, "")): str,
            vol.Required(CONF_HA_URL, default=current.get(CONF_HA_URL, "http://homeassistant.local:8123")): str,
        })

        return self.async_show_form(step_id="init", data_schema=schema)


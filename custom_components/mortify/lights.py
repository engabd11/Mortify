"""Party Lights service for Mortify — dramatic mood-based lighting.

Adopts proven patterns from Beatify (capability detection, WLED presets,
intensity levels, beat loop, celebration sequence, state save/restore)
while keeping Mortify's act-based mood system as the primary API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from .const import (
    EVENT_ACCUSATION_SUBMITTED,
    EVENT_CLUE_DISCOVERED,
    STATE_ACCUSATION,
    STATE_ACT_1,
    STATE_ACT_2,
    STATE_ACT_3,
    STATE_ENDED,
    STATE_REVEAL,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase colours — mapped to Mortify game states
# ---------------------------------------------------------------------------

PHASE_COLORS: dict[str, dict[str, Any]] = {
    STATE_ACT_1:    {"rgb_color": [0, 100, 255],   "brightness": 102},  # blue — discovery
    STATE_ACT_2:    {"rgb_color": [147, 112, 219], "brightness": 128},  # purple — suspicion
    STATE_ACT_3:    {"rgb_color": [200, 60, 40],   "brightness": 153},  # red — danger
    STATE_ACCUSATION: {"rgb_color": [255, 140, 0], "brightness": 180},  # orange — tension
    STATE_REVEAL:   {"rgb_color": [0, 200, 100],   "brightness": 204},  # green — revelation
    STATE_ENDED:    {"color_temp_kelvin": 3000,     "brightness": 255},  # warm white
    # lobby / generating — fall through to on/off handling
}

FLASH_COLORS: dict[str, list[int]] = {
    "gold":   [255, 215, 0],
    "green":  [0, 255, 0],
    "red":    [255, 0, 0],
    "orange": [255, 165, 0],
    "purple": [147, 112, 219],
}

RAINBOW_COLORS: list[list[int]] = [
    [255, 0, 0], [255, 127, 0], [255, 255, 0],
    [0, 255, 0], [0, 0, 255], [75, 0, 130], [148, 0, 211],
]

BEAT_COLORS: list[list[int]] = [
    [0, 100, 255], [0, 180, 255], [0, 60, 200],
]

INTENSITY_PRESETS: dict[str, dict[str, float]] = {
    "subtle": {"brightness_scale": 0.6, "flash_duration": 0.8},
    "medium": {"brightness_scale": 1.0, "flash_duration": 0.5},
    "party":  {"brightness_scale": 1.0, "flash_duration": 0.3},
}

WLED_PRESET_DEFAULTS: dict[str, int] = {
    STATE_ACT_1: 1, STATE_ACT_2: 2, STATE_ACT_3: 3,
    STATE_ACCUSATION: 4, STATE_REVEAL: 5, STATE_ENDED: 6,
}


@dataclass
class _SavedLightState:
    """Snapshot of a light before the game takes it over."""

    entity_id: str
    state: str  # "on" | "off"
    brightness: int | None = None
    rgb_color: tuple[int, ...] | None = None
    color_temp_kelvin: int | None = None
    effect: str | None = None

    @classmethod
    def from_ha_state(cls, entity_id: str, state_obj: Any) -> "_SavedLightState":
        attrs = state_obj.attributes
        return cls(
            entity_id=entity_id,
            state=state_obj.state,
            brightness=attrs.get("brightness"),
            rgb_color=(
                tuple(attrs["rgb_color"])
                if isinstance(attrs.get("rgb_color"), (list, tuple))
                else None
            ),
            color_temp_kelvin=attrs.get("color_temp_kelvin"),
            effect=attrs.get("effect"),
        )


class MortifyLights:
    """Dramatic lighting control for Mortify games."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._entity_ids: list[str] = []
        self._intensity: str = "medium"
        self._light_mode: str = "dynamic"
        self._wled_presets: dict[str, int] = dict(WLED_PRESET_DEFAULTS)
        self._wled_entities: set[str] = set()
        self._saved_states: dict[str, _SavedLightState] = {}
        self._active: bool = False
        self._current_phase: str | None = None
        self._beat_task: asyncio.Task | None = None
        self._base_brightness: int = 128

    # -- public API ---------------------------------------------------------

    async def start(
        self,
        entity_ids: list[str],
        intensity: str = "medium",
        light_mode: str = "dynamic",
        wled_presets: dict[str, int] | None = None,
    ) -> None:
        """Save current light states and take control."""
        if not entity_ids:
            return
        self._entity_ids = list(entity_ids)
        self._intensity = intensity if intensity in INTENSITY_PRESETS else "medium"
        self._light_mode = (
            light_mode if light_mode in ("static", "dynamic", "wled") else "dynamic"
        )
        if wled_presets:
            self._wled_presets.update(wled_presets)

        # Detect WLED entities
        self._wled_entities = self._detect_wled_entities()

        # Save current states for restoration
        self._saved_states = {}
        for eid in self._entity_ids:
            state_obj = self._hass.states.get(eid)
            if state_obj:
                self._saved_states[eid] = _SavedLightState.from_ha_state(eid, state_obj)

        # Compute base brightness for subtle mode
        brightnesses = [
            s.brightness
            for s in self._saved_states.values()
            if s.state != "off" and s.brightness is not None
        ]
        self._base_brightness = (
            int(sum(brightnesses) / len(brightnesses)) if brightnesses else 128
        )

        self._active = True
        _LOGGER.info(
            "Mortify Lights started: %d lights, intensity=%s, mode=%s, wled=%d",
            len(self._entity_ids), self._intensity, self._light_mode,
            len(self._wled_entities),
        )

    async def set_phase(self, phase: str) -> None:
        """Apply phase-appropriate colours/brightness."""
        if not self._active or not self._entity_ids:
            return
        self._current_phase = phase

        # Stop beat loop when leaving an act
        if phase not in (STATE_ACT_1, STATE_ACT_2, STATE_ACT_3):
            await self.stop_beat_loop()

        if phase == STATE_ENDED:
            return  # celebration() handles this

        # WLED mode → presets
        if self._light_mode == "wled" and self._wled_entities:
            preset_id = self._wled_presets.get(phase)
            if preset_id is not None:
                for eid in self._wled_entities:
                    await self._apply_wled(eid, preset_id)
            non_wled = [e for e in self._entity_ids if e not in self._wled_entities]
            if non_wled:
                phase_data = PHASE_COLORS.get(phase)
                if phase_data:
                    await self._apply(non_wled, dict(phase_data), transition=1.0)
        else:
            phase_data = PHASE_COLORS.get(phase)
            if not phase_data:
                return
            service_data = dict(phase_data)
            self._scale_brightness(service_data)
            await self._apply(self._entity_ids, service_data, transition=1.0)

        # Beat loop for investigation acts
        if phase in (STATE_ACT_1, STATE_ACT_2, STATE_ACT_3) and self._light_mode == "dynamic":
            await self.start_beat_loop()

    async def flash(self, color_name: str = "red") -> None:
        """Quick flash — on with colour, sleep, restore phase colour."""
        if not self._active or not self._entity_ids:
            return
        rgb = FLASH_COLORS.get(color_name)
        if not rgb:
            return
        preset = INTENSITY_PRESETS.get(self._intensity, INTENSITY_PRESETS["medium"])
        dur = preset["flash_duration"]
        await self._apply(
            self._entity_ids,
            {"rgb_color": rgb, "brightness": 255},
            transition=0.1,
        )
        await asyncio.sleep(dur)
        if self._current_phase and self._current_phase in PHASE_COLORS:
            restore = dict(PHASE_COLORS[self._current_phase])
            self._scale_brightness(restore)
            await self._apply(self._entity_ids, restore, transition=0.3)

    async def strobe(self, count: int = 5, interval: float = 0.4) -> None:
        """Rapid on/off strobe for countdown tension."""
        for _ in range(count):
            if not self._active:
                break
            await self._apply(
                self._entity_ids,
                {"rgb_color": [255, 0, 0], "brightness": 255},
                transition=0.05,
            )
            await asyncio.sleep(interval / 2)
            await self._apply(
                self._entity_ids, {"brightness": 10}, transition=0.05,
            )
            await asyncio.sleep(interval / 2)
        if self._current_phase and self._current_phase in PHASE_COLORS:
            restore = dict(PHASE_COLORS[self._current_phase])
            self._scale_brightness(restore)
            await self._apply(self._entity_ids, restore, transition=0.3)

    async def celebrate(self) -> None:
        """Rainbow cycle celebration for ~5 seconds."""
        if not self._active or not self._entity_ids:
            return
        _LOGGER.info("Mortify Lights: celebration sequence")
        brightness = 255 if self._intensity != "subtle" else min(self._base_brightness + 102, 255)
        for color in RAINBOW_COLORS:
            if not self._active:
                break
            await self._apply(
                self._entity_ids,
                {"rgb_color": color, "brightness": brightness},
                transition=0.3,
            )
            await asyncio.sleep(0.7)

    async def start_beat_loop(self, bpm: int = 90) -> None:
        """Start a background beat-pulse during investigation acts."""
        if self._light_mode != "dynamic":
            return
        await self.stop_beat_loop()
        self._beat_task = asyncio.create_task(self._beat_loop(bpm))

    async def stop_beat_loop(self) -> None:
        if self._beat_task is not None:
            self._beat_task.cancel()
            self._beat_task = None

    async def stop(self) -> None:
        """Restore saved light states and release control."""
        if not self._active:
            return
        await self.stop_beat_loop()
        self._active = False
        _LOGGER.info(
            "Mortify Lights stopping, restoring %d lights", len(self._saved_states),
        )
        for eid, saved in self._saved_states.items():
            try:
                if saved.state == "off":
                    await self._hass.services.async_call(
                        "light", "turn_off", {"entity_id": eid}, blocking=False,
                    )
                else:
                    restore_data: dict[str, Any] = {"entity_id": eid}
                    if saved.brightness is not None:
                        restore_data["brightness"] = saved.brightness
                    if saved.rgb_color is not None:
                        restore_data["rgb_color"] = list(saved.rgb_color)
                    if saved.color_temp_kelvin is not None:
                        restore_data["color_temp_kelvin"] = saved.color_temp_kelvin
                    if saved.effect is not None:
                        restore_data["effect"] = saved.effect
                    await self._hass.services.async_call(
                        "light", "turn_on", restore_data, blocking=False,
                    )
            except (HomeAssistantError, ServiceNotFound):
                _LOGGER.warning("Failed to restore light: %s", eid)
        self._saved_states = {}
        self._entity_ids = []
        self._current_phase = None

    # -- internal -----------------------------------------------------------

    def _scale_brightness(self, data: dict[str, Any]) -> None:
        """Apply intensity preset scaling to brightness."""
        preset = INTENSITY_PRESETS.get(self._intensity, INTENSITY_PRESETS["medium"])
        if "brightness" in data:
            data["brightness"] = int(data["brightness"] * preset["brightness_scale"])

    def _get_capability(self, entity_id: str) -> str:
        """Check ``supported_color_modes`` to classify a light."""
        state = self._hass.states.get(entity_id)
        if not state:
            return "onoff"
        modes = state.attributes.get("supported_color_modes", [])
        if not modes:
            return "onoff"
        if any(m in modes for m in ("rgb", "rgbw", "rgbww", "hs", "xy")):
            return "rgb"
        if "color_temp" in modes:
            return "ct"
        if "brightness" in modes:
            return "dim"
        return "onoff"

    def _detect_wled_entities(self) -> set[str]:
        """Identify WLED-backed lights via entity registry platform."""
        wled: set[str] = set()
        try:
            from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
            registry = er.async_get(self._hass)
            for eid in self._entity_ids:
                entry = registry.async_get(eid)
                if entry and entry.platform == "wled":
                    wled.add(eid)
        except (ImportError, AttributeError, KeyError):
            for eid in self._entity_ids:
                if "wled" in eid.lower():
                    wled.add(eid)
        return wled

    async def _apply(
        self,
        entity_ids: list[str],
        service_data: dict[str, Any],
        transition: float = 1.0,
    ) -> None:
        """Batch-apply light state, adapting per-bulb capability."""
        for eid in entity_ids:
            cap = self._get_capability(eid)
            call: dict[str, Any] = {"entity_id": eid, "transition": transition}

            if cap == "rgb":
                if "rgb_color" in service_data:
                    call["rgb_color"] = service_data["rgb_color"]
                if "color_temp_kelvin" in service_data:
                    call["color_temp_kelvin"] = service_data["color_temp_kelvin"]
                if "brightness" in service_data:
                    call["brightness"] = service_data["brightness"]
            elif cap == "ct":
                if "color_temp_kelvin" in service_data:
                    call["color_temp_kelvin"] = service_data["color_temp_kelvin"]
                elif "rgb_color" in service_data:
                    r, g, b = service_data["rgb_color"]
                    call["color_temp_kelvin"] = 2700 if r > b else 6500
                if "brightness" in service_data:
                    call["brightness"] = service_data["brightness"]
            elif cap == "dim":
                if "brightness" in service_data:
                    call["brightness"] = service_data["brightness"]
            # else onoff: just turn on

            try:
                await self._hass.services.async_call(
                    "light", "turn_on", call, blocking=False,
                )
            except (HomeAssistantError, ServiceNotFound):
                _LOGGER.warning("Failed to control light: %s", eid)

    async def _apply_wled(self, entity_id: str, preset_id: int) -> None:
        """Activate a WLED preset by ID."""
        preset_entity = None
        try:
            from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
            registry = er.async_get(self._hass)
            light_entry = registry.async_get(entity_id)
            if light_entry and light_entry.device_id:
                for entry in registry.entities.values():
                    if (
                        entry.device_id == light_entry.device_id
                        and entry.domain == "select"
                        and "preset" in (entry.entity_id or "")
                    ):
                        preset_entity = entry.entity_id
                        break
        except (ImportError, AttributeError, KeyError):
            pass
        if not preset_entity:
            preset_entity = entity_id.replace("light.", "select.") + "_preset"
        try:
            await self._hass.services.async_call(
                "select", "select_option",
                {"entity_id": preset_entity, "option": str(preset_id)},
                blocking=False,
            )
        except (HomeAssistantError, ServiceNotFound):
            _LOGGER.warning(
                "Failed WLED preset %d on %s (tried %s)",
                preset_id, entity_id, preset_entity,
            )

    async def _beat_loop(self, bpm: int) -> None:
        """Pulse between blue shades at the given BPM."""
        interval = 60.0 / bpm
        i = 0
        try:
            while self._active:
                entities = [e for e in self._entity_ids if e not in self._wled_entities]
                if entities:
                    await self._apply(
                        entities,
                        {"rgb_color": BEAT_COLORS[i % len(BEAT_COLORS)], "brightness": 200},
                        transition=0.1,
                    )
                i += 1
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

"""Transmitter keys and rockers as event entities."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, device_info_for, rocker_info
from .const import KIND_ROCKER, SENSOR_TYPES
from .entity import EnetEntity
from .hub import Hub, Key
from .visibility import filtered

TYPES_KEY = ["gedrueckt", "losgelassen"]

# On handheld and wall transmitters the rocker keys sit side by side; firmware < 0.5.1 reported top/bottom
SIDE = {"links": "links", "rechts": "rechts", "oben": "links", "unten": "rechts"}
TYPES_ROCKER = ["links_gedrueckt", "links_losgelassen", "rechts_gedrueckt", "rechts_losgelassen",
               "beide_gedrueckt", "beide_losgelassen"]
BOTH = 0xC000  # both direction bits in the key value: both keys of the rocker (≥ 4 s = store switch-on brightness)


@filtered
async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    entities: list = [EnetKey(hub, t) for t in hub.keys_]
    entities += [SensorScene(hub, g, k) for g in hub.installation["geraete"] if g.get("typ") in SENSOR_TYPES for k in (1, 2)]
    async_add_entities(entities)


class EnetKey(EnetEntity, EventEntity):
    _attr_device_class = EventDeviceClass.BUTTON

    def __init__(self, hub: Hub, key_obj: Key) -> None:
        super().__init__(hub)
        self.key_obj = key_obj
        self._pressed: float | None = None
        g = key_obj.enet_device
        self._attr_unique_id = f"{g['sn']}_taste_{key_obj.nr}"
        self._attr_device_info = rocker_info(hub, g, (key_obj.nr + 1) // 2)
        rocker = key_obj.art == KIND_ROCKER
        self._attr_translation_key = key_obj.art  # name "Key n" / "Keys n/n+1" from strings.json
        self._attr_translation_placeholders = {"nr": str(key_obj.nr), "nr2": str(key_obj.nr + 1)}
        self._attr_event_types = TYPES_ROCKER if rocker else TYPES_KEY

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"key_{self.key_obj.key}", self._on_key)

    @callback
    def _on_key(self, msg: dict[str, Any]) -> None:
        action = "gedrueckt" if msg.get("press") else "losgelassen"
        if self.key_obj.art == KIND_ROCKER:
            # rockers send key values (value/direct) with a direction; server keys exist only for single keys
            side = SIDE[msg.get("richtung", "links")]
            if int(msg.get("cmd", "0") or "0", 16) & BOTH == BOTH:
                side = "beide"
            action = f"{side}_{action}"
        attrs = {k: msg[k] for k in ("kind", "hold_ms", "code", "answered", "scene", "rssi") if k in msg}
        # single keys (server keys) send no hold duration: measure the time between press and release ourselves
        if msg.get("press"):
            self._pressed = time.monotonic()
        elif "hold_ms" not in attrs and self._pressed is not None:
            attrs["hold_ms"] = round((time.monotonic() - self._pressed) * 1000)
            self._pressed = None
        self._trigger_event(action, attrs)
        self.async_write_ha_state()


class SensorScene(EnetEntity, EventEntity):
    """Scene telegram of the sun sensor per channel (also in test mode via the keys on the sensor)."""

    _attr_event_types = ["ein", "aus"]

    def __init__(self, hub: Hub, enet_device: dict, channel: int) -> None:
        super().__init__(hub)
        self.sn, self.channel = enet_device["sn"], channel
        self._attr_translation_key = "sonnenschutz_szene" if channel == 1 else "daemmerung_szene"
        self._attr_unique_id = f"{self.sn}_szene_{channel}"
        self._attr_device_info = device_info_for(hub, enet_device)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"sensor_szene_{self.sn}_{self.channel}", self._on_scene)

    @property
    def available(self) -> bool:
        return True

    @callback
    def _on_scene(self, msg: dict[str, Any]) -> None:
        self._trigger_event("ein" if msg["ein"] else "aus", {"szene": msg["szene"]})
        self.async_write_ha_state()

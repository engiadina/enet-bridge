"""Sender-Tasten und -Wippen als Ereignis-Entitäten."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, geraet_info, wippe_info
from .const import ART_WIPPE, SENSOR_TYPEN
from .entity import EnetEntity
from .hub import Hub, Taste

TYPEN_TASTE = ["gedrueckt", "losgelassen"]

# Wippentasten liegen bei Hand- und Wandsendern nebeneinander; Firmware < 0.5.1 meldete oben/unten
SEITE = {"links": "links", "rechts": "rechts", "oben": "links", "unten": "rechts"}
TYPEN_WIPPE = ["links_gedrueckt", "links_losgelassen", "rechts_gedrueckt", "rechts_losgelassen",
               "beide_gedrueckt", "beide_losgelassen"]
BEIDE = 0xC000  # beide Richtungsbits im Tastenwert: beide Tasten der Wippe (≥ 4 s = Einschalthelligkeit speichern)


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    entities: list = [EnetKey(hub, t) for t in hub.tasten]
    entities += [SensorSzene(hub, g, k) for g in hub.anlage["geraete"] if g.get("typ") in SENSOR_TYPEN for k in (1, 2)]
    async_add_entities(entities)


class EnetKey(EnetEntity, EventEntity):
    _attr_device_class = EventDeviceClass.BUTTON

    def __init__(self, hub: Hub, taste: Taste) -> None:
        super().__init__(hub)
        self.taste = taste
        self._gedrueckt: float | None = None
        g = taste.geraet
        self._attr_unique_id = f"{g['sn']}_taste_{taste.nr}"
        self._attr_device_info = wippe_info(hub, g, (taste.nr + 1) // 2)
        wippe = taste.art == ART_WIPPE
        self._attr_translation_key = taste.art  # Name „Taste n“ / „Tasten n/n+1“ aus strings.json
        self._attr_translation_placeholders = {"nr": str(taste.nr), "nr2": str(taste.nr + 1)}
        self._attr_event_types = TYPEN_WIPPE if wippe else TYPEN_TASTE

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"key_{self.taste.key}", self._on_key)

    @callback
    def _on_key(self, msg: dict[str, Any]) -> None:
        aktion = "gedrueckt" if msg.get("press") else "losgelassen"
        if self.taste.art == ART_WIPPE:
            # Wippen senden Tastenwerte (wert/direkt) mit Richtung; Server-Tasten gibt es nur bei Einzeltasten
            seite = SEITE[msg.get("richtung", "links")]
            if int(msg.get("cmd", "0") or "0", 16) & BEIDE == BEIDE:
                seite = "beide"
            aktion = f"{seite}_{aktion}"
        attrs = {k: msg[k] for k in ("kind", "hold_ms", "code", "answered", "scene", "rssi") if k in msg}
        # Einzeltasten (Server-Tasten) senden keine Haltedauer: Zeit zwischen Drücken und Loslassen selbst messen
        if msg.get("press"):
            self._gedrueckt = time.monotonic()
        elif "hold_ms" not in attrs and self._gedrueckt is not None:
            attrs["hold_ms"] = round((time.monotonic() - self._gedrueckt) * 1000)
            self._gedrueckt = None
        self._trigger_event(aktion, attrs)
        self.async_write_ha_state()


class SensorSzene(EnetEntity, EventEntity):
    """Szenentelegramm des Sonnensensors je Kanal (auch beim Testbetrieb über die Tasten am Sensor)."""

    _attr_event_types = ["ein", "aus"]

    def __init__(self, hub: Hub, geraet: dict, kanal: int) -> None:
        super().__init__(hub)
        self.sn, self.kanal = geraet["sn"], kanal
        self._attr_translation_key = "sonnenschutz_szene" if kanal == 1 else "daemmerung_szene"
        self._attr_unique_id = f"{self.sn}_szene_{kanal}"
        self._attr_device_info = geraet_info(hub, geraet)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"sensor_szene_{self.sn}_{self.kanal}", self._on_szene)

    @property
    def available(self) -> bool:
        return True

    @callback
    def _on_szene(self, msg: dict[str, Any]) -> None:
        self._trigger_event("ein" if msg["ein"] else "aus", {"szene": msg["szene"]})
        self.async_write_ha_state()

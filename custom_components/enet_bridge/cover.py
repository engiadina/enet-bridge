"""Jalousie-Kanäle (CT_1F03, z. B. Gira 5427 00) als Rollladen/Jalousie.

Aus dem Katalog umgesetzt, am Gerät noch ungetestet. Der Aktor zählt die Position von oben (0 % = ganz oben,
100 % = ganz unten), HA von unten (100 = offen) – hier umgerechnet, ebenso die Lamelle. Befehle über das
Bridge-Kommando „cover“ (Firmware ab 0.8.20): Auf/Ab GO 2, Stopp GO 4, Position GO 5, Lamelle GO 6; Zustand aus
Fahrinfo (GO 10), Position (GO 12) und Lamelle (GO 13), nach einem Befehl fragt die Bridge gestaffelt nach.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, kanal_info
from .bridge import BridgeError
from .entity import EnetEntity
from .hub import Hub, Kanal

_LOGGER = logging.getLogger(__name__)

FAEHRT_AUF, FAEHRT_AB = 2, 3  # Fahrinfo GO 10 (0 steht oben, 1 steht unten)


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(EnetJalousie(hub, k) for k in hub.jalousien)


class EnetJalousie(EnetEntity, CoverEntity):
    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = (CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
                                | CoverEntityFeature.SET_POSITION | CoverEntityFeature.OPEN_TILT
                                | CoverEntityFeature.CLOSE_TILT | CoverEntityFeature.SET_TILT_POSITION)

    def __init__(self, hub: Hub, kanal: Kanal) -> None:
        super().__init__(hub)
        self.kanal = kanal
        self._attr_unique_id = f"{kanal.geraet['sn']}_{kanal.nr}"
        self._attr_device_info = kanal_info(hub, kanal)
        self._attr_name = None  # Hauptfunktion des (Kanal-)Geräts

    @property
    def _z(self):
        return self.hub.zustand[self.kanal.key]

    @property
    def current_cover_position(self) -> int | None:
        return 100 - self._z.percent if self._z.known else None

    @property
    def current_cover_tilt_position(self) -> int | None:
        return None if self._z.lamelle is None else 100 - self._z.lamelle

    @property
    def is_closed(self) -> bool | None:
        return self._z.percent >= 100 if self._z.known else None

    @property
    def is_opening(self) -> bool:
        return self._z.bewegung == FAEHRT_AUF

    @property
    def is_closing(self) -> bool:
        return self._z.bewegung == FAEHRT_AB

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"state_{self.kanal.key}")

    async def _send(self, action: str, percent: int | None = None) -> None:
        felder: dict[str, Any] = {"action": action}
        if percent is not None:
            felder["percent"] = percent
        try:
            async with self.hub.params.programmierung(self.kanal.ia):  # nicht mitten in einen Parameter-Download
                result = await self.hub.bridge.request("cover", ia=self.kanal.ia, ch=self.kanal.nr, **felder)
        except BridgeError as err:
            raise HomeAssistantError(f"{self.kanal.geraet['name']}: {err}") from err
        self._z.gemeldet = time.monotonic()
        if result.get("tries", 1) > 1:
            _LOGGER.log(logging.WARNING if result["tries"] >= 3 else logging.INFO,
                        "%s: cover %s acknowledged only after %d tries", self.kanal.geraet["name"], action,
                        result["tries"])

    def _faehrt(self, bewegung: int | None) -> None:
        """Vorläufig bis zur Fahrinfo des Aktors bzw. zur Abfrage der Bridge."""
        self._z.bewegung = bewegung
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._send("up")
        self._faehrt(FAEHRT_AUF)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._send("down")
        self._faehrt(FAEHRT_AB)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._send("stop")
        self._faehrt(None)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        ziel = 100 - int(kwargs[ATTR_POSITION])  # Aktor: 0 % = oben
        await self._send("position", ziel)
        if self._z.known and ziel != self._z.percent:
            self._faehrt(FAEHRT_AB if ziel > self._z.percent else FAEHRT_AUF)

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        await self._send("slat", 0)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        await self._send("slat", 100)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        await self._send("slat", 100 - int(kwargs[ATTR_TILT_POSITION]))

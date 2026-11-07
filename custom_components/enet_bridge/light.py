"""Aktor-Kanäle (Schalten, Dimmen) als Licht."""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, kanal_info
from .bridge import BridgeError
from .const import ART_DIMMEN
from .entity import EnetEntity
from .hub import Hub, Kanal

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(EnetLight(hub, k) for k in hub.kanaele)


class EnetLight(EnetEntity, LightEntity):
    def __init__(self, hub: Hub, kanal: Kanal) -> None:
        super().__init__(hub)
        self.kanal = kanal
        g = kanal.geraet
        self._attr_unique_id = f"{g['sn']}_{kanal.nr}"
        self._attr_device_info = kanal_info(hub, kanal)
        self._attr_name = None  # Hauptfunktion des (Kanal-)Geräts
        mode = ColorMode.BRIGHTNESS if kanal.art == ART_DIMMEN else ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def _z(self):
        return self.hub.zustand[self.kanal.key]

    @property
    def icon(self) -> str | None:
        """Während der Aktor programmiert wird (kurz aus, dann wiederhergestellt): Werkzeug statt Glühbirne."""
        return "mdi:progress-wrench" if self.kanal.ia in self.hub.programmiert else None

    @property
    def is_on(self) -> bool | None:
        return self._z.on if self._z.known else None

    @property
    def brightness(self) -> int | None:
        if self.kanal.art != ART_DIMMEN or not self._z.known:
            return None
        return round(self._z.percent * 255 / 100)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"state_{self.kanal.key}")

    async def _send(self, cmd: str, **fields: Any) -> None:
        try:
            async with self.hub.params.programmierung(self.kanal.ia):  # nicht mitten in einen Parameter-Download
                result = await self.hub.bridge.request(cmd, ia=self.kanal.ia, ch=self.kanal.nr, **fields)
        except BridgeError as err:
            raise HomeAssistantError(f"{self.kanal.geraet['name']}: {err}") from err
        self._z.gemeldet = time.monotonic()  # bewusst geschaltet: keine Nachkorrektur einer Szene
        if result.get("tries", 1) > 1:
            _LOGGER.log(logging.WARNING if result["tries"] >= 3 else logging.INFO, "%s: %s erst nach %d Versuchen quittiert",
                        self.kanal.geraet["name"], cmd, result["tries"])

    async def async_turn_on(self, **kwargs: Any) -> None:
        dimmen = self.kanal.art == ART_DIMMEN and ATTR_BRIGHTNESS in kwargs
        percent = max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)) if dimmen else None
        if self.hub.szenen.gesperrt(self.kanal.key, True, percent):  # schon per eNet-Szenentelegramm geschaltet
            self._z.on, self._z.known = True, True
            if percent is not None:
                self._z.percent = percent
            self.async_write_ha_state()
            return
        if dimmen:
            await self._send("dim", percent=percent)
            self._z.on, self._z.percent = True, percent
        else:
            await self._send("switch", on=True)
            self._z.on = True
            if self.kanal.art != ART_DIMMEN:
                self._z.percent = 100
        # vorläufig; der Aktor meldet seinen Zustand selbst bzw. die Bridge fragt nach 8 s ab
        self._z.known = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if not self.hub.szenen.gesperrt(self.kanal.key, False, None):  # sonst schon per eNet-Szene aus
            await self._send("switch", on=False)
        self._z.on, self._z.known = False, True
        self.async_write_ha_state()

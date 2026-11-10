"""Actuator channels (switching, dimming) as lights."""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, channel_info
from .bridge import BridgeError
from .const import KIND_DIMMER
from .entity import EnetEntity
from .hub import Hub, Channel

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(EnetLight(hub, k) for k in hub.channels_)


class EnetLight(EnetEntity, LightEntity):
    def __init__(self, hub: Hub, channel: Channel) -> None:
        super().__init__(hub)
        self.channel = channel
        g = channel.enet_device
        self._attr_unique_id = f"{g['sn']}_{channel.nr}"
        self._attr_device_info = channel_info(hub, channel)
        self._attr_name = None  # main function of the (channel) device
        mode = ColorMode.BRIGHTNESS if channel.art == KIND_DIMMER else ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def _z(self):
        return self.hub.state_[self.channel.key]

    @property
    def icon(self) -> str | None:
        """While the actuator is being programmed (briefly off, then restored): tool icon instead of light bulb."""
        return "mdi:progress-wrench" if self.channel.ia in self.hub.programmed else None

    @property
    def is_on(self) -> bool | None:
        return self._z.on if self._z.known else None

    @property
    def brightness(self) -> int | None:
        if self.channel.art != KIND_DIMMER or not self._z.known:
            return None
        return round(self._z.percent * 255 / 100)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"state_{self.channel.key}")

    async def _send(self, cmd: str, **fields: Any) -> None:
        try:
            async with self.hub.params.programming(self.channel.ia):  # not in the middle of a parameter download
                result = await self.hub.bridge.request(cmd, ia=self.channel.ia, ch=self.channel.nr, **fields)
        except BridgeError as err:
            raise HomeAssistantError(f"{self.channel.enet_device['name']}: {err}") from err
        self._z.reported = time.monotonic()  # switched deliberately: no follow-up correction by a scene
        if result.get("tries", 1) > 1:
            _LOGGER.log(logging.WARNING if result["tries"] >= 3 else logging.INFO, "%s: %s acknowledged only after %d tries",
                        self.channel.enet_device["name"], cmd, result["tries"])

    async def async_turn_on(self, **kwargs: Any) -> None:
        dim = self.channel.art == KIND_DIMMER and ATTR_BRIGHTNESS in kwargs
        percent = max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)) if dim else None
        if self.hub.scenes.suppressed(self.channel.key, True, percent):  # already switched by an eNet scene telegram
            self._z.on, self._z.known = True, True
            if percent is not None:
                self._z.percent = percent
            self.async_write_ha_state()
            return
        if dim:
            await self._send("dim", percent=percent)
            self._z.on, self._z.percent = True, percent
        else:
            await self._send("switch", on=True)
            self._z.on = True
            if self.channel.art != KIND_DIMMER:
                self._z.percent = 100
        # provisional; the actuator reports its state itself, or the bridge polls after 8 s
        self._z.known = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if not self.hub.scenes.suppressed(self.channel.key, False, None):  # otherwise already off via the eNet scene
            await self._send("switch", on=False)
        self._z.on, self._z.known = False, True
        self.async_write_ha_state()

"""Blind channels (CT_1F03, e.g. Gira 5427 00) as roller shutter/blind.

Implemented from the catalog, not yet tested on a device. The actuator counts the position from the top (0 % = fully
up, 100 % = fully down), HA from the bottom (100 = open) – converted here, as is the slat. Commands via the bridge
command "cover" (firmware 0.8.20 or later): up/down GO 2, stop GO 4, position GO 5, slat GO 6; state from the travel
info (GO 10), position (GO 12) and slat (GO 13); after a command the bridge polls at staggered intervals.

The channel's operating mode (parameter OPMODE, read with the other actuator parameters) decides the device class and
whether slats exist: almost all installations run their actuators as roller shutters (SHUTTER), where the slat
functions are inactive; only BLINDS has slats. As long as the mode is unknown, slat control is offered.
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, channel_info
from .bridge import BridgeError
from .entity import EnetEntity
from .hub import Hub, Channel

_LOGGER = logging.getLogger(__name__)

# travel info GO 10: 0 stopped after moving up, 1 stopped after moving down (last direction, not the end position)
MOVING_UP, MOVING_DOWN = 2, 3
BASIC = (CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
         | CoverEntityFeature.SET_POSITION)
TILT = CoverEntityFeature.OPEN_TILT | CoverEntityFeature.CLOSE_TILT | CoverEntityFeature.SET_TILT_POSITION
DEVICE_CLASSES = {"BLINDS": CoverDeviceClass.BLIND, "SHUTTER": CoverDeviceClass.SHUTTER,
                  "MARQUEE": CoverDeviceClass.AWNING}


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(EnetBlind(hub, k) for k in hub.blinds)


class EnetBlind(EnetEntity, CoverEntity):
    def __init__(self, hub: Hub, channel: Channel) -> None:
        super().__init__(hub)
        self.channel = channel
        self._attr_unique_id = f"{channel.enet_device['sn']}_{channel.nr}"
        self._attr_device_info = channel_info(hub, channel)
        self._attr_name = None  # main function of the (channel) device

    @property
    def _z(self):
        return self.hub.state_[self.channel.key]

    @property
    def _mode(self) -> str | None:
        """Operating mode BLINDS/SHUTTER/MARQUEE from the cached actuator parameters (None = not read yet)."""
        sn = self.channel.enet_device["sn"]
        for g in self.hub.device_params(self.channel.enet_device):
            if g.channel == self.channel.nr:
                for p in g.params:
                    if p.id == "OPMODE":
                        return self.hub.params.value(sn, p)
        return None

    @property
    def _has_slats(self) -> bool:
        return self._mode in (None, "BLINDS")

    @property
    def device_class(self) -> CoverDeviceClass:
        return DEVICE_CLASSES.get(self._mode or "", CoverDeviceClass.BLIND)

    @property
    def supported_features(self) -> CoverEntityFeature:
        return BASIC | TILT if self._has_slats else BASIC

    @property
    def current_cover_position(self) -> int | None:
        return 100 - self._z.percent if self._z.known else None

    @property
    def current_cover_tilt_position(self) -> int | None:
        return None if self._z.slat is None or not self._has_slats else 100 - self._z.slat

    @property
    def is_closed(self) -> bool | None:
        return self._z.percent >= 100 if self._z.known else None

    @property
    def is_opening(self) -> bool:
        return self._z.motion == MOVING_UP

    @property
    def is_closing(self) -> bool:
        return self._z.motion == MOVING_DOWN

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"state_{self.channel.key}")
        self._listen("params", self._on_params)

    @callback
    def _on_params(self, sn: str) -> None:
        if sn == self.channel.enet_device["sn"]:
            self.async_write_ha_state()  # operating mode read or changed

    async def _send(self, action: str, percent: int | None = None) -> None:
        fields_: dict[str, Any] = {"action": action}
        if percent is not None:
            fields_["percent"] = percent
        try:
            async with self.hub.params.programming(self.channel.ia):  # not in the middle of a parameter download
                result = await self.hub.bridge.request("cover", ia=self.channel.ia, ch=self.channel.nr, **fields_)
        except BridgeError as err:
            raise HomeAssistantError(f"{self.channel.enet_device['name']}: {err}") from err
        self._z.reported = time.monotonic()
        if result.get("tries", 1) > 1:
            _LOGGER.log(logging.WARNING if result["tries"] >= 3 else logging.INFO,
                        "%s: cover %s acknowledged only after %d tries", self.channel.enet_device["name"], action,
                        result["tries"])

    def _moving(self, motion: int | None) -> None:
        """Provisional until the actuator's travel info or the bridge's poll arrives."""
        self._z.motion = motion
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._send("up")
        self._moving(MOVING_UP)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._send("down")
        self._moving(MOVING_DOWN)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._send("stop")
        self._moving(None)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        target = 100 - int(kwargs[ATTR_POSITION])  # actuator: 0 % = top
        await self._send("position", target)
        if self._z.known and target != self._z.percent:
            self._moving(MOVING_DOWN if target > self._z.percent else MOVING_UP)

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        await self._send("slat", 0)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        await self._send("slat", 100)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        await self._send("slat", 100 - int(kwargs[ATTR_TILT_POSITION]))

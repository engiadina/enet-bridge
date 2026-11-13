"""Blind channels (CT_1F03, e.g. Gira 5427 00) as roller shutter/blind.

Tested with a Gira 5427 00 (office, 2026-11-13). The actuator tracks its position only after the travel time has been
written once (as the eNet Server does when commissioning – pair.py writes it after pairing). The actuator counts the position from the top (0 % = fully
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
from .hub import MOVING_DOWN, MOVING_UP, Hub, Channel  # motion: hub.ChannelState
from .visibility import filtered

_LOGGER = logging.getLogger(__name__)

BASIC = (CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
         | CoverEntityFeature.SET_POSITION)
TILT = CoverEntityFeature.OPEN_TILT | CoverEntityFeature.CLOSE_TILT | CoverEntityFeature.SET_TILT_POSITION
DEVICE_CLASSES = {"BLINDS": CoverDeviceClass.BLIND, "SHUTTER": CoverDeviceClass.SHUTTER,
                  "MARQUEE": CoverDeviceClass.AWNING}


@filtered
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

    def _actuator_target(self, real: float) -> int:  # 0…255 (raw, GO 5)
        """Actuator position for a hanging position (hub._blind_model): moving down, the actuator turns the slats
        back by the slat time after arriving, moving up it ends with the slats turned up – so the target differs by
        the slat time depending on the direction."""
        t = self.hub.blind_times(self.channel.key)
        if t is None or self._z.h is None:
            return round(real * 2.55)
        travel, m = t
        h = real / 100 * (travel - m)
        target_t = h + m if real > self._z.h else h
        return max(0, min(255, round(target_t / travel * 255)))

    def _real_position(self) -> float:
        z = self._z
        return z.h if z.h is not None else float(z.percent)

    @property
    def current_cover_position(self) -> int | None:
        return 100 - round(self._real_position()) if self._z.known else None

    @property
    def current_cover_tilt_position(self) -> int | None:
        z = self._z
        slat = z.s if z.s is not None else z.slat
        return None if slat is None or not self._has_slats else 100 - round(slat)

    @property
    def is_closed(self) -> bool | None:
        return round(self._real_position()) >= 100 if self._z.known else None

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

    async def _send(self, action: str, percent: int | None = None, raw: int | None = None) -> None:
        fields_: dict[str, Any] = {"action": action}
        if percent is not None:
            fields_["percent"] = percent
        if raw is not None:  # 0…255, firmware 0.8.27+ (the integration installs it automatically)
            fields_["raw"] = raw
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

    def _moving(self, motion: int | None, target: int | None = None) -> None:
        """Provisional until position reports of the actuator confirm or end it (hub.blind_moving)."""
        self.hub.blind_moving(self.channel, motion, target)
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
        real = 100 - int(kwargs[ATTR_POSITION])  # actuator: 0 % = top
        raw = self._actuator_target(real)
        target = round(raw / 2.55)
        await self._send("position", raw=raw)
        now = self._real_position()
        if self._z.known and round(now) != real:
            # without slats the target position ends the movement; with slats the actuator turns them afterwards
            self._moving(MOVING_DOWN if real > now else MOVING_UP, None if self.hub.blind_times(self.channel.key) else target)

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        await self._send("slat", 0)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        await self._send("slat", 100)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        await self._send("slat", 100 - int(kwargs[ATTR_TILT_POSITION]))

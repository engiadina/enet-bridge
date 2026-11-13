"""Measure the travel time of a blind channel on its device page (buttons "Measure travel time", "End position
reached", "Cancel measuring", sensor "Measuring").

The actuator knows one travel time for both directions (parameter MUDT) and switches off after it – so for the
measurement it is set to the maximum first. Sequence: up to the top end position (not timed), down (timed), up
(timed); each "End position reached" sends stop. Result: the longer of both times, rounded up (moving up under load
usually takes a little longer). Cancelling stops the blind and writes the previous travel time back.
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .bridge import BridgeError
from .texts import tr

if TYPE_CHECKING:
    from .hub import Channel, Hub

_LOGGER = logging.getLogger(__name__)

MAX_TRAVEL_S = 600  # catalogue maximum of MUDT
IDLE, TO_TOP, DOWN, UP = "bereit", "nach_oben", "ab", "auf"


class Calibration:
    """Measuring state of one blind channel."""

    def __init__(self, hub: Hub, channel: Channel) -> None:
        self.hub, self.channel = hub, channel
        self.step = IDLE
        self.old: int | None = None  # travel time before the measurement
        self.started = 0.0
        self.down: float | None = None
        self.up: float | None = None
        self.result: str | None = None  # text of the last result (sensor)

    @property
    def key(self) -> str:
        return self.channel.key

    def _param(self):
        g = self.channel.enet_device
        for grp in self.hub.device_params(g):
            if grp.channel == self.channel.nr:
                for p in grp.params:
                    if p.id == "MUDT":
                        return grp, p
        raise BridgeError("bad_args", tr("einmessen_kein_parameter", name=g["name"]))

    async def _write(self, seconds: int) -> None:
        g = self.channel.enet_device
        grp, p = self._param()
        await self.hub.params.write(g["ia"], g["sn"], grp.device_obj, p, seconds)

    async def _move(self, action: str) -> None:
        async with self.hub.params.programming(self.channel.ia):
            await self.hub.bridge.request("cover", ia=self.channel.ia, ch=self.channel.nr, action=action)
        if action != "stop":
            from .hub import MOVING_DOWN, MOVING_UP

            self.hub.blind_moving(self.channel, MOVING_UP if action == "up" else MOVING_DOWN)
        else:
            self.hub.blind_moving(self.channel, None)

    def _changed(self) -> None:
        async_dispatcher_send(self.hub.hass, self.hub.signal(f"einmessen_{self.key}"))

    def text(self) -> str:
        if self.step == TO_TOP:
            return tr("einmessen_nach_oben")
        if self.step == DOWN:
            return tr("einmessen_ab")
        if self.step == UP:
            return tr("einmessen_auf")
        return self.result or tr("einmessen_bereit")

    async def start(self) -> None:
        if self.step != IDLE:
            raise BridgeError("busy", tr("einmessen_laeuft"))
        g = self.channel.enet_device
        grp, p = self._param()
        value = self.hub.params.value(g["sn"], p)
        self.old = int(value) if isinstance(value, (int, float)) and value > 0 else None
        self.down = self.up = self.result = None
        await self._write(MAX_TRAVEL_S)
        await self._move("up")
        self.step = TO_TOP
        self.hub.device_activity(g, tr("akt_einmessen_start"))
        self._changed()

    async def end_reached(self) -> None:
        now = time.monotonic()
        if self.step == IDLE:
            raise BridgeError("bad_args", tr("einmessen_nicht_gestartet"))
        await self._move("stop")
        if self.step == TO_TOP:
            await self._move("down")
            self.started, self.step = time.monotonic(), DOWN
        elif self.step == DOWN:
            self.down = now - self.started
            await self._move("up")
            self.started, self.step = time.monotonic(), UP
        elif self.step == UP:
            self.up = now - self.started
            seconds = max(1, min(MAX_TRAVEL_S, math.ceil(max(self.down or 0, self.up))))
            self.step = IDLE
            try:
                await self._write(seconds)
            except BridgeError:
                self._changed()
                raise
            self.result = tr("einmessen_fertig", ab=f"{self.down:.1f}", auf=f"{self.up:.1f}", s=seconds)
            self.hub.device_activity(self.channel.enet_device, self.result)
            _LOGGER.info("%s: travel time measured: down %.1f s, up %.1f s → %d s",
                         self.channel.enet_device["name"], self.down or 0, self.up, seconds)
        self._changed()

    async def cancel(self) -> None:
        if self.step == IDLE:
            return
        self.step = IDLE
        self.result = tr("einmessen_abgebrochen")
        self._changed()
        try:
            await self._move("stop")
        finally:
            if self.old is not None:
                await self._write(self.old)
            self.hub.device_activity(self.channel.enet_device, self.result)
            self._changed()


def calibration(hub: Hub, channel: Channel) -> Calibration:
    """The measuring state of a blind channel (kept in the hub for all entities of the channel)."""
    if channel.key not in hub.calibrations:
        hub.calibrations[channel.key] = Calibration(hub, channel)
    return hub.calibrations[channel.key]

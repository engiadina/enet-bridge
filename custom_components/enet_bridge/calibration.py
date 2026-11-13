"""Measure the travel time of a blind channel on its device page.

Normally the page has the button "Measure travel time". Pressing it switches the page to only "Position reached" and
"Cancel measuring" (visibility.py removes the other entities of the device and restores them afterwards); every step is
announced in the device's activity list. The actuator knows one travel time for both directions (parameter MUDT) and
switches off after it – so for the measurement it is set to the maximum first. Sequence: up to the top end position
(not timed), down (timed), up (timed); each "Position reached" sends stop. Result: the longer of both times, rounded up
(moving up under load usually takes a little longer). Cancelling stops the blind and writes the previous travel time
back. The state lives in hass.data, so it survives the reloads that switch the page.
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .bridge import BridgeError
from .logbook import EVENT_DEVICE_MESSAGE
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
        self.pending: str | None = None  # message for after the page switch (logged by the new buttons)

    @property
    def running(self) -> bool:
        return self.step != IDLE

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

    def _log(self, text: str) -> None:
        """Activity list of the device (logbook.py: an entry of the device, not of one of its buttons)."""
        from . import channel_info

        g = self.channel.enet_device
        ident = next(iter(channel_info(self.hub, self.channel)["identifiers"]))
        device = dr.async_get(self.hub.hass).async_get_device(identifiers={ident})
        self.hub.hass.bus.async_fire(EVENT_DEVICE_MESSAGE, {"device_id": device.id if device else None,
                                                             "name": g["name"], "message": text})

    def status(self) -> str:
        """Step sensor while measuring: what to do now (changes only per step – no logbook flood)."""
        if self.step == TO_TOP:
            return tr("einmessen_status_oben")
        if self.step == DOWN:
            return tr("einmessen_status_ab")
        if self.step == UP:
            return tr("einmessen_status_auf", ab=f"{self.down or 0:.1f}")
        return ""

    def elapsed(self) -> float | None:
        """Running time of the timed run (seconds), None while moving to the top first."""
        return round(time.monotonic() - self.started, 1) if self.step in (DOWN, UP) else None

    def _changed(self) -> None:
        async_dispatcher_send(self.hub.hass, self.hub.signal(f"einmessen_{self.channel.key}"))

    def _switch_page(self, message: str) -> None:
        """Reload: the device page shows the measuring buttons or its normal entities again (visibility.py). The
        message goes to the activity list once the new page exists (flush, called by the buttons)."""
        self.pending = message
        self.hub.hass.config_entries.async_schedule_reload(self.hub.entry_id)

    def flush(self) -> None:
        if self.pending:
            text, self.pending = self.pending, None
            self._log(text)

    async def start(self) -> None:
        if self.running:
            raise BridgeError("busy", tr("einmessen_laeuft"))
        g = self.channel.enet_device
        grp, p = self._param()
        value = self.hub.params.value(g["sn"], p)
        self.old = int(value) if isinstance(value, (int, float)) and value > 0 else None
        self.down = None
        await self._write(MAX_TRAVEL_S)
        await self._move("up")
        self.step = TO_TOP
        self._switch_page(tr("einmessen_schritt_oben"))

    async def end_reached(self) -> None:
        now = time.monotonic()
        if not self.running:
            raise BridgeError("bad_args", tr("einmessen_nicht_gestartet"))
        await self._move("stop")
        if self.step == TO_TOP:
            await self._move("down")
            self.started, self.step = time.monotonic(), DOWN
            self._log(tr("einmessen_schritt_ab"))
            self._changed()
        elif self.step == DOWN:
            self.down = now - self.started
            await self._move("up")
            self.started, self.step = time.monotonic(), UP
            self._log(tr("einmessen_schritt_auf", ab=f"{self.down:.1f}"))
            self._changed()
        elif self.step == UP:
            up = now - self.started
            seconds = max(1, min(MAX_TRAVEL_S, math.ceil(max(self.down or 0, up))))
            text = tr("einmessen_fertig", ab=f"{self.down or 0:.1f}", auf=f"{up:.1f}", s=seconds)
            try:
                await self._write(seconds)
            except BridgeError as err:
                text = tr("einmessen_fehler", err=err)
                raise
            finally:
                self.step = IDLE
                self._switch_page(text)
            _LOGGER.info("%s: travel time measured: down %.1f s, up %.1f s → %d s",
                         self.channel.enet_device["name"], self.down or 0, up, seconds)

    async def cancel(self) -> None:
        if not self.running:
            return
        try:
            await self._move("stop")
            if self.old is not None:
                await self._write(self.old)
        finally:
            self.step = IDLE
            self._switch_page(tr("einmessen_abgebrochen"))


def calibration(hub: Hub, channel: Channel) -> Calibration:
    """The measuring state of a blind channel (kept across reloads; hub and channel are renewed on every setup)."""
    c = hub.calibrations.get(channel.key)
    if c is None:
        c = hub.calibrations[channel.key] = Calibration(hub, channel)
    c.hub, c.channel = hub, channel
    return c

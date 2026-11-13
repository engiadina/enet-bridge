"""Measure the travel time of a blind channel on its device page – the procedure of the actuator manual (Gira 5427 00,
"Behanglaufzeit speichern"), depending on the operating mode:

- roller shutter: fully up, then fully down – the time down is the travel time (MUDT);
- venetian blind: fully up, fully down, then reverse the slats (moving up) – travel time and slat time (MSMT);
- awning: fully retracted, fully extended, then tension the fabric (moving in) – travel time and tensioning time (MSMT).

Normally the page has the button "Measure travel time". Pressing it switches the page to only "Position reached",
"Cancel measuring" and the step/running-time sensors (visibility.py removes the other entities of the device and
restores them afterwards); every step is announced in the device's activity list. The actuator switches off after its
travel time – so for the measurement it is set to the maximum first. Each "Position reached" sends stop. At the end the
values are written and the blind moves back to the top, as with the manual procedure. Cancelling stops the blind and
writes the previous travel time back. The state lives in hass.data, so it survives the reloads that switch the page.
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .bridge import BridgeError
from .logbook import EVENT_DEVICE_MESSAGE
from .texts import tr

if TYPE_CHECKING:
    from .hub import Channel, Hub

_LOGGER = logging.getLogger(__name__)

MAX_TRAVEL_S = 600  # catalogue maximum of MUDT
IDLE, TO_TOP, DOWN, SLATS = "bereit", "nach_oben", "ab", "lamellen"
SHUTTER, BLINDS, MARQUEE = "SHUTTER", "BLINDS", "MARQUEE"


class Calibration:
    """Measuring state of one blind channel."""

    def __init__(self, hub: Hub, channel: Channel) -> None:
        self.hub, self.channel = hub, channel
        self.step = IDLE
        self.mode = SHUTTER
        self.old: int | None = None  # travel time before the measurement
        self.started = 0.0
        self.down: float | None = None
        self.pending: str | None = None  # message for after the page switch (logged by the new buttons)

    @property
    def running(self) -> bool:
        return self.step != IDLE

    # --- Parameters ---------------------------------------------------------
    def _param(self, ident: str):
        g = self.channel.enet_device
        for grp in self.hub.device_params(g):
            if grp.channel == self.channel.nr:
                for p in grp.params:
                    if p.id == ident:
                        return grp, p
        return None, None

    async def _write(self, ident: str, value: Any) -> None:
        g = self.channel.enet_device
        grp, p = self._param(ident)
        if p is None:
            raise BridgeError("bad_args", tr("einmessen_kein_parameter", name=g["name"]))
        self.hub.param_active(grp, p)  # value type for the current operating mode (slat time: range)
        if isinstance(value, (int, float)):
            value = max(p.typ.get("min", value), min(p.typ.get("max", value), value))
        await self.hub.params.write(g["ia"], g["sn"], grp.device_obj, p, value)

    async def _move(self, action: str) -> None:
        async with self.hub.params.programming(self.channel.ia):
            await self.hub.bridge.request("cover", ia=self.channel.ia, ch=self.channel.nr, action=action)

    # --- Messages -------------------------------------------------------------
    def _t(self, key: str, **values: Any) -> str:
        """Text for the operating mode: awning texts say retracted/extended/fabric instead of up/down/slats."""
        return tr(f"{key}_markise" if self.mode == MARQUEE else key, **values)

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
            return self._t("einmessen_status_oben")
        if self.step == DOWN:
            return self._t("einmessen_status_ab")
        if self.step == SLATS:
            return self._t("einmessen_status_lamellen", ab=f"{self.down or 0:.1f}")
        return ""

    def elapsed(self) -> float | None:
        """Running time of the timed run (seconds), None while moving to the top first."""
        return round(time.monotonic() - self.started, 1) if self.step in (DOWN, SLATS) else None

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

    # --- Steps ------------------------------------------------------------------
    async def start(self) -> None:
        if self.running:
            raise BridgeError("busy", tr("einmessen_laeuft"))
        g = self.channel.enet_device
        grp, p = self._param("MUDT")
        if p is None:
            raise BridgeError("bad_args", tr("einmessen_kein_parameter", name=g["name"]))
        value = self.hub.params.value(g["sn"], p)
        self.old = int(value) if isinstance(value, (int, float)) and value > 0 else None
        grp, mode = self._param("OPMODE")
        self.mode = (self.hub.params.value(g["sn"], mode) if mode is not None else None) or SHUTTER
        self.down = None
        await self._write("MUDT", MAX_TRAVEL_S)
        await self._move("up")
        self.step = TO_TOP
        self._switch_page(self._t("einmessen_schritt_oben"))

    async def end_reached(self) -> None:
        now = time.monotonic()
        if not self.running:
            raise BridgeError("bad_args", tr("einmessen_nicht_gestartet"))
        await self._move("stop")
        if self.step == TO_TOP:
            await self._move("down")
            self.started, self.step = time.monotonic(), DOWN
            self._log(self._t("einmessen_schritt_ab"))
            self._changed()
        elif self.step == DOWN:
            self.down = now - self.started
            if self.mode in (BLINDS, MARQUEE):  # then reverse the slats / tension the fabric: moving up/in
                await self._move("up")
                self.started, self.step = time.monotonic(), SLATS
                self._log(self._t("einmessen_schritt_lamellen", ab=f"{self.down:.1f}"))
                self._changed()
            else:
                await self._finish(None)
        elif self.step == SLATS:
            await self._finish(now - self.started)

    async def _finish(self, slats: float | None) -> None:
        seconds = max(1, min(MAX_TRAVEL_S, math.ceil(self.down or 0)))
        text = self._t("einmessen_fertig", s=seconds) if slats is None else \
            self._t("einmessen_fertig_lamellen", s=seconds, l=f"{slats:.1f}")
        try:
            await self._write("MUDT", seconds)
            if slats is not None:
                await self._write("MSMT", round(slats * 1000))
            await self._move("up")  # like the manual procedure: back to the top end position
        except BridgeError as err:
            text = tr("einmessen_fehler", err=err)
            raise
        finally:
            self.step = IDLE
            self._switch_page(text)
        _LOGGER.info("%s: travel time measured: %d s%s", self.channel.enet_device["name"], seconds,
                     "" if slats is None else f", slats/fabric {slats:.1f} s")

    async def cancel(self) -> None:
        if not self.running:
            return
        try:
            await self._move("stop")
            if self.old is not None:
                await self._write("MUDT", self.old)
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

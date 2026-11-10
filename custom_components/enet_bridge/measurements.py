"""Measuring devices without a lamp: energy sensor (CT_1F19), motion detector (CT_1F1B), repeater (CT_1F06).

Implemented from the catalog, not yet tested on a device (devices are marked "untested" in HA). Assumptions:
- The devices send their values under their own SN to GA = base·256 + channel offset + GO (like actuators and the
  sun sensor); only the lower GA byte is compared. The bridge reports them as a key event `wert1`/`wert`/`wert4`
  (1, 2 or 4 bytes).
- The energy sensor is mains powered and answers reads of its group objects like an actuator (bridge command
  "read", handle = offset + GO): HA polls the values regularly in case it does not send them itself.
- Motion detector: "Timed Start" (GO 3, retrigger GO 4, DPT 9.010 seconds) switches the linked actuators on for
  this time → motion detected, off after it expires without retriggering. Brightness GO 7 (DPT 9.004).
- Repeater: only reachable or not (rare poll, signal and last report).
Telegrams that cannot be assigned here end up in `unbekannt` (diagnostics download).
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .bridge import BridgeError
from .params import dpt9_decode

_LOGGER = logging.getLogger(__name__)

KIND_ENERGY, KIND_MOTION, KIND_REPEATER = "energie", "bewegung", "repeater"
MEASUREMENT_KINDS = (KIND_ENERGY, KIND_MOTION, KIND_REPEATER)
# device types whose mapping comes only from the catalog ("untested" shown on the device)
UNTESTED = {"DVT_ES1M", "DVT_ES1Z", "DVT_ES4R", "DVT_BS1BP", "DVT_RPZS",
              "DVT_JA1M", "DVT_JA1R", "DVT_SJA1", "DVT_SJAR"}

POLL_S = 60  # energy sensor: poll voltage, current, active power and energy this often
RARE = 5  # reactive/apparent power only every n-th round
REPEATER_ROUNDS = 10  # repeater: address every n-th round
MOTION_MIN_S = 10  # motion goes off no earlier than this long after the last trigger


def _dpt14(b: bytes) -> float:
    return round(struct.unpack(">f", b)[0], 3)


def _dpt13(b: bytes) -> int:
    return struct.unpack(">i", b)[0]


@dataclass(frozen=True)
class Value:
    key: str  # translation key of the entity and part of the unique_id
    length: int  # bytes
    decode: Callable[[bytes], float]
    factor: float = 1.0
    rare: bool = False  # energy sensor: poll only every RARE-th round


# GO → value per channel kind (catalog ChannelTypes.xml, fw2.21)
VALUES: dict[str, dict[int, Value]] = {
    KIND_ENERGY: {
        3: Value("spannung", 2, dpt9_decode, 0.001),  # DPT 9.020 mV → V
        4: Value("strom", 2, dpt9_decode, 0.001),  # DPT 9.021 mA → A
        5: Value("wirkleistung", 4, _dpt14),  # DPT 14.056 W
        6: Value("blindleistung", 4, _dpt14, rare=True),
        7: Value("scheinleistung", 4, _dpt14, rare=True),
        8: Value("energie", 4, _dpt13, 0.001),  # DPT 13.010 Wh → kWh
    },
    KIND_MOTION: {
        3: Value("ausloesung", 2, dpt9_decode),  # DPT 9.010 s
        4: Value("nachtriggern", 2, dpt9_decode),
        7: Value("helligkeit", 2, dpt9_decode),  # DPT 9.004 lx
    },
    KIND_REPEATER: {},
}
SPAN = {KIND_ENERGY: 9, KIND_MOTION: 8, KIND_REPEATER: 1}  # highest GO per channel
LENGTHS = {"wert1": 1, "wert": 2, "wert4": 4}


def is_measuring_device(enet_device: dict[str, Any]) -> bool:
    return enet_device.get("rolle") == "sensor" and any(k["art"] in MEASUREMENT_KINDS for k in enet_device.get("kanaele", []))


class Measurements:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.values_: dict[str, float] = {}  # "SN/channel/key" → value
        self.motion: dict[str, bool] = {}  # "SN/channel" → motion detected
        self.unknown: list[str] = []  # unassigned telegrams (diagnostics, the last 20)
        self._off_timers: dict[str, Callable[[], None]] = {}
        self._task: asyncio.Task | None = None

    def devices_(self) -> list[dict[str, Any]]:
        return [g for g in self.hub.installation.get("geraete", []) if is_measuring_device(g)]

    def channels_(self, art: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return [(g, k) for g in self.devices_() for k in g["kanaele"]
                if k["art"] in MEASUREMENT_KINDS and (art is None or k["art"] == art)]

    def start(self) -> None:
        if self.channels_(KIND_ENERGY) or self.channels_(KIND_REPEATER):
            self._task = self.hub.hass.async_create_background_task(self._poll_loop(), "enet_bridge Messwerte")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for off in self._off_timers.values():
            off()
        self._off_timers.clear()

    def _remember(self, text: str) -> None:
        self.unknown = (self.unknown + [f"{dt_util.now().isoformat(timespec='seconds')} {text}"])[-20:]

    def _set(self, sn: str, nr: int, key: str, value: float) -> None:
        self.values_[f"{sn}/{nr}/{key}"] = value
        async_dispatcher_send(self.hub.hass, self.hub.signal(f"messwert_{sn}_{nr}"))

    def _diag(self, g: dict[str, Any], rssi: Any) -> None:
        from .hub import Diagnose  # avoid circular import

        d = self.hub.diag.setdefault(g["ia"], Diagnose())
        d.time_ = dt_util.now()
        if rssi is not None:
            d.rssi = rssi
        async_dispatcher_send(self.hub.hass, self.hub.signal(f"diag_{g['ia']}"))

    @callback
    def telegram(self, msg: dict[str, Any]) -> bool:
        """Evaluate a key event of a measuring device. Returns True if the sender is a measuring device."""
        sn = msg.get("sn")
        g = next((g for g in self.devices_() if g["sn"] == sn), None)
        if g is None:
            return False
        self._diag(g, msg.get("rssi"))
        kind, ga = msg.get("kind"), int(msg.get("ga", -1))
        if kind not in LENGTHS or not msg.get("cmd"):
            _LOGGER.debug("%s: %s telegram to GA %d not evaluated", g["name"], kind, ga)  # e.g. SumstatusGet
            self._remember(f"{g['name']} {kind} GA {ga} {msg.get('cmd')}")
            return True
        data_ = bytes.fromhex(msg["cmd"])[-LENGTHS[kind]:]
        for k in g["kanaele"]:
            go = (ga & 0xFF) - k["offset"]
            if k["art"] in MEASUREMENT_KINDS and 1 <= go <= SPAN[k["art"]]:
                w = VALUES[k["art"]].get(go)
                if w is None or w.length != len(data_):
                    break
                self._value(g, k, w, data_)
                return True
        _LOGGER.info("%s: value telegram not assigned yet (GA %d, %s %s)", g["name"], ga, kind, msg["cmd"])
        self._remember(f"{g['name']} {kind} GA {ga} {msg['cmd']}")
        return True

    def _value(self, g: dict[str, Any], k: dict[str, Any], w: Value, data_: bytes) -> None:
        value = round(w.decode(data_) * w.factor, 3)
        if k["art"] == KIND_MOTION and w.key in ("ausloesung", "nachtriggern"):
            self._motion(g["sn"], k["nr"], value)
            return
        self._set(g["sn"], k["nr"], w.key, value)

    def _motion(self, sn: str, nr: int, duration_s: float) -> None:
        key = f"{sn}/{nr}"
        if off := self._off_timers.pop(key, None):
            off()

        @callback
        def _end(_now: Any) -> None:
            self._off_timers.pop(key, None)
            self.motion[key] = False
            async_dispatcher_send(self.hub.hass, self.hub.signal(f"bewegung_{sn}_{nr}"))

        self.motion[key] = True
        self._off_timers[key] = async_call_later(self.hub.hass, max(MOTION_MIN_S, duration_s), _end)
        async_dispatcher_send(self.hub.hass, self.hub.signal(f"bewegung_{sn}_{nr}"))

    def _may_send(self) -> bool:
        h = self.hub
        return (h.bridge.connected and not h.flashing and not h.no_bridge and not h.listen_only
                and bool(h.radio.get("ok")))

    async def _read(self, g: dict[str, Any], handle: int) -> bytes | None:
        try:
            r = await self.hub.bridge.request("read", ia=g["ia"], handle=handle)
        except BridgeError as err:
            _LOGGER.debug("%s: read handle %d: %s", g["name"], handle, err)
            if err.err == "nack":  # rejected, but answered: reachable
                self._diag(g, None)
            return None
        return bytes.fromhex(r.get("value", ""))

    async def _poll_loop(self) -> None:
        round_ = 0
        while True:
            await asyncio.sleep(POLL_S)
            if not self._may_send():
                continue
            for g, k in self.channels_(KIND_ENERGY):
                for go, w in VALUES[KIND_ENERGY].items():
                    if w.rare and round_ % RARE:
                        continue
                    data_ = await self._read(g, k["offset"] + go)
                    if data_ and len(data_) >= w.length:
                        self._value(g, k, w, data_[-w.length:])
            if round_ % REPEATER_ROUNDS == 0:
                for g, k in self.channels_(KIND_REPEATER):
                    await self._read(g, k["offset"] + 1)  # an answer (even a rejection) counts as "reachable"
            round_ += 1

    def diagnose(self) -> dict[str, Any]:
        return {"werte": dict(self.values_), "bewegung": dict(self.motion), "unbekannt": list(self.unknown),
                "stand": time.strftime("%H:%M:%S")}

"""Device parameters: definitions (from tools/katalog.py), conversion, reading and writing over the radio.

Parameter memory of a channel = channel object PID 79 (0x4F), element = byte (NOTES.md "Gerätekatalog").
Writing as the eNet Server does (capture 2026-10-23, NOTES.md "Parameter übertragen").
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .bridge import Bridge, BridgeError
from .const import DOMAIN
from .texts import german, tr

_LOGGER = logging.getLogger(__name__)

PID_PARAM = 79
MAX_BLOCK = 15  # A_PropertyValue: count is 4 bits

# Own names (app terms, German/English) and whether the entity is enabled by default. Parameters without an entry
# are named as in the catalog (English).
NAMES: dict[str, tuple[str, str, bool]] = {
    "OSV": ("Einschalthelligkeit", "Switch-on brightness", True),
    "MIN_SV": ("Minimalhelligkeit", "Minimum brightness", True),
    "MAX_SV": ("Maximalhelligkeit", "Maximum brightness", True),
    "IOND": ("Einschaltverzögerung", "Switch-on delay", True),
    "IOFFD": ("Ausschaltverzögerung", "Switch-off delay", True),
    "DTU": ("Aufdimmrampe", "Dim-up ramp", True),
    "DTD": ("Abdimmrampe", "Dim-down ramp", True),
    "RDT": ("Dimm-Verstellzeit", "Dimming time", True),
    "ITOD": ("Nachlaufzeit", "Run-on time", True),
    "BMPU": ("Verhalten bei Netzwiederkehr", "Behaviour on power return", True),
    "MPUSV": ("Helligkeit bei Netzwiederkehr", "Brightness on power return", True),
    "BTMPU": ("Zeitfunktion bei Netzwiederkehr", "Time function on power return", False),
    "LAM": ("Betriebsart", "Operating mode", True),
    "DALT": ("Lastart", "Load type", True),
    "IOS": ("Ausgang invertieren", "Invert output", True),
    "MCE": ("Bedienung am Gerät", "Operation on the device", False),
    "LME": ("Lernmodus am Gerät", "Learn mode on the device", False),
    "BUL": ("Verhalten nach Sperre", "Behaviour after lock", False),
    "SLME": ("Szenenwerte am Gerät speichern", "Store scene values on the device", False),
    "BOOE": ("Aus bei Helligkeitsüberschreitung", "Off when brightness is exceeded", False),
    "BUOE": ("Ein bei Helligkeitsunterschreitung", "On when brightness falls below", False),
    "BBE": ("Grundhelligkeit", "Base brightness", False),
    "PWE": ("Abschaltvorwarnung", "Switch-off warning", False),
    "SRT": ("Blinkzeit", "Flash time", False),
    # blind actuators (CT_1F03 and variants)
    "MUDT": ("Fahrzeit", "Travel time", True),
    "RPT": ("Umschaltpause", "Reversal pause", True),
    "OPMODE": ("Betriebsart", "Operating mode", True),
    "BPMPU": ("Position bei Netzwiederkehr", "Position on power return", True),
    "EIM": ("Fahrrichtung umkehren", "Invert direction", True),
    "MSMT": ("Lamellenumsteuerzeit", "Slat reversing time", True),  # awning: fabric tensioning time (entity.py)
    "SPMPU": ("Lamellenposition bei Netzwiederkehr", "Slat position on power return", True),
    "CAPS": ("Position senden", "Send position", False),
    "BNRT": ("Umkehrsperrzeit", "No-reversal time", False),
    "SST": ("Lamellen-Schrittzeit", "Slat step time", True),
    "REPEATER_MODE_ENABLE": ("Repeater", "Repeater", False),
    "LEARN_MODE_ENABLE": ("Lernmodus", "Learn mode", False),
    "SUMSTATE_MAX_TIMEOUT": ("Statusmeldung Wartezeit", "Status message timeout", False),
    # sun sensor (DVT_SF1S, channel type CT_1F18); channel prefix "sun protection"/"twilight" from CHANNEL_NAMES
    "PT_INThScS.THV1": ("Schwelle", "Threshold", True),
    "PT_INThScS.ASNVE": ("Szene bei Überschreitung", "Scene when exceeded", True),
    "PT_INThScS.ASNVU": ("Szene bei Unterschreitung", "Scene when below", True),
    "PT_INThScS.MCE": ("Bedienung am Gerät", "Operation on the device", False),
    "PT_INThScS.LME": ("Lernmodus am Gerät", "Learn mode on the device", False),
    "CHANNEL_1_SCENE_SENSOR_MODE": ("Funktion", "Function", False),
    "CHANNEL_2_SCENE_SENSOR_MODE": ("Funktion", "Function", False),
    "CHANNEL_1_ACTUAL_SCENE_NUMBER": ("Szenennummer", "Scene number", True),
    "CHANNEL_2_ACTUAL_SCENE_NUMBER": ("Szenennummer", "Scene number", True),
    "CHANNEL_1_SUMSTATE_MODE": ("Statusmeldung", "Status message", False),
    "CHANNEL_2_SUMSTATE_MODE": ("Statusmeldung", "Status message", False),
    "CHARGE_VOLTAGE": ("Ladespannung", "Charging voltage", True),
}
# channel names per device type (prefix of the parameter entities for devices without their own channel devices)
CHANNEL_NAMES: dict[str, dict[int, tuple[str, str]]] = {
    "DVT_SF1S": {1: ("Sonnenschutz", "Sun protection"), 2: ("Dämmerung", "Twilight")}}
CHOICE: dict[str, tuple[str, str]] = {
    "OFF": ("Aus", "Off"), "ON": ("Ein", "On"), "NO_CHANGE": ("Keine Änderung", "No change"),
    "VALUE_BEFORE_LOCKING": ("Wert vor Sperre", "Value before lock"),
    "LAST_VALUE_BEFORE_POWER_DOWN": ("Wert vor Netzausfall", "Value before power failure"),
    "VALUE_ACCORDING_TO_ADDITIONAL_PARAMETER": ("Wert laut Parameter", "Value per parameter"),
    "BLINDS": ("Jalousie", "Venetian blind"), "SHUTTER": ("Rollladen", "Roller shutter"),
    "MARQUEE": ("Markise", "Awning"),
    "NORMAL_OPERATION": ("Normalbetrieb", "Normal operation"), "NON_LATCHING": ("Tastbetrieb", "Momentary"),
    "BLINKING": ("Blinken", "Flashing"), "PERMANENT_ON": ("Dauer-Ein", "Permanently on"),
    "PERMANENT_OFF": ("Dauer-Aus", "Permanently off"),
    "CAPACITIVE": ("Kapazitiv (Phasenabschnitt)", "Capacitive (trailing edge)"),
    "INDUCTIVE": ("Induktiv (Phasenanschnitt)", "Inductive (leading edge)"), "UNIVERSAL": ("Universal", "Universal"),
    "LED_CAPACITIVE": ("LED Phasenabschnitt", "LED trailing edge"), "LED_INDUCTIVE": ("LED Phasenanschnitt", "LED leading edge"),
    "RESTART": ("Neu starten", "Restart"), "SP_SUNPROTECTION": ("Sonnenschutz", "Sun protection"),
    "TF_TWILIGHT_FNC": ("Dämmerung", "Twilight"), "NO_OPTIONALS": ("Keine", "None"),
    "OFF_3_REPETITIONS_UNLINKED": ("Aus (3 Wiederholungen, unverknüpft)", "Off (3 repetitions, unlinked)"),
}
COUNTERS = {"SWITCH_COUNTER": ("Schaltspiele", "Switching cycles"),
           "ACTIVE_COUNTER_RESETABLE": ("Betriebsstunden (rücksetzbar)", "Operating hours (resettable)"),
           "ACTIVE_COUNTER": ("Betriebsstunden", "Operating hours")}  # order: RESETABLE before ACTIVE_COUNTER


def _language(paar: tuple[str, str]) -> str:
    return paar[0] if german() else paar[1]


def choice(value: str) -> str:
    """Display of a choice value (catalog identifier) in the configured language."""
    return _language(CHOICE[value]) if value in CHOICE else value


def channel_name(typ: str, channel: int | None) -> str | None:
    """Channel prefix for parameter entities (e.g. sun sensor: "sun protection"), otherwise None."""
    paar = CHANNEL_NAMES.get(typ, {}).get(channel) if channel is not None else None
    return _language(paar) if paar else None


# scene memory belongs to scene handling, not to the settings – except the special functions 42–51 (lock-out
# protection, forced operation, wind alarm, sun protection, twilight: priority and value on call/cancel, as in the
# manuals' parameter lists)
EXCLUDED = ("SCV1[", "SCV2[", "SNA[", "PSN[", "CHANNEL_CONFIG")
SPECIAL = re.compile(r"^(SCV1|SCV2)\[(4[2-9]|5[01])\]$|^PSN\[(42|44|46|48|50)\]$")
SPECIAL_FUNCTIONS = {42: ("Aussperrschutz", "lock-out protection"), 44: ("Zwangsführung", "forced operation"),
                     46: ("Windalarm", "wind alarm"), 48: ("Sonnenschutz", "sun protection"),
                     50: ("Dämmerung", "twilight")}
SPECIAL_VALUE = {"jalousie": ("Position", "Position"), "dimmen": ("Helligkeit", "Brightness")}


def special_name(ident: str, kind: str | None) -> str | None:
    """Name of a special function parameter (SCV1/SCV2/PSN 42–51), e.g. "Windalarm aufheben: Position"."""
    m = SPECIAL.match(ident)
    if not m:
        return None
    idx = int(m.group(2) or m.group(3))
    func = _language(SPECIAL_FUNCTIONS[idx - idx % 2])
    if ident.startswith("PSN"):
        return f"{func}: " + ("Priorität" if german() else "priority")
    call = ("aufrufen" if german() else "call") if idx % 2 == 0 else ("aufheben" if german() else "cancel")
    value = _language(SPECIAL_VALUE.get(kind or "", ("Wert", "Value"))) if ident.startswith("SCV1") \
        else ("Lamellenstellung" if german() else "Slat position")
    return f"{func} {call}: {value}"


@dataclass
class Param:
    """A parameter of a device/channel with radio address and value description."""

    d: dict[str, Any]
    current: dict[str, Any] | None = field(default=None, compare=False)  # value type by dependency (Hub.param_active)
    kind: str | None = field(default=None, compare=False)  # channel kind (jalousie, dimmen, …) for special names

    @property
    def id(self) -> str:
        return self.d["id"]

    @property
    def key(self) -> str:
        return f"{self.d['obj']}/{self.d['pid']}/{self.d['start']}"

    @property
    def typ(self) -> dict[str, Any]:
        return self.current or self.d["typ"]

    @property
    def art(self) -> str:
        return self.typ.get("art", "")

    @property
    def read_only(self) -> bool:
        return self.d.get("schreiben", 0) == 0 or self.d.get("schreibgeschuetzt", False)

    @property
    def name(self) -> str:
        for ident, paar in COUNTERS.items():
            if ident in self.id:
                return _language(paar)
        if self.id in NAMES:
            de, en, _ = NAMES[self.id]
            return de if german() else en
        if special := special_name(self.id, self.kind):
            return special
        return self.d.get("name") or self.id

    @property
    def default_active(self) -> bool:
        return bool(SPECIAL.match(self.id)) or NAMES.get(self.id, ("", "", False))[2]

    @property
    def length(self) -> int:
        """Bytes in the device: PID 79 = 1 byte per element, otherwise element size from the bit length."""
        if self.d["pid"] == PID_PARAM:
            return self.d["anzahl"]
        return self.d["anzahl"] * max(1, self.typ.get("bits", 8) // 8)

    @property
    def usable(self) -> bool:
        # inactive by default but switched on by another parameter (e.g. slat time with operating mode BLINDS)
        return ((self.d.get("aktiv", True) or "abhaengig" in self.d)
                and self.typ.get("kodierung") not in (None, "unbekannt")
                and (not self.id.startswith(EXCLUDED) or bool(SPECIAL.match(self.id))))

    # --- Conversion ------------------------------------------------------
    def decode(self, raw: bytes) -> Any:
        enc = self.typ.get("kodierung")
        if enc == "dpt9":
            v = dpt9_decode(raw[:2])
        elif enc == "prozent255":
            v = round(raw[0] * 100 / 255)
        else:
            v = int.from_bytes(raw[: self.length], "big")
        if self.art == "bool":
            return v != 0
        if self.art == "auswahl":
            for k, n in self.typ.get("werte", {}).items():
                if n == v:
                    return k
            return None
        return v * self.typ.get("faktor", 1)

    def encode(self, value: Any) -> bytes:
        enc = self.typ.get("kodierung")
        if self.art == "bool":
            n = 1 if value else 0
        elif self.art == "auswahl":
            n = self.typ["werte"][value]
        elif enc == "dpt9":
            return dpt9_encode(float(value))
        elif enc == "prozent255":
            return bytes([min(255, math.floor(float(value) * 255 / 100))])
        else:
            n = int(round(float(value) / self.typ.get("faktor", 1)))
        return n.to_bytes(self.length, "big")


def dpt9_decode(b: bytes) -> float:
    """KNX 2-byte float: value = 0.01 · M · 2^E (M 12-bit two's complement with the sign in bit 15)."""
    raw = b[0] << 8 | b[1]
    exp = (raw >> 11) & 0x0F
    mant = raw & 0x07FF
    if raw & 0x8000:
        mant -= 2048
    return round(0.01 * mant * (1 << exp), 2)


def dpt9_encode(v: float) -> bytes:
    m = int(round(v * 100))
    exp = 0
    while not -2048 <= m <= 2047:
        m = int(round(m / 2))  # like the server: rounding for large values (2000 s → 3e1a)
        exp += 1
    raw = (exp << 11) | (m & 0x07FF) | (0x8000 if m < 0 else 0)
    return raw.to_bytes(2, "big")


def load_definitions(path_: str) -> dict[str, Any]:
    """Read definitionen.json (executor); empty if missing (the integration ships one)."""
    try:
        with open(path_, encoding="utf-8") as f:
            return json.load(f).get("typen", {})
    except FileNotFoundError:
        return {}


def blocks(params: list[Param]) -> list[tuple[int, int, int, int]]:
    """Read accesses (obj, pid, start, count): PID 79 merged into blocks ≤ 15 bytes, otherwise individually."""
    out: list[tuple[int, int, int, int]] = []
    p79 = sorted((p for p in params if p.d["pid"] == PID_PARAM), key=lambda p: (p.d["obj"], p.d["start"]))
    for p in p79:
        o, s, n = p.d["obj"], p.d["start"], p.length
        if out and out[-1][0] == o and out[-1][1] == PID_PARAM and s + n - out[-1][2] <= MAX_BLOCK \
                and s >= out[-1][2]:
            lo = out[-1]
            out[-1] = (o, PID_PARAM, lo[2], max(lo[3], s + n - lo[2]))
        else:
            out.append((o, PID_PARAM, s, n))
    out += [(p.d["obj"], p.d["pid"], p.d["start"], p.d["anzahl"]) for p in params if p.d["pid"] != PID_PARAM]
    return out


class ParamStore:
    """Cache of the raw values per device (SN → {key: hex}) with reading/writing via the bridge."""

    def __init__(self, hass: HomeAssistant, entry_id: str, bridge: Bridge, signal: str) -> None:
        self.hass = hass
        self.bridge = bridge
        self.signal = signal
        self._store: Store = Store(hass, 1, f"{DOMAIN}_parameter_{entry_id}")
        self.values_: dict[str, dict[str, str]] = {}
        self.lock = asyncio.Lock()
        self._programming: dict[str, asyncio.Lock] = {}
        # After the frame the actuator restarts and switches its outputs off (test 2026-10-30): the hub remembers
        # the states of its channels beforehand and restores them afterwards
        self.remember_states: Callable[[str], Any] = lambda ia: None
        # "being programmed" indicator and actuator activity: (ia, active, purpose, errors)
        self.frame_report: Callable[[str, bool, str, str | None], None] = lambda ia, active, purpose, errors: None
        self.restore_states: Callable[[str, Any], Awaitable[None]] | None = None

    def programming(self, ia: str) -> asyncio.Lock:
        """Locked while an actuator is being programmed: in the load state it acknowledges switch commands but does
        not execute them – switch commands to this actuator therefore wait for it."""
        return self._programming.setdefault(ia, asyncio.Lock())

    async def laden(self) -> None:
        self.values_ = (await self._store.async_load()) or {}

    def raw_(self, sn: str, p: Param) -> bytes | None:
        h = self.values_.get(sn, {}).get(p.key)
        return bytes.fromhex(h) if h is not None else None

    def value(self, sn: str, p: Param) -> Any:
        raw = self.raw_(sn, p)
        return p.decode(raw) if raw is not None and len(raw) >= p.length else None

    async def read(self, ia: str, sn: str, params: list[Param]) -> int:
        """Read all parameters of a device; returns the number of parameters read."""
        async with self.lock:
            values_ = self.values_.setdefault(sn, {})
            n, last = 0, None
            open_items = blocks(params)
            for _ in range(3):  # retry failed blocks at the end (weak radio link)
                errors = []
                for blk in open_items:
                    try:
                        n += self._apply(values_, params, blk, await self.bridge.request(
                            "prop_read", ia=ia, obj=blk[0], pid=blk[1], start=blk[2], count=blk[3]))
                    except BridgeError as err:
                        errors.append(blk)
                        last = err
                open_items = errors
                if not open_items:
                    break
            if open_items:
                _LOGGER.warning("%s: %d of %d reads without answer (%s)", ia, len(open_items),
                                len(blocks(params)), last)
            values_["_gelesen"] = datetime.now().isoformat(timespec="seconds")
            await self._store.async_save(self.values_)
        async_dispatcher_send(self.hass, self.signal, sn)
        return n

    @staticmethod
    def _apply(values_: dict, params: list[Param], blk: tuple, r: dict) -> int:
        obj, pid, start, _ = blk
        n = 0
        data = bytes.fromhex(r.get("value", ""))
        for p in params:
            if p.d["obj"] != obj or p.d["pid"] != pid:
                continue
            if pid == PID_PARAM:
                off = p.d["start"] - start
                if 0 <= off and off + p.length <= len(data):
                    values_[p.key] = data[off: off + p.length].hex()
                    n += 1
            elif p.d["start"] == start:
                values_[p.key] = data.hex()
                n += 1
        return n

    def missing(self, sn: str, params: list[Param]) -> list[Param]:
        return [p for p in params if p.key not in self.values_.get(sn, {})]

    async def _pw(self, ia: str, obj: int, pid: int, data: bytes, start: int = 1, count: int = 1) -> None:
        await self.bridge.request("prop_write", ia=ia, obj=obj, pid=pid, start=start, count=count, data=data.hex())

    async def _frame(self, ia: str, device_obj: int, obj: int, steps, parameter: bool, purpose: str) -> None:
        """Programming frame like the eNet Server (load state, timestamp); the caller holds the locks.
        parameter: additionally preparation and checksum of the parameter memory (parameter capture),
        otherwise only the frame as when linking (links capture). purpose: for the actuator's activity."""
        before = self.remember_states(ia)
        self.frame_report(ia, True, purpose, None)
        errors: str | None = None
        try:
            await self._pw(ia, 1, 6, bytes([2]) + bytes(9))  # load state: loading starts
        except BridgeError as err:
            self.frame_report(ia, False, purpose, str(err))
            raise
        try:
            await self._frame_content(ia, device_obj, obj, steps, parameter)
        except BridgeError as err:
            errors = str(err)
            # close the frame anyway: otherwise the actuator stays in loading (afterwards 00 = not loaded) and no longer
            # executes switch commands (indirect lights 2026-10-30 after a nack during link reconciliation)
            with contextlib.suppress(BridgeError):
                await self._pw(ia, 1, 6, bytes([1]) + bytes(9))
            raise
        finally:
            if self.restore_states and before:
                await self.restore_states(ia, before)
            self.frame_report(ia, False, purpose, errors)

    async def _frame_content(self, ia: str, device_obj: int, obj: int, steps, parameter: bool) -> None:
        await self._pw(ia, device_obj, 0x39, bytes.fromhex("000000000003"))
        if parameter:
            await self._pw(ia, device_obj, 0x3A, b"\x00")
            await self._pw(ia, device_obj, 0x41, b"\x01")
            await self._pw(ia, device_obj, 0x42, b"\x07")
        await steps()
        if parameter:
            await self.bridge.request("prop_read", ia=ia, obj=obj, pid=0xD1, start=1, count=1)  # checksum
            await self._pw(ia, obj, 0xD0, b"\x04")
        await self._pw(ia, obj, 0xD2, b"\x04")
        t = datetime.now()
        await self._pw(ia, device_obj, 0x39, bytes([t.year % 100, t.month, t.day, t.hour, t.minute, t.second]))
        for _ in range(3):
            await self.bridge.request("np_write", ia=ia, data="00123c00")
        await self._pw(ia, 1, 6, bytes([1]) + bytes(9))  # load state: loaded
        await asyncio.sleep(1)
        r = await self.bridge.request("prop_read", ia=ia, obj=1, pid=6, start=1, count=1)
        if not r.get("value", "").startswith("01"):
            _LOGGER.warning("%s: load state after programming %s instead of 01", ia, r.get("value"))

    async def program(self, ia: str, device_obj: int, obj: int, steps, purpose: str | None = None) -> None:
        """Run arbitrary steps (e.g. create/delete links) inside the programming frame."""
        async with self.lock, self.programming(ia):
            await self._frame(ia, device_obj, obj, steps, parameter=False,
                               purpose=purpose or tr("zweck_verknuepfungen_kurz"))

    async def write_raw(self, ia: str, device_obj: int, obj: int, start: int, data: bytes,
                            purpose: str | None = None) -> None:
        """Write bytes to the parameter memory (PID 79) and read them back – e.g. scene values SCV1[nr], which have no
        settings entity (write test 2026-10-30, NOTES "Szenenwerte schreiben")."""
        async with self.lock, self.programming(ia):
            async def write_value() -> None:
                await self._pw(ia, obj, PID_PARAM, data, start, len(data))

            await self._frame(ia, device_obj, obj, write_value, parameter=True,
                               purpose=purpose or tr("zweck_parameterspeicher"))
            r = await self.bridge.request("prop_read", ia=ia, obj=obj, pid=PID_PARAM, start=start, count=len(data))
        read_values = bytes.fromhex(r.get("value", ""))[: len(data)]
        if read_values != data:
            raise BridgeError("verify_failed", tr("verify_fehler", name=f"{ia} obj {obj} @{start}", neu=data.hex(), gelesen=read_values.hex()))

    async def write(self, ia: str, sn: str, device_obj: int, p: Param, value: Any) -> None:
        """Write one parameter, same sequence as the eNet Server (load state, checksum, timestamp)."""
        new = p.encode(value)
        obj = p.d["obj"]
        async with self.lock, self.programming(ia):
            if p.d["pid"] != PID_PARAM:  # device/channel property: direct
                await self._pw(ia, obj, p.d["pid"], new, p.d["start"], p.d["anzahl"])
            else:
                async def write_value() -> None:
                    await self._pw(ia, obj, PID_PARAM, new, p.d["start"], len(new))

                await self._frame(ia, device_obj, obj, write_value, parameter=True, purpose=tr("zweck_einstellung", name=p.name))
            # verify: read the value back
            r = await self.bridge.request("prop_read", ia=ia, obj=obj, pid=p.d["pid"], start=p.d["start"],
                                          count=p.d["anzahl"] if p.d["pid"] != PID_PARAM else p.length)
            read_values = bytes.fromhex(r.get("value", ""))[: p.length]
            self.values_.setdefault(sn, {})[p.key] = read_values.hex()
            await self._store.async_save(self.values_)
        async_dispatcher_send(self.hass, self.signal, sn)
        if read_values != new:
            raise BridgeError("verify_failed", tr("verify_fehler", name=p.name, neu=new.hex(), gelesen=read_values.hex()))

"""Program hand and wall transmitters: operating mode per rocker, rocker → actuators, single key → server key.

Replica of the eNet Server (capture umstellung1.jsonl 2026-10-26, NOTES.md "SENDER-PROGRAMMIERUNG KOMPLETT"):
On every change the server rewrites ALL channels of the transmitter.
  Wake:   under the server SN at GA 0 NPWrite PID 72 = transmitter SN + 02 00000000 (every ~100 ms), until the
          transmitter answers after a key press; keep awake with the broadcast PID 72 = 04 05 00 00 00 (every ~5 s)
  Frame:  load state 02, obj 0x10 PID 39 = …03, key objects, channels, timestamp, obj 0x10 PID 48 = 03
  Key objects 2n+1/2n+2 PID 4F[1]: rocker = 0x0B+n / ff, single keys = 00 / 00
  Channel (central 0x0B = channel 9 FT_INAOOS with status LED links `zentral`, single key k = 2+k,
          rocker n = 0x0B+n):
          parameters, read D1, D0 = 04, read D2,
          A_Restart 06 c (reset links), factory entries (01 00 own SN GA → GO), links, D2 = 04
Rocker n: factory GAs 48+3n+1…+3 → GO 1…3, actuator status GA(s) → GO 1 (status LED).
Single key k: PID E2 = key code (assigned by the server; stored in the installation, taken from the device only
          when no code is stored – after a factory reset it holds a factory value), factory GAs 6(k-1)+1…6k
          → GO 1…6, server SN + answer GA → GO 1. The actuator side of a rocker (GA 50+3n → GO 1,
          51+3n → GO 19) is not touched here.
Rocker with HA lamps (ha_ziele): additionally server SN + answer GA → GO 1. The rocker then (also) queries the
          server SN; the bridge answers as for a server key (green LED), HA switches/dims the lamps
          (ha_rocker.py). Which HA lamps they are is stored only in the installation – changing them needs no
          programming.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.logbook import async_log_entry
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import layouts
from .bridge import BridgeError
from .const import DOMAIN
from .pair import SRV_IA, RadioSession, apci, frame, ia_int
from .texts import tr

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

OBJ_CENTRAL, OBJ_DEVICE = 0x0B, 0x10  # standard layout (wall and hand transmitters)
PID_LINKS = 0x3F
CT_CENTRAL, CT_ROCKER = "CT_1F15", "CT_1F11"


@dataclass(frozen=True)
class Layout:
    """Objects, channel numbers and GO offsets of a transmitter. Wall and hand transmitters (WS…, HS…) all follow
    the server's rule: device object 0x10, central channel 9 at obj 0x0B / offset 48, rocker n at channel 2n−1,
    obj 0x0B+n, offset 48+3n; single key k at obj 2+k, offset 6(k−1) (key objects 2n+1/2n+2 switch a rocker to
    single keys). Others differ (universal transmitter US2M: device object 8, central obj 5 / offset 12, rocker
    obj 6 / offset 15; it switches to single keys with a device parameter instead) – taken from the catalogue."""

    device_obj: int = OBJ_DEVICE
    central: tuple[int, int, int] = (9, OBJ_CENTRAL, 48)  # channel no., object, GO offset
    rockers: tuple[tuple[int, int, int], ...] | None = None  # None: rule above for any n

    def rocker(self, n: int) -> tuple[int, int, int]:
        """Channel no., object and GO offset of rocker n (1-based)."""
        if self.rockers is None:
            return 2 * n - 1, OBJ_CENTRAL + n, 48 + 3 * n
        return self.rockers[n - 1]

    @property
    def single_keys(self) -> bool:
        """Rockers can be switched to single keys via the key objects (standard layout only)."""
        return self.device_obj == OBJ_DEVICE and self.central == (9, OBJ_CENTRAL, 48) and (
            self.rockers is None or all(r == (2 * n - 1, OBJ_CENTRAL + n, 48 + 3 * n)
                                        for n, r in enumerate(self.rockers, 1)))


def layout(hub: Hub | None, enet_device: dict[str, Any]) -> Layout:
    """Layout of a transmitter from its catalogue definition (central channel CT_1F15, rockers CT_1F11 in channel
    order); without a definition the standard layout."""
    d = (hub.definitions if hub is not None else {}).get(enet_device.get("typ", ""))
    channels_ = sorted((d or {}).get("kanaele", []), key=lambda k: k["nr"])
    central = next((k for k in channels_ if k["kanaltyp"] == CT_CENTRAL), None)
    rockers = tuple((k["nr"], k["obj"], k["go_offset"]) for k in channels_ if k["kanaltyp"] == CT_ROCKER)
    if central is None or not rockers:
        return Layout()
    return Layout(d.get("geraet_obj") or OBJ_DEVICE, (central["nr"], central["obj"], central["go_offset"]), rockers)
WAKE_S = 60
KEEP_AWAKE_S = 5.0


def assignment(enet_device: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Rocker n → {"art": "wippe", "ziele": [{ia, ch}], "ha_ziele": [entity_id], "antwort_ga"} or
    {"art": "tasten", "tasten": {k: {antwort_ga}}}."""
    b: dict[int, dict[str, Any]] = {}
    for t in enet_device.get("tasten", []):
        n = (t["nr"] + 1) // 2
        if t["art"] == "wippe":
            if "ziele" not in t:  # unknown ≠ empty: otherwise existing links would be deleted
                raise BridgeError("bad_args", tr("wippe_ziele_unbekannt", name=enet_device["name"], n=n))
            b[n] = {"art": "wippe", "ziele": t["ziele"], "ha_ziele": list(t.get("ha_ziele", [])),
                    "antwort_ga": t.get("antwort_ga")}
        else:
            b.setdefault(n, {"art": "tasten", "tasten": {}})["tasten"][t["nr"]] = {
                "antwort_ga": t.get("antwort_ga"), "code": t.get("code")}
    return b


class Sender:
    def __init__(self, hub: Hub, enet_device: dict[str, Any], radio_session: RadioSession) -> None:
        self.hub, self.g, self.radio_session = hub, enet_device, radio_session
        self.ia = enet_device["ia"]
        self.sn = enet_device["sn"]
        self.base = enet_device.get("ga_basis", 0) * 256
        self.dom = hub.installation["server"]["domain"]
        self.srv = hub.installation["server"]["sn"]
        self.lay = layout(hub, enet_device)

    # --- Building blocks ----------------------------------------------------------
    async def _request(self, art: str, description: str, **kw: Any) -> dict:
        """Bridge request to the transmitter, with duration in the debug log (troubleshooting: programming is slow)."""
        t0 = time.monotonic()
        try:
            r = await self.hub.bridge.request(art, ia=self.ia, **kw)
        except BridgeError as err:
            _LOGGER.debug("%s %s: failed after %.1f s (%s)", self.g["name"], description, time.monotonic() - t0, err)
            raise
        _LOGGER.debug("%s %s: %.1f s, %s tries", self.g["name"], description, time.monotonic() - t0, r.get("tries"))
        return r

    async def pr(self, obj: int, pid: int) -> bytes:
        r = await self._request("prop_read", f"read obj {obj} pid {pid:#x}", obj=obj, pid=pid, start=1, count=1)
        return bytes.fromhex(r.get("value", ""))

    async def pw(self, obj: int, pid: int, data: bytes, start: int = 1, count: int = 1) -> None:
        """count = number of elements (load state 10 bytes and timestamp 6 bytes are one element each)."""
        await self._request("prop_write", f"write obj {obj} pid {pid:#x}", obj=obj, pid=pid, start=start,
                            count=count, data=data.hex())

    async def link(self, obj: int, op: int, sn: str, ga: int, go: int) -> None:
        data = bytes([op, 0]) + bytes.fromhex(sn) + ga.to_bytes(2, "big") + go.to_bytes(2, "big")
        await self._request("fp_cmd", f"link obj {obj} op {op} {sn} ga {ga:#x} go {go}", obj=obj, pid=PID_LINKS,
                            data=data.hex())

    async def restart(self, channel: int) -> None:
        """A_Restart 06 c: reset the channel's links (answer 0x3A1)."""
        ia = ia_int(self.ia)
        r = await self.radio_session.request(frame(self.dom, True, False, SRV_IA, ia, 0x381, bytes([6, channel])),
                                    lambda d: apci(d) == 0x3A1 and (d[11] << 8 | d[12]) == ia, timeout=1.0, attempts=5)
        if r is None:
            raise BridgeError("timeout", tr("links_reset_unbestaetigt", kanal=channel))
        await asyncio.sleep(0.3)  # the server waits ~1 s here (first link afterwards often unanswered)

    async def channel(self, obj: int, param: tuple[int, bytes], channel: int, factory: list[tuple[int, int]],
                    links: list[tuple[str, int]], cd: bool = True) -> None:
        """Rewrite a channel: parameters, check mark, reset links, factory entries, links.
        param = (start, values) in PID 4F (one element per byte); cd: PID CD = 00 (not for the central channel)."""
        if cd:
            await self.pw(obj, 0xCD, b"\x00")
        await self.pw(obj, 0x4F, param[1], start=param[0], count=len(param[1]))
        await self.pr(obj, 0xD1)
        await self.pw(obj, 0xD0, b"\x04")
        await self.pr(obj, 0xD2)
        await self.restart(channel)
        for ga, go in factory:
            await self.link(obj, 1, self.sn, ga, go)
        for sn, ga in links:
            await self.link(obj, 0, sn, ga, 1)
        await self.pw(obj, 0xD2, b"\x04")

    # --- Procedure -------------------------------------------------------------
    async def wake(self) -> None:
        f = frame(self.srv, False, True, SRV_IA, 0, 0x3E4, bytes.fromhex("c35148") + bytes.fromhex(self.sn)
                  + bytes.fromhex("0200000000"))
        snb = bytes.fromhex(self.sn)
        t0 = time.monotonic()
        end = t0 + WAKE_S
        attempts = 0
        while time.monotonic() < end:
            await self.radio_session.send_frame(f)
            attempts += 1
            if await self.radio_session.wait(lambda d: apci(d) == 0x3DB and d[4:10] == snb, 0.1) is not None:
                _LOGGER.debug("%s awake after %.1f s (%d wake frames)", self.g["name"], time.monotonic() - t0, attempts)
                return
        raise BridgeError("timeout", tr("nicht_aufgewacht", name=self.g["name"]))

    async def keep_awake(self) -> None:
        f = frame(self.dom, True, True, SRV_IA, 0, 0x3E4, bytes.fromhex("c351480405000000"))
        while True:
            await asyncio.sleep(KEEP_AWAKE_S)
            _LOGGER.debug("%s keep awake", self.g["name"])
            with contextlib.suppress(BridgeError):
                await self.radio_session.send_frame(f)

    def status_links(self, targets: list[dict[str, Any]]) -> list[tuple[str, int]]:
        """Actuator SN + status GA (base·256 + channel offset + SSGI, 22 for CT_1F01) per target – the rocker's status LED."""
        out = []
        for z in targets:
            a = next((g for g in self.hub.installation["geraete"] if g["ia"] == z["ia"]), None)
            k = next((k for k in (a or {}).get("kanaele", []) if k["nr"] == z["ch"]), None)
            if a is None or k is None:
                raise BridgeError("bad_args", tr("ziel_fehlt", ia=z["ia"], ch=z["ch"]))
            out.append((a["sn"], a.get("ga_basis", 0) * 256 + k["offset"] + (layouts.go(k, "SSGI") or 22)))
        return out

    async def read_base(self) -> int:
        """GA base from the factory entry GO 1 of the central channel (increments on every factory reset). Links
        (type 00) come before the factory entry (type 01, own SN) in the table – so search up to it."""
        for idx in range(16):
            r = await self.hub.bridge.request("fp_read", ia=self.ia, obj=self.lay.central[1], pid=PID_LINKS,
                                              data=f"000001{idx:02x}")
            v = bytes.fromhex(r.get("value", ""))
            if len(v) >= 9 and v[0] == 0x01 and v[1:7].hex() == self.sn:
                return v[7]
        raise BridgeError("bad_reply", tr("werkseintrag_fehlt"))

    async def write(self, assigned: dict[int, dict[str, Any]]) -> None:
        rockers = sorted(assigned)  # only the assigned rockers; the others stay as they are
        lay, dev = self.lay, self.lay.device_obj
        if not lay.single_keys and any(assigned[n]["art"] != "wippe" for n in rockers):
            raise BridgeError("bad_args", tr("keine_einzeltasten", name=self.g["name"]))
        self.base = await self.read_base() * 256
        await self.pr(dev, 0x39)
        if lay.single_keys:
            for n in rockers:
                await self.pr(2 * n + 1, 0xCF)
        await self.pr(1, 6)
        await self.pw(1, 6, b"\x02" + bytes(9))  # load state: loading starts
        await self.pr(1, 6)
        await self.pr(dev, 0x39)
        await self.pw(dev, 0x39, bytes.fromhex("000000000003"))
        if lay.single_keys:
            for n in rockers:  # key objects: rocker or single keys
                w = assigned[n]["art"] == "wippe"
                await self.pw(2 * n + 1, 0x4F, bytes([lay.rocker(n)[1] if w else 0]))
                await self.pw(2 * n + 2, 0x4F, bytes([0xFF if w else 0]))
        # central channel (channel 9, CT_1F15, FT_INAOOS): only status LED links to actuators (installation `zentral`)
        if "zentral" not in self.g:  # unknown ≠ empty: otherwise existing links would be deleted
            raise BridgeError("bad_args", tr("zentral_unbekannt", name=self.g["name"]))
        c_nr, c_obj, c_off = lay.central
        await self.channel(c_obj, (2, b"\x01\x01"), c_nr, [(self.base + c_off + 1 + i, 1 + i) for i in range(3)],
                         self.status_links(self.g["zentral"]), cd=False)
        await self.pw(dev, 0x41, b"\x01")
        for n in rockers:
            b = assigned[n]
            if b["art"] == "tasten":
                for k in (2 * n - 1, 2 * n):
                    obj = 2 + k
                    key_obj = b["tasten"].setdefault(k, {})
                    if key_obj.get("code") is None:
                        # the server assigns the key code (PID E2); a stored one wins, otherwise take the one in
                        # the device – after a factory reset it holds a factory value (key 2 then sent a scene)
                        key_obj["code"] = ((await self.pr(obj, 0xE2))[:1] or b"\x00")[0]
                    await self.pw(obj, 0xE2, bytes([key_obj["code"]]))
                    answer = key_obj.get("antwort_ga")
                    if answer is None:
                        raise BridgeError("bad_args", tr("keine_antwort_ga", k=k))
                    await self.channel(obj, (4, b"\x01\x01"), k,
                                     [(self.base + 6 * (k - 1) + 1 + i, 1 + i) for i in range(6)],
                                     [(self.srv, answer)])
            else:
                links = self.status_links(b["ziele"])
                if b.get("ha_ziele"):  # HA lamps: the bridge (server SN) answers the status query
                    if b.get("antwort_ga") is None:
                        raise BridgeError("bad_args", tr("keine_antwort_ga", k=2 * n - 1))
                    links.append((self.srv, b["antwort_ga"]))
                r_nr, r_obj, r_off = lay.rocker(n)
                await self.channel(r_obj, (1, b"\x01\x01"), r_nr,
                                 [(self.base + r_off + 1 + i, 1 + i) for i in range(3)], links)
        t = datetime.now()
        await self.pw(dev, 0x39, bytes([t.year % 100, t.month, t.day, t.hour, t.minute, t.second]))
        lade = await self.pr(1, 6)
        if lade[:1] != b"\x01":
            _LOGGER.warning("%s: load state after programming %s instead of 01", self.g["name"], lade.hex())


def keys_from(enet_device: dict[str, Any], assigned: dict[int, dict[str, Any]], lay: Layout | None = None
              ) -> list[dict[str, Any]]:
    """Assignment → installation `tasten` entries (offsets/objects of the transmitter's layout)."""
    lay = lay or Layout()
    out = []
    for n in sorted(assigned):
        b = assigned[n]
        if b["art"] == "wippe":
            _, obj, offset = lay.rocker(n)
            e = {"nr": 2 * n - 1, "art": "wippe", "offset": offset, "obj": obj, "ziele": b["ziele"]}
            if b.get("ha_ziele"):
                e["ha_ziele"], e["antwort_ga"] = list(b["ha_ziele"]), b.get("antwort_ga")
            out.append(e)
        else:
            for k in (2 * n - 1, 2 * n):
                t = b["tasten"].get(k, {})
                out.append({"nr": k, "art": "taste", "offset": 6 * (k - 1), "obj": 2 + k,
                            "antwort_ga": t.get("antwort_ga"), "code": t.get("code")})
    return out


def default_assignment(enet_device: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """New transmitter: rocker 1 (keys 1/2) as a rocker without targets – fits every transmitter (including wall
    transmitters with only one rocker); the other rockers stay in factory state until they are assigned."""
    return {1: {"art": "wippe", "ziele": []}}


async def _actuator_links(hub: Hub, enet_device: dict[str, Any], old: dict, new: dict, base: int,
                       rockers: set[int] | None = None, only_ia: str | None = None) -> None:
    """Sync the actuator side of the rockers – based on the link table in the actuator, not on the installation
    (after a factory reset/import it does not reliably know the GA stored in the actuator). Target per channel:
    key value GA (base·256 + rocker offset + 2, 50+3n) → GO 1, status query (+1) → GO 19 (CT_1F01; per channel type). All entries of this transmitter with a
    different GA are deleted.
    rockers: only check the actuators of these rockers (old and new); None = all. Reading the link tables costs up
    to 3 s per entry (slow actuators answer only on the 5th attempt) – do not touch unchanged rockers.
    only_ia: only this device (actuator re-paired after a factory reset)."""
    lay = layout(hub, enet_device)
    wanted: dict[tuple[str, int], set[tuple[int, int]]] = {}
    verify: set[tuple[str, int]] = set()  # actuators on changed rockers (old or new)
    for assigned, with_ in ((old, False), (new, True)):
        for n, b in assigned.items():
            if b["art"] != "wippe":
                continue
            for z in b["ziele"]:
                # target always from ALL rockers: an actuator can be on several rockers of this transmitter,
                # otherwise the links of the unchanged rocker would be deleted (happened 2026-11-08, 0.12.9)
                s = wanted.setdefault((z["ia"], z["ch"]), set())
                if with_:
                    off = lay.rocker(n)[2]  # rocker CT_1F11: key value GBR GO 2, status query SSGET GO 3
                    s |= {(base + off + (layouts.go_of_type(CT_ROCKER, "GBR") or 2), "GBR"),
                          (base + off + (layouts.go_of_type(CT_ROCKER, "SSGET") or 3), "SSGET")}
                if rockers is None or n in rockers:
                    verify.add((z["ia"], z["ch"]))
    for (ia, ch), setpoint in sorted(wanted.items()):
        if (ia, ch) not in verify or (only_ia is not None and ia != only_ia):
            continue
        k = next((k for k in hub.channels_ if k.ia == ia and k.nr == ch), None)
        if k is None:
            _LOGGER.warning("%s: target %s/%s is not part of the installation", enet_device["name"], ia, ch)
            continue
        # GO numbers of the channel's type (CT_1F01: value GBR 1, sum status SSGET 19)
        gos = {"GBR": k.go("GBR", 1), "SSGET": k.go("SSGET", 19)}
        setpoint = {(ga, gos[f]) for ga, f in setpoint}
        is_: set[tuple[int, int]] = set()
        for go in gos.values():
            for idx in range(32):
                v = await _read_link(hub, k, go, idx)
                if v is None:
                    break  # end of table
                if len(v) >= 11 and v[1:7].hex() == enet_device["sn"]:
                    is_.add((int.from_bytes(v[7:9], "big"), int.from_bytes(v[9:11], "big")))
        gone, extra = is_ - setpoint, setpoint - is_
        if not gone and not extra:
            continue

        async def steps(k=k, gone=gone, extra=extra) -> None:
            for (ga, go), op in [(x, 2) for x in sorted(gone)] + [(x, 0) for x in sorted(extra)]:
                data = bytes([op, 0]) + bytes.fromhex(enet_device["sn"]) + ga.to_bytes(2, "big") + go.to_bytes(2, "big")
                await hub.bridge.request("fp_cmd", ia=k.ia, obj=k.obj, pid=PID_LINKS, data=data.hex())

        _LOGGER.info("%s: actuator %s delete links %s, create %s", enet_device["name"], k.key, sorted(gone), sorted(extra))
        await hub.params.program(k.ia, hub.device_obj(k.enet_device), k.obj, steps,
                                       purpose=tr("zweck_verknuepfungen", name=enet_device["name"]))


async def restore_rocker_links(hub: Hub, ia: str) -> list[str]:
    """After a factory reset + re-pairing of an actuator: write the rocker links of all transmitters pointing to it
    back into the actuator (actuator side only, no waking of transmitters). Skip transmitters whose GA base has
    not been moved yet – "Program transmitter" sets their links. Returns error texts."""
    errors = []
    for g in hub.installation["geraete"]:
        if g.get("rolle") != "sender" or not g.get("ga_basis_geprueft", True):
            continue
        try:
            assigned = assignment(g)
        except BridgeError:
            continue
        if not any(b["art"] == "wippe" and any(z["ia"] == ia for z in b["ziele"]) for b in assigned.values()):
            continue
        try:
            await _actuator_links(hub, g, assigned, assigned, g.get("ga_basis", 0) * 256, only_ia=ia)
        except BridgeError as err:
            errors.append(f"{g['name']}: {err}")
    return errors


def changed_rockers(old: dict[int, dict[str, Any]], new: dict[int, dict[str, Any]]) -> set[int]:
    """Rockers whose operating mode or eNet lamps change (HA lamps do not affect the actuators)."""
    def ident(b: dict[str, Any] | None):
        if b is None:
            return None
        return b["art"], frozenset((z["ia"], z["ch"]) for z in b.get("ziele") or [])
    return {n for n in set(old) | set(new) if ident(old.get(n)) != ident(new.get(n))}


async def _read_link(hub: Hub, k, go: int, idx: int, attempts: int = 3) -> bytes | None:
    """Entry idx of the link table of GO go; None = end of table (nack with rc 255). Other errors (radio) are
    retried – an incompletely read table would create existing links again (nack)."""
    for n in range(attempts):
        t0 = time.monotonic()
        try:
            r = await hub.bridge.request("fp_read", ia=k.ia, obj=k.obj, pid=PID_LINKS, data=f"0000{go:02x}{idx:02x}")
            _LOGGER.debug("%s link GO %d #%d: %.1f s, %s tries", k.key, go, idx, time.monotonic() - t0, r.get("tries"))
            return bytes.fromhex(r.get("value", ""))
        except BridgeError as err:
            _LOGGER.debug("%s link GO %d #%d: %s after %.1f s", k.key, go, idx, err, time.monotonic() - t0)
            if err.err == "nack" and err.rc == 255:
                return None
            if n + 1 == attempts:
                raise BridgeError(err.err, tr("linktabelle_unlesbar", key=k.key, err=err)) from err
    return None


def _server_key_base(hub: Hub, enet_device: dict[str, Any], base: int) -> bool:
    """Move the GAs of this transmitter's server keys in the ESP configuration to the new base."""
    changed = False
    for t in hub.installation.get("esp", {}).get("server_tasten", []):
        if t["sender"] == enet_device["sn"] and t["ga"] >> 8 != base >> 8:
            t["ga"] = base + (t["ga"] & 0xFF)
            changed = True
    return changed


def activity(hub: Hub, enet_device: dict[str, Any], text: str) -> None:
    """Entry in the transmitter's activity (logbook, at the "Program transmitter" button) – on the main page and
    on every rocker page, since programming is often started from there."""
    reg = er.async_get(hub.hass)
    for e in er.async_entries_for_config_entry(reg, hub.entry_id):
        if e.domain == "button" and e.unique_id.startswith(f"{enet_device['sn']}_") \
                and e.unique_id.endswith("_sender_programmieren"):
            async_log_entry(hub.hass, enet_device["name"], text, DOMAIN, e.entity_id)


async def program(hub: Hub, enet_device: dict[str, Any], assigned: dict[int, dict[str, Any]] | None = None,
                        after_pairing: bool = False, all_actuators: bool = True) -> None:
    """Wake the transmitter (user presses a key), reprogram it completely, then adapt the rockers' actuator side
    and the bridge's server keys to the assignment and GA base. Only one run at a time per transmitter."""
    if enet_device["sn"] in hub.senders_active:
        raise BridgeError("laeuft", tr("programmierung_laeuft"))
    hub.senders_active.add(enet_device["sn"])
    async_dispatcher_send(hub.hass, hub.signal(f"sender_aktiv_{enet_device['sn']}"))  # lock buttons
    try:
        await _program(hub, enet_device, assigned, after_pairing, all_actuators)
    except BridgeError as err:
        activity(hub, enet_device, tr("akt_programmierung_fehlgeschlagen", err=err))
        raise
    finally:
        hub.senders_active.discard(enet_device["sn"])
        async_dispatcher_send(hub.hass, hub.signal(f"sender_aktiv_{enet_device['sn']}"))


async def _program(hub: Hub, enet_device: dict[str, Any], assigned: dict[int, dict[str, Any]] | None,
                         after_pairing: bool, all_actuators: bool = True) -> None:
    old = assignment(enet_device)
    old_base = enet_device.get("ga_basis") if enet_device.get("ga_basis_geprueft") else None
    assigned = assigned or old
    activity(hub, enet_device, tr("akt_eingelernt_taste") if after_pairing else tr("akt_sender_wecken"))
    async with hub.params.lock, RadioSession(hub) as radio_session:
        s = Sender(hub, enet_device, radio_session)
        await s.wake()
        activity(hub, enet_device, tr("akt_sender_aktiv"))
        hold = hub.hass.async_create_background_task(s.keep_awake(), "enet Sender wach halten")
        try:
            t0 = time.monotonic()
            await s.write(assigned)
            _LOGGER.debug("%s: transmitter written in %.1f s", enet_device["name"], time.monotonic() - t0)
        finally:
            hold.cancel()
            with contextlib.suppress(BridgeError):
                await s.pw(s.lay.device_obj, 0x48, b"\x03")  # release transmitter
    enet_device["tasten"] = keys_from(enet_device, assigned, s.lay)
    enet_device["ga_basis"], enet_device["ga_basis_geprueft"] = s.base >> 8, True
    new_keys = _server_key_base(hub, enet_device, s.base)
    await hub.save_installation()
    if new_keys:
        await hub._sync_config(hub.bridge.info)
    try:  # the transmitter is done; an error here only affects the actuator side of the rockers
        t0 = time.monotonic()
        # all actuators only with a new GA base (factory reset) or on request (button without change = repair)
        rockers = None if all_actuators or old_base != s.base >> 8 else changed_rockers(old, assigned)
        _LOGGER.debug("%s: actuator links for rockers %s", enet_device["name"], "all" if rockers is None else sorted(rockers))
        await _actuator_links(hub, enet_device, old, assigned, s.base, rockers)
        _LOGGER.debug("%s: actuator links in %.1f s", enet_device["name"], time.monotonic() - t0)
    except BridgeError as err:
        _LOGGER.warning("%s: actuator links not fully updated: %s", enet_device["name"], err)
        activity(hub, enet_device, tr("akt_links_unvollstaendig", err=err))
        return
    activity(hub, enet_device, tr("akt_programmierung_beendet"))


# --- Change assignment (dialog "Assign transmitter") ------------------------------
REPLY_GA_FROM = 1000  # answer GAs of new single keys (under server SN), above those assigned by the server/HA
REPORT = "melden"


def rocker_count(hub: Hub, enet_device: dict[str, Any]) -> int:
    """Rockers of the transmitter type = rocker channels (CT_1F11) in the catalogue; otherwise from the assignment
    (at least 1)."""
    d = hub.definitions.get(enet_device.get("typ", ""))
    if d:
        n = sum(1 for k in d["kanaele"] if k["kanaltyp"] == "CT_1F11")
        if n:
            return n
    return max([(t["nr"] + 1) // 2 for t in enet_device.get("tasten", [])] + [1])


def key_code(k: int) -> int:
    """PID E2 following the server's scheme: key 1 → 0, key 2 → 15, key k ≥ 3 → k − 2 (key 7/8 → 5/6)."""
    return {1: 0, 2: 15}.get(k, k - 2)


def key_ga(enet_device: dict[str, Any], k: int) -> int:
    """GA to which single key k sends its sum status (server key): base·256 + offset 6(k−1) + 6."""
    return enet_device.get("ga_basis", 0) * 256 + 6 * (k - 1) + 6


def _scene_call(actions: list[dict[str, Any]]) -> tuple[int, int] | None:
    """Bridge scene that only recalls an eNet scene → (GA, no.)."""
    if len(actions) == 1 and actions[0]["ia"] == "0.0.0":
        return actions[0]["handle"], int(actions[0]["wert"], 16)
    return None


def action_options(hub: Hub) -> list[tuple[str, str]]:
    """Selectable actions of a single key: (value, label)."""
    out = [(REPORT, tr("aktion_melden"))]
    out += [(f"ha:{ha_id}", tr("aktion_szene", name=name)) for ha_id, name in hub.ha_scenes.names_().items()]
    out += [(f"enet:{s.id}", tr("aktion_enet_szene", name=s.name)) for s in hub.scenes.scenes if s.art != "ha"]
    out += [(f"esp:{n}", tr("aktion_bridge_szene", name=n)) for n, a in hub.installation.get("esp", {}).get("szenen", {}).items()
            if _scene_call(a) is None]
    return out


def key_action(hub: Hub, enet_device: dict[str, Any], k: int) -> str:
    esp = hub.installation.get("esp", {})
    t = next((t for t in esp.get("server_tasten", []) if t["sender"] == enet_device["sn"]
              and t["ga"] & 0xFF == key_ga(enet_device, k) & 0xFF and (t.get("szene") or t.get("ha"))), None)
    if t is not None and t.get("ha"):
        return f"ha:{t['ha']}"
    if t is None or t["szene"] not in esp.get("szenen", {}):
        return REPORT
    call = _scene_call(esp["szenen"][t["szene"]])
    s = next((s for s in hub.scenes.scenes if (s.ga, s.nr) == call), None) if call else None
    if s and s.ha_id:  # eNet scene that is now the eNet part of an HA scene (converted import)
        return f"ha:{s.ha_id}"
    return f"enet:{s.id}" if s else f"esp:{t['szene']}"


def needs_programming(enet_device: dict[str, Any], assigned: dict[int, dict[str, Any]]) -> bool:
    """Only the operating mode, the rockers' lamps and new single keys change the transmitter; a single key's
    action is stored in the bridge."""
    old = assignment(enet_device)
    for n, b in assigned.items():
        a = old.get(n)
        if a is None or a["art"] != b["art"]:
            return True
        if b["art"] == "wippe" and sorted((z["ia"], z["ch"]) for z in a["ziele"]) != \
                sorted((z["ia"], z["ch"]) for z in b["ziele"]):
            return True
        if b["art"] == "wippe" and bool(a.get("ha_ziele")) != bool(b.get("ha_ziele")):
            return True  # the bridge's answer (server SN → GO 1) is added or removed
        if b["art"] == "tasten" and any(t.get("antwort_ga") is None for t in a["tasten"].values()):
            return True
    return False


def prepare_new_keys(hub: Hub, assigned: dict[int, dict[str, Any]]) -> None:
    """Single keys without answer GA/code (previously rocker or free): assign them like the server does."""
    used = {s.ga for s in hub.scenes.scenes} | {s.ga_status for s in hub.scenes.scenes if s.ga_status}
    used |= {t["antwort_ga"] for g in hub.installation["geraete"] for t in g.get("tasten", []) if t.get("antwort_ga")}
    used |= {t["antwort_ga"] for b in assigned.values() if b["art"] == "tasten"
               for t in b["tasten"].values() if t.get("antwort_ga")}
    used |= {b["antwort_ga"] for b in assigned.values() if b["art"] == "wippe" and b.get("antwort_ga")}
    ga = REPLY_GA_FROM
    for n in sorted(assigned):
        b = assigned[n]
        if b["art"] == "wippe" and b.get("ha_ziele") and b.get("antwort_ga") is None:
            while ga in used:
                ga += 1
            b["antwort_ga"] = ga
            used.add(ga)
        if b["art"] != "tasten":
            continue
        for k in (2 * n - 1, 2 * n):
            t = assigned[n]["tasten"].setdefault(k, {})
            if t.get("antwort_ga") is None:
                while ga in used:
                    ga += 1
                t["antwort_ga"], t["code"] = ga, key_code(k)
                used.add(ga)


def set_server_keys(hub: Hub, enet_device: dict[str, Any], actions: dict[int, str]) -> None:
    """Reset this transmitter's server keys in the bridge configuration (key k → action); eNet scenes are created
    as a bridge scene with one scene recall. Recall scenes no longer used are dropped."""
    esp = hub.installation.setdefault("esp", {})
    scenes = esp.setdefault("szenen", {})
    keys_ = [t for t in esp.get("server_tasten", []) if t["sender"] != enet_device["sn"]]
    for k, value in sorted(actions.items()):
        key_obj = {"name": tr("taste_name", name=enet_device["name"], k=k), "sender": enet_device["sn"], "ga": key_ga(enet_device, k)}
        s = None
        if value.startswith("ha:"):  # HA scene: the bridge recalls the eNet part itself, HA switches the rest
            key_obj["ha"] = value[3:]
            s = hub.scenes.ha_scene(value[3:])
            if s is not None and (not s.values_ or s.ha_id in hub.ha_scenes.in_progress):
                s = None  # no (finished) eNet part: only report the key, HA switches the scene
        elif value.startswith("enet:"):
            s = hub.scenes.get(value[5:])
            if s is None:
                continue
        if s is not None:
            name = next((n for n, a in scenes.items() if _scene_call(a) == (s.ga, s.nr)), None)
            if name is None:
                name = s.name
                while name in scenes:
                    name += "'"
                scenes[name] = [{"ia": "0.0.0", "handle": s.ga, "wert": f"{s.nr:02x}"}]
            key_obj["szene"] = name
        elif value.startswith("esp:") and value[4:] in scenes:
            key_obj["szene"] = value[4:]
        elif "ha" not in key_obj:
            continue  # report only: the bridge answers every server key, HA gets the event
        keys_.append(key_obj)
    esp["server_tasten"] = keys_
    used_ = {t.get("szene") for t in keys_}
    for n in [n for n, a in scenes.items() if n not in used_ and _scene_call(a)]:
        del scenes[n]


def apply_ha_targets(enet_device: dict[str, Any], assigned: dict[int, dict[str, Any]]) -> None:
    """Take the rockers' HA lamps into the installation without programming the transmitter (needs_programming:
    no – the rocker already had HA lamps, i.e. the bridge's answer)."""
    for t in enet_device.get("tasten", []):
        b = assigned.get((t["nr"] + 1) // 2)
        if t["art"] == "wippe" and b and b["art"] == "wippe" and b.get("ha_ziele") and t.get("ha_ziele"):
            t["ha_ziele"] = list(b["ha_ziele"])


async def change_assignment(hub: Hub, enet_device: dict[str, Any], assigned: dict[int, dict[str, Any]],
                           actions: dict[int, str]) -> bool:
    """Apply a new assignment: program the transmitter (if needed, the user has to press a key), then the server
    keys in the bridge. Returns whether programming took place."""
    programmed = needs_programming(enet_device, assigned)
    if programmed:
        prepare_new_keys(hub, assigned)
        await program(hub, enet_device, assigned, all_actuators=False)
        clear_pending(enet_device)
    else:
        apply_ha_targets(enet_device, assigned)
    set_server_keys(hub, enet_device, actions)
    await hub.save_installation()
    await hub._sync_config(hub.bridge.info)
    return programmed


# --- Device page: queue operating mode and lamps, action immediately, button "Program transmitter" ---
OPERATING_MODES = ("wippe", "tasten")  # rocker without lamps = unused (the same in the transmitter)
LAMP_SIDE = 2  # more lamps per rocker only in the "Assign transmitter" dialog; then locked on the device page
PENDING = ("geplant", "geplant_lampen")


def pending(enet_device: dict[str, Any]) -> bool:
    """Are there changes that only take effect with "Program transmitter"?"""
    return any(enet_device.get(k) for k in PENDING)


def clear_pending(enet_device: dict[str, Any]) -> None:
    for k in PENDING:
        enet_device.pop(k, None)


def modes(hub: Hub, enet_device: dict[str, Any]) -> list[str]:
    """Selectable operating modes of the transmitter's rockers (single keys only with the standard layout)."""
    return list(OPERATING_MODES) if layout(hub, enet_device).single_keys else ["wippe"]


def mode(enet_device: dict[str, Any], n: int) -> str:
    """Queued, otherwise programmed operating mode of rocker n (unassigned = rocker without lamps)."""
    if str(n) in enet_device.get("geplant", {}):
        art = enet_device["geplant"][str(n)]
        return art if art in OPERATING_MODES else "wippe"  # previously queued "frei" (free)
    a = assignment(enet_device).get(n)
    return "tasten" if a is not None and a["art"] == "tasten" else "wippe"


def planned_assignment(enet_device: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Assignment with the queued operating modes (lamps of a rocker that stays a rocker are kept)."""
    old = assignment(enet_device)
    assigned = dict(old)
    for nr, art in enet_device.get("geplant", {}).items():
        n, a = int(nr), old.get(int(nr))
        if art == "tasten":
            assigned[n] = a if a and a["art"] == "tasten" else {"art": "tasten", "tasten": {}}
        elif a is not None:  # rocker: lamps stay; from single keys → rocker without lamps
            assigned[n] = a if a["art"] == "wippe" else {"art": "wippe", "ziele": []}
    for nr, keys in enet_device.get("geplant_lampen", {}).items():
        n = int(nr)
        if n not in assigned or assigned[n]["art"] == "wippe":
            targets, ha = unlink_lamps(keys)
            before = assigned.get(n) or {}
            assigned[n] = {"art": "wippe", "ziele": targets, "ha_ziele": ha, "antwort_ga": before.get("antwort_ga")}
    return assigned


def is_ha_lamp(key: str) -> bool:
    """Lamp key: eNet channel "ia/ch", HA entity "light.x" / "switch.x"."""
    return "/" not in key


def unlink_lamps(keys: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Lamp keys → (eNet targets [{ia, ch}], HA entities)."""
    targets = [{"ia": k.split("/")[0], "ch": int(k.split("/")[1])} for k in keys if not is_ha_lamp(k)]
    return targets, [k for k in keys if is_ha_lamp(k)]


def rocker_lamps(enet_device: dict[str, Any], n: int) -> list[str]:
    """Queued, otherwise programmed lamps of rocker n: eNet channels "ia/ch", then HA entities."""
    b = planned_assignment(enet_device).get(n)
    if not b or b["art"] != "wippe":
        return []
    return [f"{z['ia']}/{z['ch']}" for z in b["ziele"]] + list(b.get("ha_ziele", []))


def lamp_choices(enet_device: dict[str, Any], n: int) -> int:
    """Number of "Lamp i" selects of rocker n: lamp 2 only once lamp 1 is assigned."""
    if mode(enet_device, n) != "wippe":
        return 0
    return min(len(rocker_lamps(enet_device, n)) + 1, LAMP_SIDE)


def complex_(enet_device: dict[str, Any], n: int) -> bool:
    """Rocker with more lamps than the device page shows: change only in the "Assign transmitter" dialog."""
    return len(rocker_lamps(enet_device, n)) > LAMP_SIDE


def queue_lamps(enet_device: dict[str, Any], n: int, keys: list[str]) -> None:
    """Queue the lamps of rocker n (equal to the programmed state: queue entry removed). If only the HA lamps
    change and the rocker stays with or without HA lamps, it takes effect immediately – the transmitter stays the
    same."""
    keys = list(dict.fromkeys(keys))
    planned = enet_device.setdefault("geplant_lampen", {})
    planned.pop(str(n), None)
    targets, ha = unlink_lamps(keys)
    entry_ = next((t for t in enet_device.get("tasten", []) if t["art"] == "wippe" and (t["nr"] + 1) // 2 == n), None)
    if entry_ is not None and str(n) not in enet_device.get("geplant", {}) and bool(ha) == bool(entry_.get("ha_ziele")) \
            and sorted((z["ia"], z["ch"]) for z in targets) == sorted((z["ia"], z["ch"]) for z in entry_["ziele"]):
        if ha:
            entry_["ha_ziele"] = ha
    elif sorted(rocker_lamps(enet_device, n)) != sorted(keys):
        planned[str(n)] = keys
    if not planned:
        enet_device.pop("geplant_lampen")


def sender_actions(hub: Hub, enet_device: dict[str, Any]) -> dict[int, str]:
    """Action per single key (including queued ones) of this transmitter."""
    return {k: key_action(hub, enet_device, k) for n, b in planned_assignment(enet_device).items()
            if b["art"] == "tasten" for k in (2 * n - 1, 2 * n)}


async def set_action(hub: Hub, enet_device: dict[str, Any], k: int, value: str) -> None:
    """Change a single key's action – only the bridge configuration, the transmitter stays unchanged."""
    actions = sender_actions(hub, enet_device)
    actions[k] = value
    set_server_keys(hub, enet_device, actions)
    await hub.save_installation()
    await hub._sync_config(hub.bridge.info)


async def planned_programming(hub: Hub, enet_device: dict[str, Any]) -> None:
    """Button "Program transmitter": write the queued operating modes (and the existing assignment)."""
    actions = sender_actions(hub, enet_device)
    assigned = planned_assignment(enet_device)
    repair = not pending(enet_device)  # nothing changed: check everything, including the actuators
    prepare_new_keys(hub, assigned)
    await program(hub, enet_device, assigned, all_actuators=repair)
    clear_pending(enet_device)
    set_server_keys(hub, enet_device, actions)  # GA base may have changed, rockers may be dropped
    await hub.save_installation()
    await hub._sync_config(hub.bridge.info)

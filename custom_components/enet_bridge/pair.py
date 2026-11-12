"""Pair and remove actuators without an eNet Server (via the bridge: send + monitor).

Procedure as the eNet Server does it (NOTES.md "Einlernen", tested with tools/einlernen.py 2026-10-26):
  1. Search under the server SN at GA 0: DoA_Read 0x3E1 + DoA_SelRead 0x3E3 → device in learning mode: DoA_Resp
     0x3E2 with its domain. Only a device in factory state (domain 0) is taken; devices with a domain – transmitters
     in learning mode also answer like this (hand transmitter measured 2026-10-29) – are skipped and reported.
  2. Product number via SN (NetParamRead 0x3DA, obj 0 PID 0xD2) → device type; unknown = abort before changing
  3. Select NetParamWrite 0x3E4 PID 71, domain 0x3EE + check 0x3EC, address 0x3DE + check 0x3DC
  4. GA base from the factory entry of the link table (GO 1)
Calibration on the side: if the bridge is not yet calibrated, the search cycles through the frequency corrections
(a device in learning mode answers even without an installation); the device's answers then fine-tune it.
Removing: A_Restart 0x381 `02 00` (factory state) to the address, answer 0x3A1 (wake transmitters first).
Name and assignment of removed devices stay in anlage["entfernt"]; the same transmitter takes them over when
re-paired (followed by "Program transmitter", which also adapts the actuator side to the new GA base).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .bridge import BridgeError
from .iex import ACTUATOR_ROCKERS, CHANNEL_TYPES, DEVICE_TYPES, PRODUCT_TYPES, ia_str, product_ident
from .measurements import MEASUREMENT_KINDS
from .texts import tr

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

SRV_IA = 0x7FFF  # 7.15.255
SEARCH_S = 120
ROUND_S = 3.0  # search round like the server (~3 s)


def frame(addr_hex: str, doa: bool, group: bool, src: int, dst: int, apci: int, data: bytes) -> str:
    """Telegram without CRCs (like knxrf::buildTelegram, tools/enet.py); the bridge sets the LFN."""
    b = bytearray([0, 0x44, 0xFF, 0x02]) + bytes.fromhex(addr_hex)
    b += bytes([0x00, src >> 8, src & 255, dst >> 8, dst & 255, (0x80 if group else 0) | 6 << 4 | (1 if doa else 0),
                (apci >> 8) & 3, apci & 255]) + bytes(data)
    b[0] = len(b) - 1
    return b.hex()


def apci(d: bytes) -> int:
    return (d[16] & 3) << 8 | d[17]


def ia_int(s: str) -> int:
    a, b, c = (int(x) for x in s.split("."))
    return a << 12 | b << 8 | c


class RadioSession:
    """Raw telegrams via the bridge: send (send) and listen to received ones (monitor)."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.rx: asyncio.Queue[bytes] = asyncio.Queue()
        self._down = None

    async def __aenter__(self) -> RadioSession:
        def listen(msg: dict[str, Any]) -> None:
            if msg.get("ev") == "telegram" and msg.get("dir") == "rx":
                self.rx.put_nowait(bytes.fromhex(msg["data"]))

        self._down = self.hub.bridge.add_listener(listen)
        await self.hub.bridge.command("monitor", on=True)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._down()
        try:
            await self.hub.bridge.command("monitor", on=False)
        except BridgeError:
            pass

    async def send_frame(self, f: str) -> None:
        await self.hub.bridge.command("send", frame=f)

    async def wait(self, matches, timeout: float) -> bytes | None:
        end = time.monotonic() + timeout
        while (rest := end - time.monotonic()) > 0:
            try:
                d = await asyncio.wait_for(self.rx.get(), rest)
            except asyncio.TimeoutError:
                return None
            if len(d) >= 18 and matches(d):
                return d
        return None

    async def request(self, f: str, matches, timeout: float = 0.5, attempts: int = 4) -> bytes | None:
        for _ in range(attempts):
            await self.send_frame(f)
            if (r := await self.wait(matches, timeout)) is not None:
                return r
        return None


def _type_for(hub: Hub, product: str) -> str | None:
    """Device type for a product number: from the catalogue definitions (all types), otherwise from our own table."""
    ident = product_ident(product)
    catalogue = {product_ident(d.get("produkt") or ""): typ for typ, d in hub.definitions.items()}
    if typ := catalogue.get(ident) or PRODUCT_TYPES.get(ident):
        return typ
    # Last digit = manufacturer (2 Gira, 3 Jung, e.g. WS3BG 521302 / WS3BJ 521303). Devices both build identically
    # are listed in the catalogue only under Gira: Jung sun sensor reports 521233, catalogue DVT_SF1S 521232.
    gira = ident[:5] + "2"
    return catalogue.get(gira) or PRODUCT_TYPES.get(gira)


async def _channel_variants(hub: Hub, typ: str, ia: str) -> dict[int, dict[str, Any]]:
    """Channel types chosen by device parameters (catalogue `varianten`), read from the paired device.

    Example: an operating top unit (UP1A) reports its insert (obj 9 PID 62: switching, dimming, blind …); the insert
    decides the type of channel 2 (CT_1F0B/CT_1F0C/CT_1F10 …) and of the rocker channel 1. A rule only applies while
    the channel still has one of the types it chooses between (a later rule refines an earlier one, e.g. the operating
    mode of a 2-channel switching insert). Returns {channel no.: {kanaltyp, obj, go_offset}}; unreadable → default."""
    chosen: dict[int, dict[str, Any]] = {}
    cache: dict[tuple, int | None] = {}
    for k in (hub.definitions.get(typ) or {}).get("kanaele", []):
        current = k["kanaltyp"]
        for rule in k.get("varianten", []):
            if current not in {v["kanaltyp"] for v in rule["werte"].values()}:
                continue
            p = rule["parameter"]
            loc = (p["obj"], p["pid"], p["start"])
            if loc not in cache:
                try:
                    r = await hub.bridge.request("prop_read", ia=ia, obj=p["obj"], pid=p["pid"], start=p["start"],
                                                 count=1)
                    data = bytes.fromhex(r.get("value", ""))
                    cache[loc] = data[0] if data else None
                except BridgeError as err:
                    _LOGGER.warning("Pairing %s: %s not read: %s", ia, p["katalog_id"], err)
                    cache[loc] = None
            if (v := rule["werte"].get(str(cache[loc]))) is not None:
                chosen[k["nr"]], current = v, v["kanaltyp"]
                _LOGGER.info("Pairing %s: channel %d → %s (%s = %d)", ia, k["nr"], current, p["katalog_id"],
                             cache[loc])
    return chosen


def _type_data(hub: Hub, typ: str, variants: dict[int, dict[str, Any]] | None = None
               ) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Description, role, actuator channels and rocker channels of a type – from the catalogue (channel type, GO
    offset, object; `variants` = types read from the device), otherwise from our own table. Channel types without an
    implementation (e.g. energy sensor) are left out. Rocker channels of an actuator (operating top unit: the rocker
    switches its own insert by wire and is also a transmitter) become keys of the actuator."""
    d = hub.definitions.get(typ)
    if d is None:
        desc, role, channels_ = DEVICE_TYPES[typ]
        return desc, role, [{"nr": i + 1, "art": a, "offset": o} for i, (a, o) in enumerate(channels_)], []
    defs = [k | (variants or {}).get(k["nr"], {}) for k in d["kanaele"]]
    kinds = [(k, CHANNEL_TYPES.get(k["kanaltyp"])) for k in defs]
    channels_ = [{"nr": k["nr"], "art": art, "offset": k["go_offset"], "obj": k["obj"], "kanaltyp": k["kanaltyp"]}
               for k, art in kinds if art in ("schalten", "dimmen", "jalousie")]
    # the rocker of an operating top unit does not make it a transmitter (no batteries, no sender programming)
    role = "aktor" if channels_ else ("sender" if any(a in ("wippe", "taste") and k["kanaltyp"] not in ACTUATOR_ROCKERS
                                                      for k, a in kinds) else "unbekannt")
    if role == "aktor":  # actuator with a measuring channel (sensIQ: motion + switching)
        channels_ += [{"nr": k["nr"], "art": art, "offset": k["go_offset"], "obj": k["obj"], "kanaltyp": k["kanaltyp"]}
                      for k, art in kinds if art in MEASUREMENT_KINDS]
        channels_.sort(key=lambda k: k["nr"])
    if role == "unbekannt" and (meas := [{"nr": k["nr"], "art": art, "offset": k["go_offset"], "obj": k["obj"],
                                           "kanaltyp": k["kanaltyp"]} for k, art in kinds if art in MEASUREMENT_KINDS]):
        role, channels_ = "sensor", meas  # energy sensor, motion detector, repeater (measurements.py)
    rockers = [{"nr": k["nr"], "art": "wippe", "offset": k["go_offset"], "obj": k["obj"], "kanaltyp": k["kanaltyp"],
                "ziele": []} for k, art in kinds if art == "wippe"] if role == "aktor" else []
    desc = DEVICE_TYPES.get(typ, (d.get("produktname") or d.get("name") or typ,))[0]
    return desc, role, channels_, rockers


def _free_ia(hub: Hub) -> str:
    used = {ia_int(g["ia"]) for g in hub.installation["geraete"]}
    n = 1
    while n in used:
        n += 1
    return ia_str(n)


async def pair(hub: Hub, name: str | None = None) -> dict[str, Any]:
    """Search for a device in learning mode, take it over and add it to the installation. Returns the device entry."""
    from .hub import CAL_CANDIDATES

    srv, dom = hub.installation["server"]["sn"], hub.installation["server"]["domain"]
    calibrated = bool(hub.radio.get("calibrated"))
    start = hub.radio.get("freq_offset", CAL_CANDIDATES[0])
    candidates = [start] if calibrated else list(dict.fromkeys((start, *CAL_CANDIDATES)))

    def srv_group(ap: int, data: bytes) -> str:
        return frame(srv, False, True, SRV_IA, 0, ap, data)

    def dom_group(ap: int, data: bytes) -> str:
        return frame(dom, True, True, SRV_IA, 0, ap, data)

    hub.activity(tr("akt_einlernen_start"))
    async with hub._cal_lock, RadioSession(hub) as radio_session:
        # 1. Search (with an uncalibrated bridge, cycling through all frequency corrections)
        sn, round_, offset = None, 0, start
        skipped: dict[str, str] = {}  # SN -> reason (device already has a domain)
        end = time.monotonic() + SEARCH_S
        while sn is None and time.monotonic() < end:
            f = candidates[round_ % len(candidates)]
            if f != offset:
                await hub._radio(freq_offset=f)
                offset = f
            round_ += 1
            await radio_session.send_frame(srv_group(0x3E1, b""))
            await asyncio.sleep(0.5)
            await radio_session.send_frame(srv_group(0x3E3, bytes.fromhex("02000000000000ffffffffffff00")))
            r = await radio_session.wait(lambda d: apci(d) == 0x3E2 and not d[15] & 1, ROUND_S - 0.5)
            if r is None:
                continue
            found_, domain_ = r[4:10].hex(), r[18:24].hex()
            if domain_ == "000000000000":  # factory state: take it
                sn = found_
            elif found_ not in skipped:  # transmitters in learning mode answer too, with their domain
                known = next((g for g in hub.installation["geraete"] if g["sn"] == found_), None)
                skipped[found_] = (tr("schon_in_anlage", name=known["name"], ia=known["ia"])
                                         if known else tr("andere_anlage", domaene=domain_))
                _LOGGER.info("Pairing: %s skipped: %s", found_, skipped[found_])
        if sn is None:
            if offset != start:
                await hub._radio(freq_offset=start)
            text = tr("nichts_gefunden")
            if skipped:
                text += tr("uebergangen", liste="; ".join(f"{k} {v}" for k, v in skipped.items()))
            raise BridgeError("timeout", text)
        snb = bytes.fromhex(sn)
        _LOGGER.info("Pairing: device %s found (frequency correction %d)", sn, offset)

        # 2. Product number → type (before any change to the device)
        r = await radio_session.request(srv_group(0x3DA, bytes.fromhex("c35143") + snb + bytes.fromhex("0000d20001")),
                               lambda d: apci(d) == 0x3DB and d[4:10] == snb)
        product = r[21:].hex() if r is not None else None
        typ = _type_for(hub, product) if product else None
        if typ is None:
            # for "Download diagnostics": see device types we do not know yet in bug reports
            hub.unknown_devices[sn] = {"produkt": product, "zeit": datetime.now().isoformat(timespec="seconds")}
            raise BridgeError("unknown_type", tr("typ_unbekannt", sn=sn, produkt=product))

        # 3. Select, domain, address
        # reset at the device but still in the installation: keep the previous address
        existing = next((g for g in hub.installation["geraete"] if g["sn"] == sn), None)
        ia_s = existing["ia"] if existing else _free_ia(hub)
        ia = ia_int(ia_s)
        await radio_session.request(srv_group(0x3E4, bytes.fromhex("c35147") + snb),
                           lambda d: apci(d) == 0x3DB and d[18:21] == bytes.fromhex("c35147"))
        await radio_session.send_frame(srv_group(0x3EE, snb + bytes.fromhex(dom)))
        await asyncio.sleep(0.15)
        r = await radio_session.request(srv_group(0x3EC, snb), lambda d: apci(d) == 0x3ED and d[18:24] == snb)
        if r is None or r[24:30].hex() != dom:
            raise BridgeError("learn", tr("domaene_nicht_gesetzt", wert=r[24:30].hex() if r is not None else tr("keine_antwort")))
        await radio_session.send_frame(dom_group(0x3DE, snb + ia.to_bytes(2, "big") + b"\0\0\0\0"))
        await asyncio.sleep(0.15)
        r = await radio_session.request(dom_group(0x3DC, snb), lambda d: apci(d) == 0x3DD and d[18:24] == snb)
        if r is None or (r[11] << 8 | r[12]) != ia:
            raise BridgeError("learn", tr("adresse_unbestaetigt"))

        # 4. GA base (factory entry GO 1)
        r = await radio_session.request(frame(dom, True, False, SRV_IA, ia, 0x2C8, bytes([3, 0x3F, 0, 0, 1, 0])),
                               lambda d: apci(d) == 0x2C9 and (d[11] << 8 | d[12]) == ia)
        base = r[28] if r is not None and len(r) >= 32 and r[20] == 0 else 0

        # fine-tuning from the device's answers
        est = await hub._radio()
        if est.get("freqest_n", 0) >= 3:
            new = max(-128, min(127, offset + round(est["freqest"])))
            if new != offset or not calibrated:
                await hub._radio(freq_offset=new, save=True)
                _LOGGER.info("Pairing: frequency correction %d saved", new)
        elif not calibrated:
            await hub._radio(freq_offset=offset, save=True)

    desc, role, channels_, rockers = _type_data(hub, typ, await _channel_variants(hub, typ, ia_s))
    enet_device = {"name": name or f"{desc} {ia_s}", "ia": ia_s, "sn": sn, "typ": typ, "beschreibung": desc,
              "rolle": role, "kanaele": channels_,
              "ga_basis": base, "ga_basis_geprueft": True, "produkt": product,
              "eingelernt": time.strftime("%Y-%m-%d %H:%M")}
    previous = hub.installation.get("entfernt", {}).pop(sn, None)  # same device was in the installation before
    if existing is not None:  # factory reset at the device: replace the entry, keep name/assignment/old base
        hub.installation["geraete"].remove(existing)
        previous = {k: existing[k] for k in ("name", "tasten", "zentral", "ga_basis", "raum") if k in existing}
    if previous and not name:
        enet_device["name"] = previous["name"]
    if previous and "raum" in previous:
        enet_device["raum"] = previous["raum"]
    if rockers:  # operating top unit: its rocker as a key of the actuator (events in HA)
        enet_device["tasten"] = rockers
    if role == "sender":  # assignment: as before removal, otherwise all rockers free (program afterwards)
        from .sender import default_assignment, keys_from, layout
        enet_device["tasten"] = (previous or {}).get("tasten") or keys_from(
            enet_device, default_assignment(enet_device), layout(hub, enet_device))
        enet_device["zentral"] = (previous or {}).get("zentral", [])
        if previous and "ga_basis" in previous:  # keep the old base until "Program transmitter" has
            enet_device["ga_basis"] = previous["ga_basis"]  # moved the actuator links to the new one
            enet_device["ga_basis_geprueft"] = False
    hub.installation["geraete"].append(enet_device)
    await hub.save_installation()
    hub.activity(tr("akt_eingelernt", name=enet_device["name"], typ=desc, ia=ia_s))
    return enet_device


def impact(hub: Hub, enet_device: dict[str, Any]) -> dict[str, list[str]]:
    """What else is affected by removal (for the confirmation in the dialog)."""
    ia = enet_device["ia"]
    scenes = [s.name for s in hub.scenes.scenes if any(m.startswith(f"{ia}/") for m in s.members)]
    esp_scenes = [n for n, actions in hub.installation.get("esp", {}).get("szenen", {}).items()
                  if any(a["ia"] == ia for a in actions)]
    references, actuators, keys_ = [], [], []
    if enet_device.get("rolle") == "sender":
        for t in enet_device.get("tasten", []):
            for z in t.get("ziele", []):
                a = next((g for g in hub.installation["geraete"] if g["ia"] == z["ia"]), None)
                actuators.append(tr("verweis_aktor_wippe", aktor=a["name"] if a else z["ia"], ch=z["ch"], n=(t["nr"] + 1) // 2))
        keys_ = [t.get("name") or str(t["ga"]) for t in hub.installation.get("esp", {}).get("server_tasten", [])
                  if t["sender"] == enet_device["sn"]]
    else:
        for g in hub.installation["geraete"]:
            for t in g.get("tasten", []):
                if any(z["ia"] == ia for z in t.get("ziele", [])):
                    references.append(tr("verweis_wippe", name=g["name"], n=(t["nr"] + 1) // 2))
            if any(z["ia"] == ia for z in g.get("zentral", [])):
                references.append(tr("verweis_zentral", name=g["name"]))
    return {"szenen": scenes, "esp_szenen": esp_scenes, "verweise": references, "aktoren": actuators, "tasten": keys_}


async def remove(hub: Hub, enet_device: dict[str, Any]) -> dict[str, Any]:
    """Put the device into factory state, remove it from the installation and clean up: scenes, ESP scenes and
    references of other transmitters (actuator) or actuator links and server keys (transmitter).
    Returns {"bestaetigt": device confirmed the reset, "offen": items not done}."""
    from .sender import Sender, _actuator_links, assignment

    dom, ia = hub.installation["server"]["domain"], ia_int(enet_device["ia"])
    open_items: list[str] = []
    async with RadioSession(hub) as radio_session:
        if hub.sleeps(enet_device):  # transmitters, sun sensor …: wake first (press a key)
            hub.activity(tr("akt_entfernen_wecken", name=enet_device["name"]))
            await Sender(hub, enet_device, radio_session).wake()
        r = await radio_session.request(frame(dom, True, False, SRV_IA, ia, 0x381, b"\x02\x00"),
                               lambda d: apci(d) == 0x3A1 and (d[11] << 8 | d[12]) == ia, timeout=0.6, attempts=6)
    if r is None:
        open_items.append(tr("reset_unbestaetigt"))
    esp = hub.installation.setdefault("esp", {})
    if enet_device.get("rolle") == "sender":
        try:  # actuator side of the rockers: delete all entries of this transmitter
            await _actuator_links(hub, enet_device, assignment(enet_device), {}, 0)
        except BridgeError as err:
            open_items.append(tr("links_nicht_geloescht", err=err))
        esp["server_tasten"] = [t for t in esp.get("server_tasten", []) if t["sender"] != enet_device["sn"]]
    else:
        # references of other transmitters (links in the transmitter stay until programming)
        for g in hub.installation["geraete"]:
            for t in g.get("tasten", []):
                if "ziele" in t:
                    t["ziele"] = [z for z in t["ziele"] if z["ia"] != enet_device["ia"]]
            if "zentral" in g:
                g["zentral"] = [z for z in g["zentral"] if z["ia"] != enet_device["ia"]]
        for n in list(esp.get("szenen", {})):
            esp["szenen"][n] = [a for a in esp["szenen"][n] if a["ia"] != enet_device["ia"]]
    hub.installation["geraete"].remove(enet_device)
    # remember: if the same device is paired again, it takes over name and assignment
    hub.installation.setdefault("entfernt", {})[enet_device["sn"]] = {k: enet_device[k] for k in ("name", "tasten", "zentral", "ga_basis")
                                                          if k in enet_device}
    for s in hub.installation.get("szenen", []):
        s["mitglieder"] = [m for m in s.get("mitglieder", []) if m["ia"] != enet_device["ia"]]
    await hub.save_installation()
    await hub.scenes.remove_member(enet_device["ia"])
    try:
        await hub._sync_config(hub.bridge.info)
    except BridgeError as err:
        open_items.append(tr("config_nicht_uebertragen", err=err))
    hub.activity(tr("akt_entfernt_offen", name=enet_device["name"], offen="; ".join(open_items)) if open_items
                   else tr("akt_entfernt", name=enet_device["name"]))
    return {"bestaetigt": r is not None, "offen": open_items}


async def add(hub: Hub, name: str | None = None, room: str | None = None) -> dict[str, Any]:
    """Complete pairing (service and "+ Add device"): search, take over, set room; a transmitter is programmed right
    away (awake in learning mode) and released – its learning LED stops blinking = paired."""
    known = {x["sn"] for x in hub.installation["geraete"]}
    g = await pair(hub, name)
    if g["rolle"] == "aktor" and g["sn"] in known:
        # factory reset of an actuator of the installation: scene links and values in the device are gone – write back
        hub.activity(tr("akt_szenen_wiederherstellen", name=g["name"]))
        errors = await hub.check.restore_actuator(g["ia"])
        from .sender import restore_rocker_links

        errors += await restore_rocker_links(hub, g["ia"])
        if errors:
            _LOGGER.warning("Restoring scenes of %s: %s", g["name"], "; ".join(errors))
    if room:
        g["raum"] = room  # suggestion for the HA area when creating the device
        await hub.save_installation()
    if g["rolle"] == "sender":
        from .sender import program

        await program(hub, g, after_pairing=True)
    return g


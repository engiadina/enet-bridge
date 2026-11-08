"""Hand- und Wandsender programmieren: Betriebsart je Wippe, Wippe → Aktoren, Einzeltaste → Server-Taste.

Nachbau des eNet Servers (Mitschnitt umstellung1.jsonl 2026-10-26, NOTES.md „SENDER-PROGRAMMIERUNG KOMPLETT“):
Der Server schreibt bei jeder Änderung ALLE Kanäle des Senders neu.
  Wecken: unter Server-SN an GA 0 NPWrite PID 72 = Sender-SN + 02 00000000 (alle ~100 ms), bis der Sender
          nach einem Tastendruck antwortet; wach halten mit dem Rundruf PID 72 = 04 05 00 00 00 (alle ~5 s)
  Rahmen: Lade-Status 02, Obj 0x10 PID 39 = …03, Tasten-Objekte, Kanäle, Zeitstempel, Obj 0x10 PID 48 = 03
  Tasten-Objekte 2n+1/2n+2 PID 4F[1]: Wippe = 0x0B+n / ff, Einzeltasten = 00 / 00
  Kanal (Zentral 0x0B = Kanal 9 FT_INAOOS mit Status-LED-Links „zentral“, Einzeltaste k = 2+k, Wippe n = 0x0B+n):
          Parameter, D1 lesen, D0 = 04, D2 lesen,
          A_Restart 06 c (Links zurücksetzen), Werkseinträge (01 00 eigene SN GA → GO), Verknüpfungen, D2 = 04
Wippe n: Werks-GAs 48+3n+1…+3 → GO 1…3, Aktor-Status-GA(s) → GO 1 (Status-LED).
Einzeltaste k: PID E2 = Tastencode (vom Server vergeben; in der Anlage gespeichert, nur ohne gespeicherten Code
          aus dem Gerät übernommen – nach einem Werksreset steht dort ein Werkswert), Werks-GAs 6(k-1)+1…6k
          → GO 1…6, Server-SN + Antwort-GA → GO 1. Die Aktor-Seite einer Wippe (GA 50+3n → GO 1,
          51+3n → GO 19) bleibt hier unberührt.
Wippe mit HA-Lampen (ha_ziele): zusätzlich Server-SN + Antwort-GA → GO 1. Die Wippe fragt dann (auch) die
          Server-SN; die Bridge antwortet wie bei einer Server-Taste (grüne LED), HA schaltet/dimmt die Lampen
          (ha_wippe.py). Welche HA-Lampen es sind, steht nur in der Anlage – Ändern braucht kein Programmieren.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.logbook import async_log_entry
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .bridge import BridgeError
from .const import DOMAIN
from .einlernen import SRV_IA, Funk, apci, frame, ia_int
from .texte import tr

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

OBJ_ZENTRAL, OBJ_GERAET = 0x0B, 0x10
PID_LINKS = 0x3F
WECKEN_S = 60
WACH_HALTEN_S = 5.0


def belegung(geraet: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Wippe n → {"art": "wippe", "ziele": [{ia, ch}], "ha_ziele": [entity_id], "antwort_ga"} bzw.
    {"art": "tasten", "tasten": {k: {antwort_ga}}}."""
    b: dict[int, dict[str, Any]] = {}
    for t in geraet.get("tasten", []):
        n = (t["nr"] + 1) // 2
        if t["art"] == "wippe":
            if "ziele" not in t:  # unbekannt ≠ leer: sonst würden bestehende Verknüpfungen gelöscht
                raise BridgeError("bad_args", tr("wippe_ziele_unbekannt", name=geraet["name"], n=n))
            b[n] = {"art": "wippe", "ziele": t["ziele"], "ha_ziele": list(t.get("ha_ziele", [])),
                    "antwort_ga": t.get("antwort_ga")}
        else:
            b.setdefault(n, {"art": "tasten", "tasten": {}})["tasten"][t["nr"]] = {
                "antwort_ga": t.get("antwort_ga"), "code": t.get("code")}
    return b


class Sender:
    def __init__(self, hub: Hub, geraet: dict[str, Any], funk: Funk) -> None:
        self.hub, self.g, self.funk = hub, geraet, funk
        self.ia = geraet["ia"]
        self.sn = geraet["sn"]
        self.basis = geraet.get("ga_basis", 0) * 256
        self.dom = hub.anlage["server"]["domain"]
        self.srv = hub.anlage["server"]["sn"]

    # --- Bausteine ----------------------------------------------------------
    async def _anfrage(self, art: str, beschreibung: str, **kw: Any) -> dict:
        """Bridge-Anfrage an den Sender, mit Dauer im Debug-Log (Fehlersuche: Programmieren dauert lange)."""
        t0 = time.monotonic()
        try:
            r = await self.hub.bridge.request(art, ia=self.ia, **kw)
        except BridgeError as err:
            _LOGGER.debug("%s %s: failed after %.1f s (%s)", self.g["name"], beschreibung, time.monotonic() - t0, err)
            raise
        _LOGGER.debug("%s %s: %.1f s, %s tries", self.g["name"], beschreibung, time.monotonic() - t0, r.get("tries"))
        return r

    async def pr(self, obj: int, pid: int) -> bytes:
        r = await self._anfrage("prop_read", f"read obj {obj} pid {pid:#x}", obj=obj, pid=pid, start=1, count=1)
        return bytes.fromhex(r.get("value", ""))

    async def pw(self, obj: int, pid: int, data: bytes, start: int = 1, count: int = 1) -> None:
        """count = Anzahl Elemente (Lade-Status 10 Byte und Zeitstempel 6 Byte sind je ein Element)."""
        await self._anfrage("prop_write", f"write obj {obj} pid {pid:#x}", obj=obj, pid=pid, start=start,
                            count=count, data=data.hex())

    async def link(self, obj: int, op: int, sn: str, ga: int, go: int) -> None:
        data = bytes([op, 0]) + bytes.fromhex(sn) + ga.to_bytes(2, "big") + go.to_bytes(2, "big")
        await self._anfrage("fp_cmd", f"link obj {obj} op {op} {sn} ga {ga:#x} go {go}", obj=obj, pid=PID_LINKS,
                            data=data.hex())

    async def restart(self, kanal: int) -> None:
        """A_Restart 06 c: Links des Kanals zurücksetzen (Antwort 0x3A1)."""
        ia = ia_int(self.ia)
        r = await self.funk.anfrage(frame(self.dom, True, False, SRV_IA, ia, 0x381, bytes([6, kanal])),
                                    lambda d: apci(d) == 0x3A1 and (d[11] << 8 | d[12]) == ia, timeout=1.0, versuche=5)
        if r is None:
            raise BridgeError("timeout", tr("links_reset_unbestaetigt", kanal=kanal))
        await asyncio.sleep(0.3)  # der Server wartet hier ~1 s (erster Link danach oft unbeantwortet)

    async def kanal(self, obj: int, param: tuple[int, bytes], kanal: int, werk: list[tuple[int, int]],
                    links: list[tuple[str, int]], cd: bool = True) -> None:
        """Kanal neu schreiben: Parameter, Prüfmarke, Links zurücksetzen, Werkseinträge, Verknüpfungen.
        param = (Start, Werte) in PID 4F (je Byte ein Element); cd: PID CD = 00 (nicht beim Zentralkanal)."""
        if cd:
            await self.pw(obj, 0xCD, b"\x00")
        await self.pw(obj, 0x4F, param[1], start=param[0], count=len(param[1]))
        await self.pr(obj, 0xD1)
        await self.pw(obj, 0xD0, b"\x04")
        await self.pr(obj, 0xD2)
        await self.restart(kanal)
        for ga, go in werk:
            await self.link(obj, 1, self.sn, ga, go)
        for sn, ga in links:
            await self.link(obj, 0, sn, ga, 1)
        await self.pw(obj, 0xD2, b"\x04")

    # --- Ablauf -------------------------------------------------------------
    async def wecken(self) -> None:
        f = frame(self.srv, False, True, SRV_IA, 0, 0x3E4, bytes.fromhex("c35148") + bytes.fromhex(self.sn)
                  + bytes.fromhex("0200000000"))
        snb = bytes.fromhex(self.sn)
        t0 = time.monotonic()
        ende = t0 + WECKEN_S
        versuche = 0
        while time.monotonic() < ende:
            await self.funk.senden(f)
            versuche += 1
            if await self.funk.warten(lambda d: apci(d) == 0x3DB and d[4:10] == snb, 0.1) is not None:
                _LOGGER.debug("%s awake after %.1f s (%d wake frames)", self.g["name"], time.monotonic() - t0, versuche)
                return
        raise BridgeError("timeout", tr("nicht_aufgewacht", name=self.g["name"]))

    async def wach_halten(self) -> None:
        f = frame(self.dom, True, True, SRV_IA, 0, 0x3E4, bytes.fromhex("c351480405000000"))
        while True:
            await asyncio.sleep(WACH_HALTEN_S)
            _LOGGER.debug("%s keep awake", self.g["name"])
            with contextlib.suppress(BridgeError):
                await self.funk.senden(f)

    def status_links(self, ziele: list[dict[str, Any]]) -> list[tuple[str, int]]:
        """Aktor-SN + Status-GA (Basis·256 + Kanaloffset + 22) je Ziel – die Status-LED der Wippe."""
        out = []
        for z in ziele:
            a = next((g for g in self.hub.anlage["geraete"] if g["ia"] == z["ia"]), None)
            k = next((k for k in (a or {}).get("kanaele", []) if k["nr"] == z["ch"]), None)
            if a is None or k is None:
                raise BridgeError("bad_args", tr("ziel_fehlt", ia=z["ia"], ch=z["ch"]))
            out.append((a["sn"], a.get("ga_basis", 0) * 256 + k["offset"] + 22))
        return out

    async def basis_lesen(self) -> int:
        """GA-Basis aus dem Werkseintrag GO 1 des Zentralkanals (zählt bei jedem Werksreset weiter). Verknüpfungen
        (Typ 00) stehen in der Tabelle vor dem Werkseintrag (Typ 01, eigene SN) – daher bis zu diesem suchen."""
        for idx in range(16):
            r = await self.hub.bridge.request("fp_read", ia=self.ia, obj=OBJ_ZENTRAL, pid=PID_LINKS,
                                              data=f"000001{idx:02x}")
            v = bytes.fromhex(r.get("value", ""))
            if len(v) >= 9 and v[0] == 0x01 and v[1:7].hex() == self.sn:
                return v[7]
        raise BridgeError("bad_reply", tr("werkseintrag_fehlt"))

    async def schreiben(self, bel: dict[int, dict[str, Any]]) -> None:
        wippen = sorted(bel)  # nur die belegten Wippen; die übrigen bleiben, wie sie sind
        self.basis = await self.basis_lesen() * 256
        await self.pr(OBJ_GERAET, 0x39)
        for n in wippen:
            await self.pr(2 * n + 1, 0xCF)
        await self.pr(1, 6)
        await self.pw(1, 6, b"\x02" + bytes(9))  # Lade-Status: Laden beginnt
        await self.pr(1, 6)
        await self.pr(OBJ_GERAET, 0x39)
        await self.pw(OBJ_GERAET, 0x39, bytes.fromhex("000000000003"))
        for n in wippen:  # Tasten-Objekte: Wippe oder Einzeltasten
            w = bel[n]["art"] == "wippe"
            await self.pw(2 * n + 1, 0x4F, bytes([0x0B + n if w else 0]))
            await self.pw(2 * n + 2, 0x4F, bytes([0xFF if w else 0]))
        # Zentralkanal (Kanal 9, CT_1F15, FT_INAOOS): nur Status-LED-Links zu Aktoren (Anlage „zentral“)
        if "zentral" not in self.g:  # unbekannt ≠ leer: sonst würden bestehende Links gelöscht
            raise BridgeError("bad_args", tr("zentral_unbekannt", name=self.g["name"]))
        await self.kanal(OBJ_ZENTRAL, (2, b"\x01\x01"), 9, [(self.basis + 49 + i, 1 + i) for i in range(3)],
                         self.status_links(self.g["zentral"]), cd=False)
        await self.pw(OBJ_GERAET, 0x41, b"\x01")
        for n in wippen:
            b = bel[n]
            if b["art"] == "tasten":
                for k in (2 * n - 1, 2 * n):
                    obj = 2 + k
                    taste = b["tasten"].setdefault(k, {})
                    if taste.get("code") is None:
                        # Tastencode (PID E2) vergibt der Server; gespeichert gilt er, sonst den im Gerät übernehmen
                        # – nach einem Werksreset steht dort ein Werkswert (Taste 2 sendete dann eine Szene)
                        taste["code"] = ((await self.pr(obj, 0xE2))[:1] or b"\x00")[0]
                    await self.pw(obj, 0xE2, bytes([taste["code"]]))
                    antwort = taste.get("antwort_ga")
                    if antwort is None:
                        raise BridgeError("bad_args", tr("keine_antwort_ga", k=k))
                    await self.kanal(obj, (4, b"\x01\x01"), k,
                                     [(self.basis + 6 * (k - 1) + 1 + i, 1 + i) for i in range(6)],
                                     [(self.srv, antwort)])
            else:
                links = self.status_links(b["ziele"])
                if b.get("ha_ziele"):  # HA-Lampen: die Bridge (Server-SN) beantwortet die Statusabfrage
                    if b.get("antwort_ga") is None:
                        raise BridgeError("bad_args", tr("keine_antwort_ga", k=2 * n - 1))
                    links.append((self.srv, b["antwort_ga"]))
                await self.kanal(0x0B + n, (1, b"\x01\x01"), 2 * n - 1,
                                 [(self.basis + 48 + 3 * n + 1 + i, 1 + i) for i in range(3)], links)
        t = datetime.now()
        await self.pw(OBJ_GERAET, 0x39, bytes([t.year % 100, t.month, t.day, t.hour, t.minute, t.second]))
        lade = await self.pr(1, 6)
        if lade[:1] != b"\x01":
            _LOGGER.warning("%s: load state after programming %s instead of 01", self.g["name"], lade.hex())


def tasten_aus(geraet: dict[str, Any], bel: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Belegung → Einträge „tasten“ der Anlage (Offsets/Objekte nach der Regel des Servers)."""
    out = []
    for n in sorted(bel):
        b = bel[n]
        if b["art"] == "wippe":
            e = {"nr": 2 * n - 1, "art": "wippe", "offset": 48 + 3 * n, "obj": 0x0B + n, "ziele": b["ziele"]}
            if b.get("ha_ziele"):
                e["ha_ziele"], e["antwort_ga"] = list(b["ha_ziele"]), b.get("antwort_ga")
            out.append(e)
        else:
            for k in (2 * n - 1, 2 * n):
                t = b["tasten"].get(k, {})
                out.append({"nr": k, "art": "taste", "offset": 6 * (k - 1), "obj": 2 + k,
                            "antwort_ga": t.get("antwort_ga"), "code": t.get("code")})
    return out


def standard_belegung(geraet: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Neuer Sender: Wippe 1 (Tasten 1/2) als Wippe ohne Ziele – passt für jeden Sender (auch Wandsender mit nur
    einer Wippe); die übrigen Wippen bleiben im Werkszustand, bis sie belegt werden."""
    return {1: {"art": "wippe", "ziele": []}}


async def _aktor_links(hub: Hub, geraet: dict[str, Any], alt: dict, neu: dict, basis: int,
                       wippen: set[int] | None = None) -> None:
    """Aktor-Seite der Wippen abgleichen – nach der Link-Tabelle im Aktor, nicht nach der Anlage (die kennt nach
    Werksreset/Import nicht sicher die GA, die im Aktor steht). Soll je Ziel: Tastenwert-GA (Basis·256 + 50+3n)
    → GO 1, Statusabfrage (+1) → GO 19. Alle Einträge dieses Senders mit anderer GA werden gelöscht.
    wippen: nur die Aktoren dieser Wippen (alt und neu) prüfen; None = alle. Das Lesen der Link-Tabellen kostet je
    Eintrag bis 3 s (langsame Aktoren antworten erst im 5. Versuch) – unveränderte Wippen nicht anfassen."""
    soll: dict[tuple[str, int], set[tuple[int, int]]] = {}
    pruefen: set[tuple[str, int]] = set()  # Aktoren an geänderten Wippen (alt oder neu)
    for bel, mit in ((alt, False), (neu, True)):
        for n, b in bel.items():
            if b["art"] != "wippe":
                continue
            for z in b["ziele"]:
                # Soll immer aus ALLEN Wippen: ein Aktor kann an mehreren Wippen dieses Senders hängen, sonst
                # würden die Links der unveränderten Wippe gelöscht (so geschehen 2026-11-08, 0.12.9)
                s = soll.setdefault((z["ia"], z["ch"]), set())
                if mit:
                    s |= {(basis + 50 + 3 * n, 1), (basis + 51 + 3 * n, 19)}
                if wippen is None or n in wippen:
                    pruefen.add((z["ia"], z["ch"]))
    for (ia, ch), sollwert in sorted(soll.items()):
        if (ia, ch) not in pruefen:
            continue
        k = next((k for k in hub.kanaele if k.ia == ia and k.nr == ch), None)
        if k is None:
            _LOGGER.warning("%s: target %s/%s is not part of the installation", geraet["name"], ia, ch)
            continue
        ist: set[tuple[int, int]] = set()
        for go in (1, 19):
            for idx in range(32):
                v = await _link_lesen(hub, k, go, idx)
                if v is None:
                    break  # Ende der Tabelle
                if len(v) >= 11 and v[1:7].hex() == geraet["sn"]:
                    ist.add((int.from_bytes(v[7:9], "big"), int.from_bytes(v[9:11], "big")))
        weg, dazu = ist - sollwert, sollwert - ist
        if not weg and not dazu:
            continue

        async def schritte(k=k, weg=weg, dazu=dazu) -> None:
            for (ga, go), op in [(x, 2) for x in sorted(weg)] + [(x, 0) for x in sorted(dazu)]:
                data = bytes([op, 0]) + bytes.fromhex(geraet["sn"]) + ga.to_bytes(2, "big") + go.to_bytes(2, "big")
                await hub.bridge.request("fp_cmd", ia=k.ia, obj=k.obj, pid=PID_LINKS, data=data.hex())

        _LOGGER.info("%s: actuator %s delete links %s, create %s", geraet["name"], k.key, sorted(weg), sorted(dazu))
        await hub.params.programmieren(k.ia, hub.geraet_obj(k.geraet), k.obj, schritte,
                                       zweck=tr("zweck_verknuepfungen", name=geraet["name"]))


def geaenderte_wippen(alt: dict[int, dict[str, Any]], neu: dict[int, dict[str, Any]]) -> set[int]:
    """Wippen, deren Betriebsart oder eNet-Lampen sich ändern (HA-Lampen betreffen die Aktoren nicht)."""
    def kennung(b: dict[str, Any] | None):
        if b is None:
            return None
        return b["art"], frozenset((z["ia"], z["ch"]) for z in b.get("ziele") or [])
    return {n for n in set(alt) | set(neu) if kennung(alt.get(n)) != kennung(neu.get(n))}


async def _link_lesen(hub: Hub, k, go: int, idx: int, versuche: int = 3) -> bytes | None:
    """Eintrag idx der Link-Tabelle von GO go; None = Ende der Tabelle (nack mit rc 255). Andere Fehler
    (Funk) werden wiederholt – eine unvollständig gelesene Tabelle würde vorhandene Links neu anlegen (nack)."""
    for n in range(versuche):
        t0 = time.monotonic()
        try:
            r = await hub.bridge.request("fp_read", ia=k.ia, obj=k.obj, pid=PID_LINKS, data=f"0000{go:02x}{idx:02x}")
            _LOGGER.debug("%s link GO %d #%d: %.1f s, %s tries", k.key, go, idx, time.monotonic() - t0, r.get("tries"))
            return bytes.fromhex(r.get("value", ""))
        except BridgeError as err:
            _LOGGER.debug("%s link GO %d #%d: %s after %.1f s", k.key, go, idx, err, time.monotonic() - t0)
            if err.err == "nack" and err.rc == 255:
                return None
            if n + 1 == versuche:
                raise BridgeError(err.err, tr("linktabelle_unlesbar", key=k.key, err=err)) from err
    return None


def _server_tasten_basis(hub: Hub, geraet: dict[str, Any], basis: int) -> bool:
    """GAs der Server-Tasten dieses Senders in der ESP-Konfiguration auf die neue Basis setzen."""
    geaendert = False
    for t in hub.anlage.get("esp", {}).get("server_tasten", []):
        if t["sender"] == geraet["sn"] and t["ga"] >> 8 != basis >> 8:
            t["ga"] = basis + (t["ga"] & 0xFF)
            geaendert = True
    return geaendert


def aktivitaet(hub: Hub, geraet: dict[str, Any], text: str) -> None:
    """Eintrag in der Aktivität des Senders (Logbuch, am Knopf „Sender programmieren“) – auf der Hauptseite und
    auf jeder Wippen-Seite, denn programmiert wird oft von dort."""
    reg = er.async_get(hub.hass)
    for e in er.async_entries_for_config_entry(reg, hub.entry_id):
        if e.domain == "button" and e.unique_id.startswith(f"{geraet['sn']}_") \
                and e.unique_id.endswith("_sender_programmieren"):
            async_log_entry(hub.hass, geraet["name"], text, DOMAIN, e.entity_id)


async def programmieren(hub: Hub, geraet: dict[str, Any], bel: dict[int, dict[str, Any]] | None = None,
                        nach_einlernen: bool = False, alle_aktoren: bool = True) -> None:
    """Sender wecken (Benutzer drückt eine Taste), komplett neu programmieren, danach die Aktor-Seite der Wippen
    und die Server-Tasten der Bridge an Belegung und GA-Basis anpassen. Je Sender nur ein Lauf gleichzeitig."""
    if geraet["sn"] in hub.sender_aktiv:
        raise BridgeError("laeuft", tr("programmierung_laeuft"))
    hub.sender_aktiv.add(geraet["sn"])
    async_dispatcher_send(hub.hass, hub.signal(f"sender_aktiv_{geraet['sn']}"))  # Knöpfe sperren
    try:
        await _programmieren(hub, geraet, bel, nach_einlernen, alle_aktoren)
    except BridgeError as err:
        aktivitaet(hub, geraet, tr("akt_programmierung_fehlgeschlagen", err=err))
        raise
    finally:
        hub.sender_aktiv.discard(geraet["sn"])
        async_dispatcher_send(hub.hass, hub.signal(f"sender_aktiv_{geraet['sn']}"))


async def _programmieren(hub: Hub, geraet: dict[str, Any], bel: dict[int, dict[str, Any]] | None,
                         nach_einlernen: bool, alle_aktoren: bool = True) -> None:
    alt = belegung(geraet)
    alte_basis = geraet.get("ga_basis") if geraet.get("ga_basis_geprueft") else None
    bel = bel or alt
    aktivitaet(hub, geraet, tr("akt_eingelernt_taste") if nach_einlernen else tr("akt_sender_wecken"))
    async with hub.params.lock, Funk(hub) as funk:
        s = Sender(hub, geraet, funk)
        await s.wecken()
        aktivitaet(hub, geraet, tr("akt_sender_aktiv"))
        halten = hub.hass.async_create_background_task(s.wach_halten(), "enet Sender wach halten")
        try:
            t0 = time.monotonic()
            await s.schreiben(bel)
            _LOGGER.debug("%s: transmitter written in %.1f s", geraet["name"], time.monotonic() - t0)
        finally:
            halten.cancel()
            with contextlib.suppress(BridgeError):
                await s.pw(OBJ_GERAET, 0x48, b"\x03")  # Sender entlassen
    geraet["tasten"] = tasten_aus(geraet, bel)
    geraet["ga_basis"], geraet["ga_basis_geprueft"] = s.basis >> 8, True
    neue_tasten = _server_tasten_basis(hub, geraet, s.basis)
    await hub.anlage_speichern()
    if neue_tasten:
        await hub._config_abgleichen(hub.bridge.info)
    try:  # der Sender ist fertig; ein Fehler hier betrifft nur die Aktor-Seite der Wippen
        t0 = time.monotonic()
        # alle Aktoren nur bei neuer GA-Basis (Werksreset) oder auf Wunsch (Knopf ohne Änderung = Reparatur)
        wippen = None if alle_aktoren or alte_basis != s.basis >> 8 else geaenderte_wippen(alt, bel)
        _LOGGER.debug("%s: actuator links for rockers %s", geraet["name"], "all" if wippen is None else sorted(wippen))
        await _aktor_links(hub, geraet, alt, bel, s.basis, wippen)
        _LOGGER.debug("%s: actuator links in %.1f s", geraet["name"], time.monotonic() - t0)
    except BridgeError as err:
        _LOGGER.warning("%s: actuator links not fully updated: %s", geraet["name"], err)
        aktivitaet(hub, geraet, tr("akt_links_unvollstaendig", err=err))
        return
    aktivitaet(hub, geraet, tr("akt_programmierung_beendet"))


# --- Belegung ändern (Dialog „Sender belegen“) ------------------------------
ANTWORT_GA_AB = 1000  # Antwort-GAs neuer Einzeltasten (unter Server-SN), oberhalb der vom Server/HA vergebenen
MELDEN = "melden"


def wippen_anzahl(hub: Hub, geraet: dict[str, Any]) -> int:
    """Wippen des Sendertyps = Wippen-Kanäle (CT_1F11) im Katalog; sonst aus der Belegung (mindestens 1)."""
    d = hub.definitionen.get(geraet.get("typ", ""))
    if d:
        n = sum(1 for k in d["kanaele"] if k["kanaltyp"] == "CT_1F11")
        if n:
            return n
    return max([(t["nr"] + 1) // 2 for t in geraet.get("tasten", [])] + [1])


def tasten_code(k: int) -> int:
    """PID E2 nach dem Schema des Servers: Taste 1 → 0, Taste 2 → 15, Taste k ≥ 3 → k − 2 (Taste 7/8 → 5/6)."""
    return {1: 0, 2: 15}.get(k, k - 2)


def taste_ga(geraet: dict[str, Any], k: int) -> int:
    """GA, an die Einzeltaste k ihren Sammelstatus schickt (Server-Taste): Basis·256 + Offset 6(k−1) + 6."""
    return geraet.get("ga_basis", 0) * 256 + 6 * (k - 1) + 6


def _szenen_aufruf(aktionen: list[dict[str, Any]]) -> tuple[int, int] | None:
    """Bridge-Szene, die nur eine eNet-Szene aufruft → (GA, Nr.)."""
    if len(aktionen) == 1 and aktionen[0]["ia"] == "0.0.0":
        return aktionen[0]["handle"], int(aktionen[0]["wert"], 16)
    return None


def aktion_optionen(hub: Hub) -> list[tuple[str, str]]:
    """Wählbare Aktionen einer Einzeltaste: (Wert, Anzeige)."""
    out = [(MELDEN, tr("aktion_melden"))]
    out += [(f"ha:{ha_id}", tr("aktion_szene", name=name)) for ha_id, name in hub.ha_szenen.namen().items()]
    out += [(f"enet:{s.id}", tr("aktion_enet_szene", name=s.name)) for s in hub.szenen.szenen if s.art != "ha"]
    out += [(f"esp:{n}", tr("aktion_bridge_szene", name=n)) for n, a in hub.anlage.get("esp", {}).get("szenen", {}).items()
            if _szenen_aufruf(a) is None]
    return out


def aktion_der_taste(hub: Hub, geraet: dict[str, Any], k: int) -> str:
    esp = hub.anlage.get("esp", {})
    t = next((t for t in esp.get("server_tasten", []) if t["sender"] == geraet["sn"]
              and t["ga"] & 0xFF == taste_ga(geraet, k) & 0xFF and (t.get("szene") or t.get("ha"))), None)
    if t is not None and t.get("ha"):
        return f"ha:{t['ha']}"
    if t is None or t["szene"] not in esp.get("szenen", {}):
        return MELDEN
    aufruf = _szenen_aufruf(esp["szenen"][t["szene"]])
    s = next((s for s in hub.szenen.szenen if (s.ga, s.nr) == aufruf), None) if aufruf else None
    if s and s.ha_id:  # eNet-Szene, die inzwischen der eNet-Teil einer HA-Szene ist (umgewandelter Import)
        return f"ha:{s.ha_id}"
    return f"enet:{s.id}" if s else f"esp:{t['szene']}"


def muss_programmieren(geraet: dict[str, Any], bel: dict[int, dict[str, Any]]) -> bool:
    """Nur Betriebsart, Lampen der Wippen und neue Einzeltasten verändern den Sender; die Aktion einer
    Einzeltaste steht in der Bridge."""
    alt = belegung(geraet)
    for n, b in bel.items():
        a = alt.get(n)
        if a is None or a["art"] != b["art"]:
            return True
        if b["art"] == "wippe" and sorted((z["ia"], z["ch"]) for z in a["ziele"]) != \
                sorted((z["ia"], z["ch"]) for z in b["ziele"]):
            return True
        if b["art"] == "wippe" and bool(a.get("ha_ziele")) != bool(b.get("ha_ziele")):
            return True  # Antwort der Bridge (Server-SN → GO 1) kommt dazu oder fällt weg
        if b["art"] == "tasten" and any(t.get("antwort_ga") is None for t in a["tasten"].values()):
            return True
    return False


def neue_tasten_vorbereiten(hub: Hub, bel: dict[int, dict[str, Any]]) -> None:
    """Einzeltasten ohne Antwort-GA/Code (bisher Wippe oder frei): wie der Server vergeben."""
    belegt = {s.ga for s in hub.szenen.szenen} | {s.ga_status for s in hub.szenen.szenen if s.ga_status}
    belegt |= {t["antwort_ga"] for g in hub.anlage["geraete"] for t in g.get("tasten", []) if t.get("antwort_ga")}
    belegt |= {t["antwort_ga"] for b in bel.values() if b["art"] == "tasten"
               for t in b["tasten"].values() if t.get("antwort_ga")}
    belegt |= {b["antwort_ga"] for b in bel.values() if b["art"] == "wippe" and b.get("antwort_ga")}
    ga = ANTWORT_GA_AB
    for n in sorted(bel):
        b = bel[n]
        if b["art"] == "wippe" and b.get("ha_ziele") and b.get("antwort_ga") is None:
            while ga in belegt:
                ga += 1
            b["antwort_ga"] = ga
            belegt.add(ga)
        if b["art"] != "tasten":
            continue
        for k in (2 * n - 1, 2 * n):
            t = bel[n]["tasten"].setdefault(k, {})
            if t.get("antwort_ga") is None:
                while ga in belegt:
                    ga += 1
                t["antwort_ga"], t["code"] = ga, tasten_code(k)
                belegt.add(ga)


def server_tasten_setzen(hub: Hub, geraet: dict[str, Any], aktionen: dict[int, str]) -> None:
    """Server-Tasten dieses Senders in der Bridge-Konfiguration neu setzen (Taste k → Aktion); eNet-Szenen
    werden als Bridge-Szene mit einem Szenenaufruf angelegt. Nicht mehr genutzte Aufruf-Szenen fallen weg."""
    esp = hub.anlage.setdefault("esp", {})
    szenen = esp.setdefault("szenen", {})
    tasten = [t for t in esp.get("server_tasten", []) if t["sender"] != geraet["sn"]]
    for k, wert in sorted(aktionen.items()):
        taste = {"name": tr("taste_name", name=geraet["name"], k=k), "sender": geraet["sn"], "ga": taste_ga(geraet, k)}
        s = None
        if wert.startswith("ha:"):  # HA-Szene: eNet-Teil ruft die Bridge selbst auf, den Rest schaltet HA
            taste["ha"] = wert[3:]
            s = hub.szenen.ha_szene(wert[3:])
            if s is not None and (not s.werte or s.ha_id in hub.ha_szenen.in_arbeit):
                s = None  # kein (fertiger) eNet-Teil: Taste nur melden, HA schaltet die Szene
        elif wert.startswith("enet:"):
            s = hub.szenen.get(wert[5:])
            if s is None:
                continue
        if s is not None:
            name = next((n for n, a in szenen.items() if _szenen_aufruf(a) == (s.ga, s.nr)), None)
            if name is None:
                name = s.name
                while name in szenen:
                    name += "'"
                szenen[name] = [{"ia": "0.0.0", "handle": s.ga, "wert": f"{s.nr:02x}"}]
            taste["szene"] = name
        elif wert.startswith("esp:") and wert[4:] in szenen:
            taste["szene"] = wert[4:]
        elif "ha" not in taste:
            continue  # nur melden: die Bridge beantwortet jede Server-Taste, HA bekommt das Ereignis
        tasten.append(taste)
    esp["server_tasten"] = tasten
    genutzt = {t.get("szene") for t in tasten}
    for n in [n for n, a in szenen.items() if n not in genutzt and _szenen_aufruf(a)]:
        del szenen[n]


def ha_ziele_uebernehmen(geraet: dict[str, Any], bel: dict[int, dict[str, Any]]) -> None:
    """HA-Lampen der Wippen in die Anlage übernehmen, ohne den Sender zu programmieren (muss_programmieren:
    nein – die Wippe hatte schon HA-Lampen, also die Antwort der Bridge)."""
    for t in geraet.get("tasten", []):
        b = bel.get((t["nr"] + 1) // 2)
        if t["art"] == "wippe" and b and b["art"] == "wippe" and b.get("ha_ziele") and t.get("ha_ziele"):
            t["ha_ziele"] = list(b["ha_ziele"])


async def belegung_aendern(hub: Hub, geraet: dict[str, Any], bel: dict[int, dict[str, Any]],
                           aktionen: dict[int, str]) -> bool:
    """Neue Belegung übernehmen: Sender programmieren (falls nötig, Benutzer muss eine Taste drücken), dann
    Server-Tasten in der Bridge. Liefert, ob programmiert wurde."""
    programmiert = muss_programmieren(geraet, bel)
    if programmiert:
        neue_tasten_vorbereiten(hub, bel)
        await programmieren(hub, geraet, bel, alle_aktoren=False)
        vormerkungen_loeschen(geraet)
    else:
        ha_ziele_uebernehmen(geraet, bel)
    server_tasten_setzen(hub, geraet, aktionen)
    await hub.anlage_speichern()
    await hub._config_abgleichen(hub.bridge.info)
    return programmiert


# --- Geräteseite: Betriebsart und Lampen vormerken, Aktion sofort, Knopf „Sender programmieren“ ---
BETRIEBSARTEN = ("wippe", "tasten")  # Wippe ohne Lampen = nicht benutzt (im Sender dasselbe)
LAMPEN_SEITE = 2  # mehr Lampen je Wippe nur im Dialog „Sender belegen“; dann auf der Geräteseite gesperrt
VORMERKUNGEN = ("geplant", "geplant_lampen")


def vorgemerkt(geraet: dict[str, Any]) -> bool:
    """Gibt es Änderungen, die erst mit „Sender programmieren“ wirksam werden?"""
    return any(geraet.get(k) for k in VORMERKUNGEN)


def vormerkungen_loeschen(geraet: dict[str, Any]) -> None:
    for k in VORMERKUNGEN:
        geraet.pop(k, None)


def betriebsart(geraet: dict[str, Any], n: int) -> str:
    """Vorgemerkte, sonst programmierte Betriebsart der Wippe n (unbelegt = Wippe ohne Lampen)."""
    if str(n) in geraet.get("geplant", {}):
        art = geraet["geplant"][str(n)]
        return art if art in BETRIEBSARTEN else "wippe"  # früher vorgemerktes „frei“
    a = belegung(geraet).get(n)
    return "tasten" if a is not None and a["art"] == "tasten" else "wippe"


def geplante_belegung(geraet: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Belegung mit den vorgemerkten Betriebsarten (Lampen einer bleibenden Wippe bleiben)."""
    alt = belegung(geraet)
    bel = dict(alt)
    for nr, art in geraet.get("geplant", {}).items():
        n, a = int(nr), alt.get(int(nr))
        if art == "tasten":
            bel[n] = a if a and a["art"] == "tasten" else {"art": "tasten", "tasten": {}}
        elif a is not None:  # Wippe: Lampen bleiben; aus Einzeltasten → Wippe ohne Lampen
            bel[n] = a if a["art"] == "wippe" else {"art": "wippe", "ziele": []}
    for nr, keys in geraet.get("geplant_lampen", {}).items():
        n = int(nr)
        if n not in bel or bel[n]["art"] == "wippe":
            ziele, ha = lampen_trennen(keys)
            vorher = bel.get(n) or {}
            bel[n] = {"art": "wippe", "ziele": ziele, "ha_ziele": ha, "antwort_ga": vorher.get("antwort_ga")}
    return bel


def ist_ha_lampe(key: str) -> bool:
    """Lampen-Schlüssel: eNet-Kanal „ia/ch“, HA-Entität „light.x“ / „switch.x“."""
    return "/" not in key


def lampen_trennen(keys: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Lampen-Schlüssel → (eNet-Ziele [{ia, ch}], HA-Entitäten)."""
    ziele = [{"ia": k.split("/")[0], "ch": int(k.split("/")[1])} for k in keys if not ist_ha_lampe(k)]
    return ziele, [k for k in keys if ist_ha_lampe(k)]


def lampen_der_wippe(geraet: dict[str, Any], n: int) -> list[str]:
    """Vorgemerkte, sonst programmierte Lampen der Wippe n: eNet-Kanäle „ia/ch“, dann HA-Entitäten."""
    b = geplante_belegung(geraet).get(n)
    if not b or b["art"] != "wippe":
        return []
    return [f"{z['ia']}/{z['ch']}" for z in b["ziele"]] + list(b.get("ha_ziele", []))


def lampen_auswahlen(geraet: dict[str, Any], n: int) -> int:
    """Anzahl „Lampe i“-Auswahlen der Wippe n: Lampe 2 erst, wenn Lampe 1 belegt ist."""
    if betriebsart(geraet, n) != "wippe":
        return 0
    return min(len(lampen_der_wippe(geraet, n)) + 1, LAMPEN_SEITE)


def komplex(geraet: dict[str, Any], n: int) -> bool:
    """Wippe mit mehr Lampen, als die Geräteseite zeigt: nur im Dialog „Sender belegen“ ändern."""
    return len(lampen_der_wippe(geraet, n)) > LAMPEN_SEITE


def lampen_vormerken(geraet: dict[str, Any], n: int, keys: list[str]) -> None:
    """Lampen der Wippe n vormerken (gleich dem programmierten Stand: Vormerkung weg). Ändern sich nur die
    HA-Lampen und bleibt die Wippe dabei mit bzw. ohne HA-Lampen, gilt das sofort – der Sender bleibt gleich."""
    keys = list(dict.fromkeys(keys))
    geplant = geraet.setdefault("geplant_lampen", {})
    geplant.pop(str(n), None)
    ziele, ha = lampen_trennen(keys)
    eintrag = next((t for t in geraet.get("tasten", []) if t["art"] == "wippe" and (t["nr"] + 1) // 2 == n), None)
    if eintrag is not None and str(n) not in geraet.get("geplant", {}) and bool(ha) == bool(eintrag.get("ha_ziele")) \
            and sorted((z["ia"], z["ch"]) for z in ziele) == sorted((z["ia"], z["ch"]) for z in eintrag["ziele"]):
        if ha:
            eintrag["ha_ziele"] = ha
    elif sorted(lampen_der_wippe(geraet, n)) != sorted(keys):
        geplant[str(n)] = keys
    if not geplant:
        geraet.pop("geplant_lampen")


def aktionen_des_senders(hub: Hub, geraet: dict[str, Any]) -> dict[int, str]:
    """Aktion je Einzeltaste (auch vorgemerkter) dieses Senders."""
    return {k: aktion_der_taste(hub, geraet, k) for n, b in geplante_belegung(geraet).items()
            if b["art"] == "tasten" for k in (2 * n - 1, 2 * n)}


async def aktion_setzen(hub: Hub, geraet: dict[str, Any], k: int, wert: str) -> None:
    """Aktion einer Einzeltaste ändern – nur die Bridge-Konfiguration, der Sender bleibt unverändert."""
    aktionen = aktionen_des_senders(hub, geraet)
    aktionen[k] = wert
    server_tasten_setzen(hub, geraet, aktionen)
    await hub.anlage_speichern()
    await hub._config_abgleichen(hub.bridge.info)


async def geplant_programmieren(hub: Hub, geraet: dict[str, Any]) -> None:
    """Knopf „Sender programmieren“: vorgemerkte Betriebsarten (und vorhandene Belegung) schreiben."""
    aktionen = aktionen_des_senders(hub, geraet)
    bel = geplante_belegung(geraet)
    reparatur = not vorgemerkt(geraet)  # nichts geändert: alles prüfen, auch die Aktoren
    neue_tasten_vorbereiten(hub, bel)
    await programmieren(hub, geraet, bel, alle_aktoren=reparatur)
    vormerkungen_loeschen(geraet)
    server_tasten_setzen(hub, geraet, aktionen)  # GA-Basis kann sich geändert haben, Wippen fallen weg
    await hub.anlage_speichern()
    await hub._config_abgleichen(hub.bridge.info)

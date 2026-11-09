"""Aktoren ohne eNet Server einlernen und entfernen (über die Bridge: send + monitor).

Ablauf wie der eNet Server (NOTES.md „Einlernen“, erprobt mit tools/einlernen.py 2026-10-26):
  1. Suche unter Server-SN an GA 0: DoA_Read 0x3E1 + DoA_SelRead 0x3E3 → Gerät im Lernmodus: DoA_Resp 0x3E2
     mit seiner Domäne. Aufgenommen wird nur ein Gerät im Werkszustand (Domäne 0); Geräte mit Domäne – auch
     Sender im Lernmodus antworten so (Handsender gemessen 2026-10-29) – werden übergangen und gemeldet.
  2. Produktnummer per SN (NetParamRead 0x3DA, Obj 0 PID 0xD2) → Gerätetyp; unbekannt = Abbruch vor dem Ändern
  3. Auswahl NetParamWrite 0x3E4 PID 71, Domäne 0x3EE + Kontrolle 0x3EC, Adresse 0x3DE + Kontrolle 0x3DC
  4. GA-Basis aus dem Werkseintrag der Link-Tabelle (GO 1)
Kalibrierung nebenbei: Ist die Bridge noch nicht kalibriert, probiert die Suche reihum die Frequenzkorrekturen
durch (ein Gerät im Lernmodus antwortet auch ohne Anlage); die Antworten des Geräts gleichen danach fein ab.
Entfernen: A_Restart 0x381 `02 00` (Werkszustand) an die Adresse, Antwort 0x3A1 (Sender vorher wecken).
Name und Belegung entfernter Geräte bleiben in anlage["entfernt"]; derselbe Sender übernimmt sie beim
Wieder-Einlernen (danach „Sender programmieren“, das auch die Aktor-Seite an die neue GA-Basis anpasst).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .bridge import BridgeError
from .iex import CHANNEL_TYPES, DEVICE_TYPES, PRODUKT_TYPEN, ia_str, produkt_kennung
from .texte import tr

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

SRV_IA = 0x7FFF  # 7.15.255
SUCHE_S = 120
RUNDE_S = 3.0  # Suchrunde wie der Server (~3 s)


def frame(addr_hex: str, doa: bool, group: bool, src: int, dst: int, apci: int, data: bytes) -> str:
    """Telegramm ohne CRCs (wie knxrf::buildTelegram, tools/enet.py); die Bridge setzt die LFN."""
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


class Funk:
    """Rohe Telegramme über die Bridge: senden (send) und empfangene mitlesen (monitor)."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.rx: asyncio.Queue[bytes] = asyncio.Queue()
        self._ab = None

    async def __aenter__(self) -> Funk:
        def hoeren(msg: dict[str, Any]) -> None:
            if msg.get("ev") == "telegram" and msg.get("dir") == "rx":
                self.rx.put_nowait(bytes.fromhex(msg["data"]))

        self._ab = self.hub.bridge.add_listener(hoeren)
        await self.hub.bridge.command("monitor", on=True)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._ab()
        try:
            await self.hub.bridge.command("monitor", on=False)
        except BridgeError:
            pass

    async def senden(self, f: str) -> None:
        await self.hub.bridge.command("send", frame=f)

    async def warten(self, passt, timeout: float) -> bytes | None:
        ende = time.monotonic() + timeout
        while (rest := ende - time.monotonic()) > 0:
            try:
                d = await asyncio.wait_for(self.rx.get(), rest)
            except asyncio.TimeoutError:
                return None
            if len(d) >= 18 and passt(d):
                return d
        return None

    async def anfrage(self, f: str, passt, timeout: float = 0.5, versuche: int = 4) -> bytes | None:
        for _ in range(versuche):
            await self.senden(f)
            if (r := await self.warten(passt, timeout)) is not None:
                return r
        return None


def _typ_fuer(hub: Hub, produkt: str) -> str | None:
    """Gerätetyp zur Produktnummer: aus den Katalog-Definitionen (alle Typen), sonst aus der eigenen Tabelle."""
    kennung = produkt_kennung(produkt)
    katalog = {produkt_kennung(d.get("produkt") or ""): typ for typ, d in hub.definitionen.items()}
    if typ := katalog.get(kennung) or PRODUKT_TYPEN.get(kennung):
        return typ
    # Letzte Ziffer = Hersteller (2 Gira, 3 Jung, z. B. WS3BG 521302 / WS3BJ 521303). Geräte, die beide gleich
    # bauen, führt der Katalog nur unter Gira: Jung-Sonnensensor meldet 521233, Katalog DVT_SF1S 521232.
    gira = kennung[:5] + "2"
    return katalog.get(gira) or PRODUKT_TYPEN.get(gira)


def _typ_daten(hub: Hub, typ: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Beschreibung, Rolle und Aktor-Kanäle eines Typs – aus dem Katalog (Kanaltyp, GO-Offset, Objekt),
    sonst aus der eigenen Tabelle. Kanaltypen ohne Umsetzung (z. B. Energiesensor) bleiben außen vor."""
    d = hub.definitionen.get(typ)
    if d is None:
        desc, rolle, kanaele = DEVICE_TYPES[typ]
        return desc, rolle, [{"nr": i + 1, "art": a, "offset": o} for i, (a, o) in enumerate(kanaele)]
    arten = [(k, CHANNEL_TYPES.get(k["kanaltyp"])) for k in d["kanaele"]]
    kanaele = [{"nr": k["nr"], "art": art, "offset": k["go_offset"], "obj": k["obj"]}
               for k, art in arten if art in ("schalten", "dimmen", "jalousie")]
    rolle = "aktor" if kanaele else ("sender" if any(a in ("wippe", "taste") for _, a in arten) else "unbekannt")
    desc = DEVICE_TYPES.get(typ, (d.get("produktname") or d.get("name") or typ,))[0]
    return desc, rolle, kanaele


def _freie_ia(hub: Hub) -> str:
    belegt = {ia_int(g["ia"]) for g in hub.anlage["geraete"]}
    n = 1
    while n in belegt:
        n += 1
    return ia_str(n)


async def einlernen(hub: Hub, name: str | None = None) -> dict[str, Any]:
    """Gerät im Lernmodus suchen, aufnehmen und in die Anlage eintragen. Liefert den Geräte-Eintrag."""
    from .hub import KAL_KANDIDATEN

    srv, dom = hub.anlage["server"]["sn"], hub.anlage["server"]["domain"]
    kalibriert = bool(hub.radio.get("calibrated"))
    start = hub.radio.get("freq_offset", KAL_KANDIDATEN[0])
    kandidaten = [start] if kalibriert else list(dict.fromkeys((start, *KAL_KANDIDATEN)))

    def srv_gruppe(ap: int, data: bytes) -> str:
        return frame(srv, False, True, SRV_IA, 0, ap, data)

    def dom_gruppe(ap: int, data: bytes) -> str:
        return frame(dom, True, True, SRV_IA, 0, ap, data)

    hub.aktivitaet(tr("akt_einlernen_start"))
    async with hub._kal_lock, Funk(hub) as funk:
        # 1. Suche (bei unkalibrierter Bridge reihum mit allen Frequenzkorrekturen)
        sn, runde, offset = None, 0, start
        uebergangen: dict[str, str] = {}  # SN -> Grund (Gerät hat schon eine Domäne)
        ende = time.monotonic() + SUCHE_S
        while sn is None and time.monotonic() < ende:
            f = kandidaten[runde % len(kandidaten)]
            if f != offset:
                await hub._radio(freq_offset=f)
                offset = f
            runde += 1
            await funk.senden(srv_gruppe(0x3E1, b""))
            await asyncio.sleep(0.5)
            await funk.senden(srv_gruppe(0x3E3, bytes.fromhex("02000000000000ffffffffffff00")))
            r = await funk.warten(lambda d: apci(d) == 0x3E2 and not d[15] & 1, RUNDE_S - 0.5)
            if r is None:
                continue
            gefunden, domaene = r[4:10].hex(), r[18:24].hex()
            if domaene == "000000000000":  # Werkszustand: aufnehmen
                sn = gefunden
            elif gefunden not in uebergangen:  # auch Sender im Lernmodus antworten, mit ihrer Domäne
                bekannt = next((g for g in hub.anlage["geraete"] if g["sn"] == gefunden), None)
                uebergangen[gefunden] = (tr("schon_in_anlage", name=bekannt["name"], ia=bekannt["ia"])
                                         if bekannt else tr("andere_anlage", domaene=domaene))
                _LOGGER.info("Pairing: %s skipped: %s", gefunden, uebergangen[gefunden])
        if sn is None:
            if offset != start:
                await hub._radio(freq_offset=start)
            text = tr("nichts_gefunden")
            if uebergangen:
                text += tr("uebergangen", liste="; ".join(f"{k} {v}" for k, v in uebergangen.items()))
            raise BridgeError("timeout", text)
        snb = bytes.fromhex(sn)
        _LOGGER.info("Pairing: device %s found (frequency correction %d)", sn, offset)

        # 2. Produktnummer → Typ (vor jeder Änderung am Gerät)
        r = await funk.anfrage(srv_gruppe(0x3DA, bytes.fromhex("c35143") + snb + bytes.fromhex("0000d20001")),
                               lambda d: apci(d) == 0x3DB and d[4:10] == snb)
        produkt = r[21:].hex() if r is not None else None
        typ = _typ_fuer(hub, produkt) if produkt else None
        if typ is None:
            # für „Diagnosedaten herunterladen“: Gerätetypen, die wir noch nicht kennen, bei Fehlerberichten sehen
            hub.unbekannte_geraete[sn] = {"produkt": produkt, "zeit": datetime.now().isoformat(timespec="seconds")}
            raise BridgeError("unknown_type", tr("typ_unbekannt", sn=sn, produkt=produkt))

        # 3. Auswahl, Domäne, Adresse
        # am Gerät zurückgesetzt, aber noch in der Anlage: bisherige Adresse behalten
        bestehend = next((g for g in hub.anlage["geraete"] if g["sn"] == sn), None)
        ia_s = bestehend["ia"] if bestehend else _freie_ia(hub)
        ia = ia_int(ia_s)
        await funk.anfrage(srv_gruppe(0x3E4, bytes.fromhex("c35147") + snb),
                           lambda d: apci(d) == 0x3DB and d[18:21] == bytes.fromhex("c35147"))
        await funk.senden(srv_gruppe(0x3EE, snb + bytes.fromhex(dom)))
        await asyncio.sleep(0.15)
        r = await funk.anfrage(srv_gruppe(0x3EC, snb), lambda d: apci(d) == 0x3ED and d[18:24] == snb)
        if r is None or r[24:30].hex() != dom:
            raise BridgeError("learn", tr("domaene_nicht_gesetzt", wert=r[24:30].hex() if r is not None else tr("keine_antwort")))
        await funk.senden(dom_gruppe(0x3DE, snb + ia.to_bytes(2, "big") + b"\0\0\0\0"))
        await asyncio.sleep(0.15)
        r = await funk.anfrage(dom_gruppe(0x3DC, snb), lambda d: apci(d) == 0x3DD and d[18:24] == snb)
        if r is None or (r[11] << 8 | r[12]) != ia:
            raise BridgeError("learn", tr("adresse_unbestaetigt"))

        # 4. GA-Basis (Werkseintrag GO 1)
        r = await funk.anfrage(frame(dom, True, False, SRV_IA, ia, 0x2C8, bytes([3, 0x3F, 0, 0, 1, 0])),
                               lambda d: apci(d) == 0x2C9 and (d[11] << 8 | d[12]) == ia)
        basis = r[28] if r is not None and len(r) >= 32 and r[20] == 0 else 0

        # Feinabgleich an den Antworten des Geräts
        est = await hub._radio()
        if est.get("freqest_n", 0) >= 3:
            neu = max(-128, min(127, offset + round(est["freqest"])))
            if neu != offset or not kalibriert:
                await hub._radio(freq_offset=neu, save=True)
                _LOGGER.info("Pairing: frequency correction %d saved", neu)
        elif not kalibriert:
            await hub._radio(freq_offset=offset, save=True)

    desc, rolle, kanaele = _typ_daten(hub, typ)
    geraet = {"name": name or f"{desc} {ia_s}", "ia": ia_s, "sn": sn, "typ": typ, "beschreibung": desc,
              "rolle": rolle, "kanaele": kanaele,
              "ga_basis": basis, "ga_basis_geprueft": True, "produkt": produkt,
              "eingelernt": time.strftime("%Y-%m-%d %H:%M")}
    frueher = hub.anlage.get("entfernt", {}).pop(sn, None)  # dasselbe Gerät schon einmal in der Anlage
    if bestehend is not None:  # Werksreset am Gerät: Eintrag ersetzen, Name/Belegung/alte Basis übernehmen
        hub.anlage["geraete"].remove(bestehend)
        frueher = {k: bestehend[k] for k in ("name", "tasten", "zentral", "ga_basis", "raum") if k in bestehend}
    if frueher and not name:
        geraet["name"] = frueher["name"]
    if frueher and "raum" in frueher:
        geraet["raum"] = frueher["raum"]
    if rolle == "sender":  # Belegung: wie vor dem Entfernen, sonst alle Wippen frei (danach programmieren)
        from .sender import standard_belegung, tasten_aus
        geraet["tasten"] = (frueher or {}).get("tasten") or tasten_aus(geraet, standard_belegung(geraet))
        geraet["zentral"] = (frueher or {}).get("zentral", [])
        if frueher and "ga_basis" in frueher:  # alte Basis behalten, bis „Sender programmieren“ die
            geraet["ga_basis"] = frueher["ga_basis"]  # Aktor-Verknüpfungen auf die neue umgetragen hat
            geraet["ga_basis_geprueft"] = False
    hub.anlage["geraete"].append(geraet)
    await hub.anlage_speichern()
    hub.aktivitaet(tr("akt_eingelernt", name=geraet["name"], typ=desc, ia=ia_s))
    return geraet


def auswirkungen(hub: Hub, geraet: dict[str, Any]) -> dict[str, list[str]]:
    """Was beim Entfernen mitbetroffen ist (für die Rückfrage im Dialog)."""
    ia = geraet["ia"]
    szenen = [s.name for s in hub.szenen.szenen if any(m.startswith(f"{ia}/") for m in s.mitglieder)]
    esp_szenen = [n for n, aktionen in hub.anlage.get("esp", {}).get("szenen", {}).items()
                  if any(a["ia"] == ia for a in aktionen)]
    verweise, aktoren, tasten = [], [], []
    if geraet.get("rolle") == "sender":
        for t in geraet.get("tasten", []):
            for z in t.get("ziele", []):
                a = next((g for g in hub.anlage["geraete"] if g["ia"] == z["ia"]), None)
                aktoren.append(tr("verweis_aktor_wippe", aktor=a["name"] if a else z["ia"], ch=z["ch"], n=(t["nr"] + 1) // 2))
        tasten = [t.get("name") or str(t["ga"]) for t in hub.anlage.get("esp", {}).get("server_tasten", [])
                  if t["sender"] == geraet["sn"]]
    else:
        for g in hub.anlage["geraete"]:
            for t in g.get("tasten", []):
                if any(z["ia"] == ia for z in t.get("ziele", [])):
                    verweise.append(tr("verweis_wippe", name=g["name"], n=(t["nr"] + 1) // 2))
            if any(z["ia"] == ia for z in g.get("zentral", [])):
                verweise.append(tr("verweis_zentral", name=g["name"]))
    return {"szenen": szenen, "esp_szenen": esp_szenen, "verweise": verweise, "aktoren": aktoren, "tasten": tasten}


async def entfernen(hub: Hub, geraet: dict[str, Any]) -> dict[str, Any]:
    """Gerät in den Werkszustand versetzen, aus der Anlage nehmen und aufräumen: Szenen, ESP-Szenen und Verweise
    anderer Sender (Aktor) bzw. Aktor-Verknüpfungen und Server-Tasten (Sender).
    Liefert {"bestaetigt": Gerät hat den Reset bestätigt, "offen": nicht erledigte Punkte}."""
    from .sender import Sender, _aktor_links, belegung

    dom, ia = hub.anlage["server"]["domain"], ia_int(geraet["ia"])
    offen: list[str] = []
    async with Funk(hub) as funk:
        if geraet.get("rolle") == "sender":  # Sender schlafen: erst wecken (Taste drücken)
            hub.aktivitaet(tr("akt_entfernen_wecken", name=geraet["name"]))
            await Sender(hub, geraet, funk).wecken()
        r = await funk.anfrage(frame(dom, True, False, SRV_IA, ia, 0x381, b"\x02\x00"),
                               lambda d: apci(d) == 0x3A1 and (d[11] << 8 | d[12]) == ia, timeout=0.6, versuche=6)
    if r is None:
        offen.append(tr("reset_unbestaetigt"))
    esp = hub.anlage.setdefault("esp", {})
    if geraet.get("rolle") == "sender":
        try:  # Aktor-Seite der Wippen: alle Einträge dieses Senders löschen
            await _aktor_links(hub, geraet, belegung(geraet), {}, 0)
        except BridgeError as err:
            offen.append(tr("links_nicht_geloescht", err=err))
        esp["server_tasten"] = [t for t in esp.get("server_tasten", []) if t["sender"] != geraet["sn"]]
    else:
        for g in hub.anlage["geraete"]:  # Verweise anderer Sender (Links im Sender bleiben bis zum Programmieren)
            for t in g.get("tasten", []):
                if "ziele" in t:
                    t["ziele"] = [z for z in t["ziele"] if z["ia"] != geraet["ia"]]
            if "zentral" in g:
                g["zentral"] = [z for z in g["zentral"] if z["ia"] != geraet["ia"]]
        for n in list(esp.get("szenen", {})):
            esp["szenen"][n] = [a for a in esp["szenen"][n] if a["ia"] != geraet["ia"]]
    hub.anlage["geraete"].remove(geraet)
    # merken: wird dasselbe Gerät wieder eingelernt, übernimmt es Name und Belegung
    hub.anlage.setdefault("entfernt", {})[geraet["sn"]] = {k: geraet[k] for k in ("name", "tasten", "zentral", "ga_basis")
                                                          if k in geraet}
    for s in hub.anlage.get("szenen", []):
        s["mitglieder"] = [m for m in s.get("mitglieder", []) if m["ia"] != geraet["ia"]]
    await hub.anlage_speichern()
    await hub.szenen.mitglied_entfernen(geraet["ia"])
    try:
        await hub._config_abgleichen(hub.bridge.info)
    except BridgeError as err:
        offen.append(tr("config_nicht_uebertragen", err=err))
    hub.aktivitaet(tr("akt_entfernt_offen", name=geraet["name"], offen="; ".join(offen)) if offen
                   else tr("akt_entfernt", name=geraet["name"]))
    return {"bestaetigt": r is not None, "offen": offen}


async def aufnehmen(hub: Hub, name: str | None = None, raum: str | None = None) -> dict[str, Any]:
    """Einlernen komplett (Dienst und „+ Gerät hinzufügen“): suchen, aufnehmen, Raum setzen; ein Sender wird gleich
    programmiert (im Lernmodus wach) und entlassen – seine Lern-LED hört auf zu blinken = eingelernt."""
    bekannt = {x["sn"] for x in hub.anlage["geraete"]}
    g = await einlernen(hub, name)
    if g["rolle"] == "aktor" and g["sn"] in bekannt:
        # Werksreset eines Aktors der Anlage: Szenen-Links und -Werte im Gerät sind weg – zurückschreiben
        hub.aktivitaet(tr("akt_szenen_wiederherstellen", name=g["name"]))
        fehler = await hub.pruefung.aktor_wiederherstellen(g["ia"])
        if fehler:
            _LOGGER.warning("Restoring scenes of %s: %s", g["name"], "; ".join(fehler))
    if raum:
        g["raum"] = raum  # Vorschlag für den HA-Bereich beim Anlegen des Geräts
        await hub.anlage_speichern()
    if g["rolle"] == "sender":
        from .sender import programmieren

        await programmieren(hub, g, nach_einlernen=True)
    return g


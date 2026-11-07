"""Geräteparameter: Definitionen (aus tools/katalog.py), Umrechnung, Lesen und Schreiben per Funk.

Parameterspeicher eines Kanals = Kanal-Objekt PID 79 (0x4F), Element = Byte (NOTES.md „Gerätekatalog“).
Schreiben wie der eNet Server (Mitschnitt 2026-10-23, NOTES.md „Parameter übertragen“).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .bridge import Bridge, BridgeError
from .const import DOMAIN
from .texte import deutsch, tr

_LOGGER = logging.getLogger(__name__)

PID_PARAM = 79
MAX_BLOCK = 15  # A_PropertyValue: Anzahl 4 Bit

# Eigene Bezeichnungen (App-Begriffe, Deutsch/Englisch) und ob die Entität standardmäßig aktiv ist. Parameter ohne
# Eintrag heißen wie im Katalog (englisch).
NAMEN: dict[str, tuple[str, str, bool]] = {
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
    "REPEATER_MODE_ENABLE": ("Repeater", "Repeater", False),
    "LEARN_MODE_ENABLE": ("Lernmodus", "Learn mode", False),
    "SUMSTATE_MAX_TIMEOUT": ("Statusmeldung Wartezeit", "Status message timeout", False),
    # Sonnensensor (DVT_SF1S, Kanaltyp CT_1F18); Kanal-Vorsatz „Sonnenschutz“/„Dämmerung“ aus KANAL_NAMEN
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
# Kanalnamen je Gerätetyp (Vorsatz der Parameter-Entitäten bei Geräten ohne eigene Kanal-Geräte)
KANAL_NAMEN: dict[str, dict[int, tuple[str, str]]] = {
    "DVT_SF1S": {1: ("Sonnenschutz", "Sun protection"), 2: ("Dämmerung", "Twilight")}}
AUSWAHL: dict[str, tuple[str, str]] = {
    "OFF": ("Aus", "Off"), "ON": ("Ein", "On"), "NO_CHANGE": ("Keine Änderung", "No change"),
    "VALUE_BEFORE_LOCKING": ("Wert vor Sperre", "Value before lock"),
    "LAST_VALUE_BEFORE_POWER_DOWN": ("Wert vor Netzausfall", "Value before power failure"),
    "VALUE_ACCORDING_TO_ADDITIONAL_PARAMETER": ("Helligkeit laut Parameter", "Brightness per parameter"),
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
ZAEHLER = {"SWITCH_COUNTER": ("Schaltspiele", "Switching cycles"),
           "ACTIVE_COUNTER_RESETABLE": ("Betriebsstunden (rücksetzbar)", "Operating hours (resettable)"),
           "ACTIVE_COUNTER": ("Betriebsstunden", "Operating hours")}  # Reihenfolge: RESETABLE vor ACTIVE_COUNTER


def _sprache(paar: tuple[str, str]) -> str:
    return paar[0] if deutsch() else paar[1]


def auswahl(wert: str) -> str:
    """Anzeige eines Auswahlwerts (Katalog-Kennung) in der eingestellten Sprache."""
    return _sprache(AUSWAHL[wert]) if wert in AUSWAHL else wert


def kanal_name(typ: str, kanal: int | None) -> str | None:
    """Kanal-Vorsatz für Parameter-Entitäten (z. B. Sonnensensor: „Sonnenschutz“), sonst None."""
    paar = KANAL_NAMEN.get(typ, {}).get(kanal) if kanal is not None else None
    return _sprache(paar) if paar else None


# Szenenspeicher und Sonderfunktionen gehören zur Szenenbehandlung, nicht zu den Einstellungen
AUSGESCHLOSSEN = ("SCV1[", "SNA[", "PSN[", "CHANNEL_CONFIG")


@dataclass
class Param:
    """Ein Parameter eines Geräts/Kanals mit Funk-Adresse und Wertebeschreibung."""

    d: dict[str, Any]

    @property
    def id(self) -> str:
        return self.d["id"]

    @property
    def key(self) -> str:
        return f"{self.d['obj']}/{self.d['pid']}/{self.d['start']}"

    @property
    def typ(self) -> dict[str, Any]:
        return self.d["typ"]

    @property
    def art(self) -> str:
        return self.typ.get("art", "")

    @property
    def nur_lesen(self) -> bool:
        return self.d.get("schreiben", 0) == 0 or self.d.get("schreibgeschuetzt", False)

    @property
    def name(self) -> str:
        for kennung, paar in ZAEHLER.items():
            if kennung in self.id:
                return _sprache(paar)
        if self.id in NAMEN:
            de, en, _ = NAMEN[self.id]
            return de if deutsch() else en
        return self.d.get("name") or self.id

    @property
    def standard_aktiv(self) -> bool:
        return NAMEN.get(self.id, ("", "", False))[2]

    @property
    def laenge(self) -> int:
        """Bytes im Gerät: PID 79 = 1 Byte je Element, sonst Elementgröße aus der Bitlänge."""
        if self.d["pid"] == PID_PARAM:
            return self.d["anzahl"]
        return self.d["anzahl"] * max(1, self.typ.get("bits", 8) // 8)

    @property
    def nutzbar(self) -> bool:
        return (self.d.get("aktiv", True) and self.typ.get("kodierung") not in (None, "unbekannt")
                and not self.id.startswith(AUSGESCHLOSSEN))

    # --- Umrechnung --------------------------------------------------------
    def dekodiere(self, raw: bytes) -> Any:
        kod = self.typ.get("kodierung")
        if kod == "dpt9":
            v = dpt9_dekodiere(raw[:2])
        elif kod == "prozent255":
            v = round(raw[0] * 100 / 255)
        else:
            v = int.from_bytes(raw[: self.laenge], "big")
        if self.art == "bool":
            return v != 0
        if self.art == "auswahl":
            for k, n in self.typ.get("werte", {}).items():
                if n == v:
                    return k
            return None
        return v * self.typ.get("faktor", 1)

    def kodiere(self, wert: Any) -> bytes:
        kod = self.typ.get("kodierung")
        if self.art == "bool":
            n = 1 if wert else 0
        elif self.art == "auswahl":
            n = self.typ["werte"][wert]
        elif kod == "dpt9":
            return dpt9_kodiere(float(wert))
        elif kod == "prozent255":
            return bytes([min(255, math.floor(float(wert) * 255 / 100))])
        else:
            n = int(round(float(wert) / self.typ.get("faktor", 1)))
        return n.to_bytes(self.laenge, "big")


def dpt9_dekodiere(b: bytes) -> float:
    """KNX 2-Byte-Gleitkomma: Wert = 0,01 · M · 2^E (M 12 Bit Zweierkomplement mit Vorzeichen in Bit 15)."""
    raw = b[0] << 8 | b[1]
    exp = (raw >> 11) & 0x0F
    mant = raw & 0x07FF
    if raw & 0x8000:
        mant -= 2048
    return round(0.01 * mant * (1 << exp), 2)


def dpt9_kodiere(v: float) -> bytes:
    m = int(round(v * 100))
    exp = 0
    while not -2048 <= m <= 2047:
        m = int(round(m / 2))  # wie der Server: Rundung bei großen Werten (2000 s → 3e1a)
        exp += 1
    raw = (exp << 11) | (m & 0x07FF) | (0x8000 if m < 0 else 0)
    return raw.to_bytes(2, "big")


def lade_definitionen(pfad: str) -> dict[str, Any]:
    """definitionen.json lesen (Executor); fehlt sie, leer (die Integration bringt eine mit)."""
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f).get("typen", {})
    except FileNotFoundError:
        return {}


def bloecke(params: list[Param]) -> list[tuple[int, int, int, int]]:
    """Lesezugriffe (obj, pid, start, anzahl): PID 79 zu Blöcken ≤ 15 Byte zusammengefasst, sonst einzeln."""
    out: list[tuple[int, int, int, int]] = []
    p79 = sorted((p for p in params if p.d["pid"] == PID_PARAM), key=lambda p: (p.d["obj"], p.d["start"]))
    for p in p79:
        o, s, n = p.d["obj"], p.d["start"], p.laenge
        if out and out[-1][0] == o and out[-1][1] == PID_PARAM and s + n - out[-1][2] <= MAX_BLOCK \
                and s >= out[-1][2]:
            lo = out[-1]
            out[-1] = (o, PID_PARAM, lo[2], max(lo[3], s + n - lo[2]))
        else:
            out.append((o, PID_PARAM, s, n))
    out += [(p.d["obj"], p.d["pid"], p.d["start"], p.d["anzahl"]) for p in params if p.d["pid"] != PID_PARAM]
    return out


class ParamSpeicher:
    """Zwischenspeicher der Rohwerte je Gerät (SN → {key: hex}) mit Lesen/Schreiben über die Bridge."""

    def __init__(self, hass: HomeAssistant, entry_id: str, bridge: Bridge, signal: str) -> None:
        self.hass = hass
        self.bridge = bridge
        self.signal = signal
        self._store: Store = Store(hass, 1, f"{DOMAIN}_parameter_{entry_id}")
        self.werte: dict[str, dict[str, str]] = {}
        self.lock = asyncio.Lock()
        self._programmierung: dict[str, asyncio.Lock] = {}
        # Nach dem Rahmen startet der Aktor neu und schaltet seine Ausgänge ab (Test 2026-10-30): der Hub merkt
        # sich vorher die Zustände seiner Kanäle und stellt sie danach wieder her
        self.zustaende_merken: Callable[[str], Any] = lambda ia: None
        # Anzeige „wird programmiert“ und Aktivität des Aktors: (ia, aktiv, zweck, fehler)
        self.rahmen_meldung: Callable[[str, bool, str, str | None], None] = lambda ia, aktiv, zweck, fehler: None
        self.zustaende_wiederherstellen: Callable[[str, Any], Awaitable[None]] | None = None

    def programmierung(self, ia: str) -> asyncio.Lock:
        """Gesperrt, solange ein Aktor programmiert wird: im Lade-Status quittiert er Schaltbefehle, führt sie
        aber nicht aus – Schaltbefehle an diesen Aktor warten deshalb darauf."""
        return self._programmierung.setdefault(ia, asyncio.Lock())

    async def laden(self) -> None:
        self.werte = (await self._store.async_load()) or {}

    def roh(self, sn: str, p: Param) -> bytes | None:
        h = self.werte.get(sn, {}).get(p.key)
        return bytes.fromhex(h) if h is not None else None

    def wert(self, sn: str, p: Param) -> Any:
        raw = self.roh(sn, p)
        return p.dekodiere(raw) if raw is not None and len(raw) >= p.laenge else None

    async def lesen(self, ia: str, sn: str, params: list[Param]) -> int:
        """Alle Parameter eines Geräts lesen; liefert die Anzahl gelesener Parameter."""
        async with self.lock:
            werte = self.werte.setdefault(sn, {})
            n, last = 0, None
            offen = bloecke(params)
            for _ in range(3):  # fehlgeschlagene Blöcke am Ende erneut (schwache Funkstrecke)
                fehler = []
                for blk in offen:
                    try:
                        n += self._uebernehmen(werte, params, blk, await self.bridge.request(
                            "prop_read", ia=ia, obj=blk[0], pid=blk[1], start=blk[2], count=blk[3]))
                    except BridgeError as err:
                        fehler.append(blk)
                        last = err
                offen = fehler
                if not offen:
                    break
            if offen:
                _LOGGER.warning("%s: %d of %d reads without answer (%s)", ia, len(offen),
                                len(bloecke(params)), last)
            werte["_gelesen"] = datetime.now().isoformat(timespec="seconds")
            await self._store.async_save(self.werte)
        async_dispatcher_send(self.hass, self.signal, sn)
        return n

    @staticmethod
    def _uebernehmen(werte: dict, params: list[Param], blk: tuple, r: dict) -> int:
        obj, pid, start, _ = blk
        n = 0
        data = bytes.fromhex(r.get("value", ""))
        for p in params:
            if p.d["obj"] != obj or p.d["pid"] != pid:
                continue
            if pid == PID_PARAM:
                off = p.d["start"] - start
                if 0 <= off and off + p.laenge <= len(data):
                    werte[p.key] = data[off: off + p.laenge].hex()
                    n += 1
            elif p.d["start"] == start:
                werte[p.key] = data.hex()
                n += 1
        return n

    def fehlend(self, sn: str, params: list[Param]) -> list[Param]:
        return [p for p in params if p.key not in self.werte.get(sn, {})]

    async def _pw(self, ia: str, obj: int, pid: int, data: bytes, start: int = 1, count: int = 1) -> None:
        await self.bridge.request("prop_write", ia=ia, obj=obj, pid=pid, start=start, count=count, data=data.hex())

    async def _rahmen(self, ia: str, geraet_obj: int, obj: int, schritte, parameter: bool, zweck: str) -> None:
        """Programmier-Rahmen wie der eNet Server (Lade-Status, Zeitstempel); Sperren hält der Aufrufer.
        parameter: zusätzlich Vorbereitung und Prüfsumme des Parameterspeichers (Mitschnitt Parameter),
        sonst nur der Rahmen wie beim Verknüpfen (Mitschnitt Links). zweck: für die Aktivität des Aktors."""
        vorher = self.zustaende_merken(ia)
        self.rahmen_meldung(ia, True, zweck, None)
        fehler: str | None = None
        try:
            await self._pw(ia, 1, 6, bytes([2]) + bytes(9))  # Lade-Status: Laden beginnt
        except BridgeError as err:
            self.rahmen_meldung(ia, False, zweck, str(err))
            raise
        try:
            await self._rahmen_inhalt(ia, geraet_obj, obj, schritte, parameter)
        except BridgeError as err:
            fehler = str(err)
            # Rahmen trotzdem schließen: sonst bleibt der Aktor im Laden (danach 00 = nicht geladen) und führt
            # keine Schaltbefehle mehr aus (Indirekte 2026-10-30 nach nack beim Link-Abgleich)
            with contextlib.suppress(BridgeError):
                await self._pw(ia, 1, 6, bytes([1]) + bytes(9))
            raise
        finally:
            if self.zustaende_wiederherstellen and vorher:
                await self.zustaende_wiederherstellen(ia, vorher)
            self.rahmen_meldung(ia, False, zweck, fehler)

    async def _rahmen_inhalt(self, ia: str, geraet_obj: int, obj: int, schritte, parameter: bool) -> None:
        await self._pw(ia, geraet_obj, 0x39, bytes.fromhex("000000000003"))
        if parameter:
            await self._pw(ia, geraet_obj, 0x3A, b"\x00")
            await self._pw(ia, geraet_obj, 0x41, b"\x01")
            await self._pw(ia, geraet_obj, 0x42, b"\x07")
        await schritte()
        if parameter:
            await self.bridge.request("prop_read", ia=ia, obj=obj, pid=0xD1, start=1, count=1)  # Prüfsumme
            await self._pw(ia, obj, 0xD0, b"\x04")
        await self._pw(ia, obj, 0xD2, b"\x04")
        t = datetime.now()
        await self._pw(ia, geraet_obj, 0x39, bytes([t.year % 100, t.month, t.day, t.hour, t.minute, t.second]))
        for _ in range(3):
            await self.bridge.request("np_write", ia=ia, data="00123c00")
        await self._pw(ia, 1, 6, bytes([1]) + bytes(9))  # Lade-Status: geladen
        await asyncio.sleep(1)
        r = await self.bridge.request("prop_read", ia=ia, obj=1, pid=6, start=1, count=1)
        if not r.get("value", "").startswith("01"):
            _LOGGER.warning("%s: load state after programming %s instead of 01", ia, r.get("value"))

    async def programmieren(self, ia: str, geraet_obj: int, obj: int, schritte, zweck: str | None = None) -> None:
        """Beliebige Schritte (z. B. Links anlegen/löschen) im Programmier-Rahmen ausführen."""
        async with self.lock, self.programmierung(ia):
            await self._rahmen(ia, geraet_obj, obj, schritte, parameter=False,
                               zweck=zweck or tr("zweck_verknuepfungen_kurz"))

    async def roh_schreiben(self, ia: str, geraet_obj: int, obj: int, start: int, data: bytes,
                            zweck: str | None = None) -> None:
        """Bytes in den Parameterspeicher (PID 79) schreiben und zurücklesen – z. B. Szenenwerte SCV1[Nr],
        die keine Einstellungs-Entität haben (Schreibtest 2026-10-30, NOTES „Szenenwerte schreiben“)."""
        async with self.lock, self.programmierung(ia):
            async def wert_schreiben() -> None:
                await self._pw(ia, obj, PID_PARAM, data, start, len(data))

            await self._rahmen(ia, geraet_obj, obj, wert_schreiben, parameter=True,
                               zweck=zweck or tr("zweck_parameterspeicher"))
            r = await self.bridge.request("prop_read", ia=ia, obj=obj, pid=PID_PARAM, start=start, count=len(data))
        gelesen = bytes.fromhex(r.get("value", ""))[: len(data)]
        if gelesen != data:
            raise BridgeError("verify_failed", tr("verify_fehler", name=f"{ia} obj {obj} @{start}", neu=data.hex(), gelesen=gelesen.hex()))

    async def schreiben(self, ia: str, sn: str, geraet_obj: int, p: Param, wert: Any) -> None:
        """Einen Parameter schreiben, Ablauf wie der eNet Server (Lade-Status, Prüfsumme, Zeitstempel)."""
        neu = p.kodiere(wert)
        obj = p.d["obj"]
        async with self.lock, self.programmierung(ia):
            if p.d["pid"] != PID_PARAM:  # Geräte-/Kanaleigenschaft: direkt
                await self._pw(ia, obj, p.d["pid"], neu, p.d["start"], p.d["anzahl"])
            else:
                async def wert_schreiben() -> None:
                    await self._pw(ia, obj, PID_PARAM, neu, p.d["start"], len(neu))

                await self._rahmen(ia, geraet_obj, obj, wert_schreiben, parameter=True, zweck=tr("zweck_einstellung", name=p.name))
            # Kontrolle: Wert zurücklesen
            r = await self.bridge.request("prop_read", ia=ia, obj=obj, pid=p.d["pid"], start=p.d["start"],
                                          count=p.d["anzahl"] if p.d["pid"] != PID_PARAM else p.laenge)
            gelesen = bytes.fromhex(r.get("value", ""))[: p.laenge]
            self.werte.setdefault(sn, {})[p.key] = gelesen.hex()
            await self._store.async_save(self.werte)
        async_dispatcher_send(self.hass, self.signal, sn)
        if gelesen != neu:
            raise BridgeError("verify_failed", tr("verify_fehler", name=p.name, neu=neu.hex(), gelesen=gelesen.hex()))

"""Messgeräte ohne Lampe: Energiesensor (CT_1F19), Bewegungsmelder (CT_1F1B), Repeater (CT_1F06).

Aus dem Katalog umgesetzt, am Gerät noch ungetestet (Geräte in HA als „ungetestet“ gekennzeichnet). Annahmen:
- Die Geräte senden ihre Werte unter eigener SN an GA = Basis·256 + Kanaloffset + GO (wie Aktoren und der
  Sonnensensor); verglichen wird nur das untere GA-Byte. Die Bridge meldet sie als key-Ereignis „wert1/wert/wert4“
  (1, 2 bzw. 4 Byte).
- Der Energiesensor ist netzbetrieben und beantwortet Abfragen seiner Gruppenobjekte wie ein Aktor (Bridge-Befehl
  „read“, Handle = Offset + GO): HA fragt die Werte regelmäßig ab, falls er sie nicht selbst sendet.
- Bewegungsmelder: „Timed Start“ (GO 3, nachtriggern GO 4, DPT 9.010 Sekunden) schaltet die verknüpften Aktoren für
  diese Zeit ein → Bewegung erkannt, aus nach Ablauf ohne erneutes Auslösen. Helligkeit GO 7 (DPT 9.004).
- Repeater: nur erreichbar oder nicht (seltene Abfrage, Signal und letzte Meldung).
Telegramme, die hier nicht zugeordnet werden können, landen in `unbekannt` (Diagnose-Download).
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
from .params import dpt9_dekodiere

_LOGGER = logging.getLogger(__name__)

ART_ENERGIE, ART_BEWEGUNG, ART_REPEATER = "energie", "bewegung", "repeater"
MESS_ARTEN = (ART_ENERGIE, ART_BEWEGUNG, ART_REPEATER)
# Gerätetypen, deren Abbildung nur aus dem Katalog stammt (Anzeige „ungetestet“ am Gerät)
UNGETESTET = {"DVT_ES1M", "DVT_ES1Z", "DVT_ES4R", "DVT_BS1BP", "DVT_RPZS",
              "DVT_JA1M", "DVT_JA1R", "DVT_SJA1", "DVT_SJAR"}

ABFRAGE_S = 60  # Energiesensor: so oft Spannung, Strom, Wirkleistung und Energie abfragen
SELTEN = 5  # Blind-/Scheinleistung nur jede n-te Runde
REPEATER_RUNDEN = 10  # Repeater: jede n-te Runde ansprechen
BEWEGUNG_MIN_S = 10  # Bewegung frühestens so lange nach dem letzten Auslösen wieder aus


def _dpt14(b: bytes) -> float:
    return round(struct.unpack(">f", b)[0], 3)


def _dpt13(b: bytes) -> int:
    return struct.unpack(">i", b)[0]


@dataclass(frozen=True)
class Wert:
    key: str  # Übersetzungsschlüssel der Entität und Teil der unique_id
    laenge: int  # Byte
    dekodiere: Callable[[bytes], float]
    faktor: float = 1.0
    selten: bool = False  # Energiesensor: nur jede SELTEN-te Runde abfragen


# GO → Wert je Kanalart (Katalog ChannelTypes.xml, fw2.21)
WERTE: dict[str, dict[int, Wert]] = {
    ART_ENERGIE: {
        3: Wert("spannung", 2, dpt9_dekodiere, 0.001),  # DPT 9.020 mV → V
        4: Wert("strom", 2, dpt9_dekodiere, 0.001),  # DPT 9.021 mA → A
        5: Wert("wirkleistung", 4, _dpt14),  # DPT 14.056 W
        6: Wert("blindleistung", 4, _dpt14, selten=True),
        7: Wert("scheinleistung", 4, _dpt14, selten=True),
        8: Wert("energie", 4, _dpt13, 0.001),  # DPT 13.010 Wh → kWh
    },
    ART_BEWEGUNG: {
        3: Wert("ausloesung", 2, dpt9_dekodiere),  # DPT 9.010 s
        4: Wert("nachtriggern", 2, dpt9_dekodiere),
        7: Wert("helligkeit", 2, dpt9_dekodiere),  # DPT 9.004 lx
    },
    ART_REPEATER: {},
}
SPANNE = {ART_ENERGIE: 9, ART_BEWEGUNG: 8, ART_REPEATER: 1}  # höchstes GO je Kanal
LAENGE = {"wert1": 1, "wert": 2, "wert4": 4}


def ist_messgeraet(geraet: dict[str, Any]) -> bool:
    return geraet.get("rolle") == "sensor" and any(k["art"] in MESS_ARTEN for k in geraet.get("kanaele", []))


class Messung:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.werte: dict[str, float] = {}  # „SN/Kanal/key“ → Wert
        self.bewegung: dict[str, bool] = {}  # „SN/Kanal“ → Bewegung erkannt
        self.unbekannt: list[str] = []  # nicht zugeordnete Telegramme (Diagnose, die letzten 20)
        self._aus: dict[str, Callable[[], None]] = {}
        self._task: asyncio.Task | None = None

    def geraete(self) -> list[dict[str, Any]]:
        return [g for g in self.hub.anlage.get("geraete", []) if ist_messgeraet(g)]

    def kanaele(self, art: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return [(g, k) for g in self.geraete() for k in g["kanaele"]
                if k["art"] in MESS_ARTEN and (art is None or k["art"] == art)]

    def start(self) -> None:
        if self.kanaele(ART_ENERGIE) or self.kanaele(ART_REPEATER):
            self._task = self.hub.hass.async_create_background_task(self._abfragen(), "enet_bridge Messwerte")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for aus in self._aus.values():
            aus()
        self._aus.clear()

    def _merken(self, text: str) -> None:
        self.unbekannt = (self.unbekannt + [f"{dt_util.now().isoformat(timespec='seconds')} {text}"])[-20:]

    def _setzen(self, sn: str, nr: int, key: str, wert: float) -> None:
        self.werte[f"{sn}/{nr}/{key}"] = wert
        async_dispatcher_send(self.hub.hass, self.hub.signal(f"messwert_{sn}_{nr}"))

    def _diag(self, g: dict[str, Any], rssi: Any) -> None:
        from .hub import Diagnose  # zirkulären Import vermeiden

        d = self.hub.diag.setdefault(g["ia"], Diagnose())
        d.zeit = dt_util.now()
        if rssi is not None:
            d.rssi = rssi
        async_dispatcher_send(self.hub.hass, self.hub.signal(f"diag_{g['ia']}"))

    @callback
    def telegramm(self, msg: dict[str, Any]) -> bool:
        """key-Ereignis eines Messgeräts auswerten. Liefert True, wenn der Absender ein Messgerät ist."""
        sn = msg.get("sn")
        g = next((g for g in self.geraete() if g["sn"] == sn), None)
        if g is None:
            return False
        self._diag(g, msg.get("rssi"))
        kind, ga = msg.get("kind"), int(msg.get("ga", -1))
        if kind not in LAENGE or not msg.get("cmd"):
            _LOGGER.debug("%s: %s telegram to GA %d not evaluated", g["name"], kind, ga)  # z. B. SumstatusGet
            self._merken(f"{g['name']} {kind} GA {ga} {msg.get('cmd')}")
            return True
        daten = bytes.fromhex(msg["cmd"])[-LAENGE[kind]:]
        for k in g["kanaele"]:
            go = (ga & 0xFF) - k["offset"]
            if k["art"] in MESS_ARTEN and 1 <= go <= SPANNE[k["art"]]:
                w = WERTE[k["art"]].get(go)
                if w is None or w.laenge != len(daten):
                    break
                self._wert(g, k, w, daten)
                return True
        _LOGGER.info("%s: value telegram not assigned yet (GA %d, %s %s)", g["name"], ga, kind, msg["cmd"])
        self._merken(f"{g['name']} {kind} GA {ga} {msg['cmd']}")
        return True

    def _wert(self, g: dict[str, Any], k: dict[str, Any], w: Wert, daten: bytes) -> None:
        wert = round(w.dekodiere(daten) * w.faktor, 3)
        if k["art"] == ART_BEWEGUNG and w.key in ("ausloesung", "nachtriggern"):
            self._bewegung(g["sn"], k["nr"], wert)
            return
        self._setzen(g["sn"], k["nr"], w.key, wert)

    def _bewegung(self, sn: str, nr: int, dauer_s: float) -> None:
        key = f"{sn}/{nr}"
        if aus := self._aus.pop(key, None):
            aus()

        @callback
        def _ende(_now: Any) -> None:
            self._aus.pop(key, None)
            self.bewegung[key] = False
            async_dispatcher_send(self.hub.hass, self.hub.signal(f"bewegung_{sn}_{nr}"))

        self.bewegung[key] = True
        self._aus[key] = async_call_later(self.hub.hass, max(BEWEGUNG_MIN_S, dauer_s), _ende)
        async_dispatcher_send(self.hub.hass, self.hub.signal(f"bewegung_{sn}_{nr}"))

    def _darf_senden(self) -> bool:
        h = self.hub
        return (h.bridge.connected and not h.flasht and not h.keine_bridge and not h.mithoeren
                and bool(h.radio.get("ok")))

    async def _lesen(self, g: dict[str, Any], handle: int) -> bytes | None:
        try:
            r = await self.hub.bridge.request("read", ia=g["ia"], handle=handle)
        except BridgeError as err:
            _LOGGER.debug("%s: read handle %d: %s", g["name"], handle, err)
            if err.err == "nack":  # abgelehnt, aber geantwortet: erreichbar
                self._diag(g, None)
            return None
        return bytes.fromhex(r.get("value", ""))

    async def _abfragen(self) -> None:
        runde = 0
        while True:
            await asyncio.sleep(ABFRAGE_S)
            if not self._darf_senden():
                continue
            for g, k in self.kanaele(ART_ENERGIE):
                for go, w in WERTE[ART_ENERGIE].items():
                    if w.selten and runde % SELTEN:
                        continue
                    daten = await self._lesen(g, k["offset"] + go)
                    if daten and len(daten) >= w.laenge:
                        self._wert(g, k, w, daten[-w.laenge:])
            if runde % REPEATER_RUNDEN == 0:
                for g, k in self.kanaele(ART_REPEATER):
                    await self._lesen(g, k["offset"] + 1)  # Antwort (auch Ablehnung) zählt als „erreichbar“
            runde += 1

    def diagnose(self) -> dict[str, Any]:
        return {"werte": dict(self.werte), "bewegung": dict(self.bewegung), "unbekannt": list(self.unbekannt),
                "stand": time.strftime("%H:%M:%S")}

"""Anlage (Geräteverzeichnis) + Bridge: Zustandstabelle, Tastenzuordnung, Resync nach ESP-Start.

Inbetriebnahme ohne Eingaben: Beim Verbinden gleicht HA die Server-Konfiguration der Bridge (CRC) mit
anlage.json ab und überträgt sie bei Abweichung. Eine noch nicht kalibrierte Bridge (oder eine, bei der
kein Aktor antwortet) sucht per Statusabfragen ihre Frequenzkorrektur und speichert sie im NVS.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.logbook import async_log_entry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import esp_config, firmware
from .bridge import Bridge, BridgeError, fremdes_funkmodul
from .const import ART_DIMMEN, ART_SCHALTEN, ART_TASTE, ART_WIPPE, DOMAIN, SENSOR_TYPEN
from .anlage import AnlageSpeicher
from .params import Param, ParamSpeicher, dpt9_dekodiere
from .ha_szenen import HaSzenen
from .ha_wippe import HaWippe
from .pruefung import Pruefung
from .szenen import SzenenVerwaltung
from .texte import deutsch, tr

_LOGGER = logging.getLogger(__name__)
BATTERIE_WARNUNG_S = 3600  # Abstand der Log-Warnungen „Battery low“ je Gerät

OSV_HALTEN_MS = 4000  # beide Wippentasten so lange: Dimmer speichert die Einschalthelligkeit
SYNC_DELAY_S = 3.0  # nach dem Öffnen: ESP startet unter Linux neu, start-Ereignis abwarten
WIEDERHOLUNG_S = 2.0  # gleiches Tastentelegramm (SN, GA, Tastenwert) innerhalb dieser Zeit: Wiederholung
REAL_SERVER_S = 300  # „echter Server aktiv“ so lange nach seinem letzten Telegramm (Bridge meldet ≤ 1×/min)
HELLO_VERSUCHE = 3  # so oft hello, bevor der Port als „keine Bridge“ gilt

# Frequenzkorrektur (FSCTRL0, ~1,59 kHz je Schritt): Suchreihenfolge, falls kein Aktor antwortet.
# Abstand 30 (~48 kHz) liegt innerhalb des Fangbereichs; 60 = bisher gebaute Platinen/Module.
KAL_KANDIDATEN = (60, 0, 30, -30, 90, -60, 120, -90, -120)
KAL_PROBEN = 3       # Feinabgleich: mindestens so viele gemittelte Telegramme
KAL_TESTKANAELE = 2  # Grobsuche: je Kandidat höchstens so viele Geräte abfragen
KAL_PASSIV_S = 300   # „Nur mithören“: so lange je Frequenzkorrektur auf fremde Telegramme warten
NACHFUEHREN_S = 600         # so oft die mittlere Abweichung der empfangenen Telegramme prüfen
NACHFUEHREN_PROBEN = 20     # erst ab so vielen Telegrammen seit der letzten Änderung nachstellen
NACHFUEHREN_SCHWELLE = 2    # ab so vielen FSCTRL0-Schritten (~1,59 kHz) Abweichung nachstellen
NACHFUEHREN_ABSTAND_S = 3600  # höchstens so oft im NVS speichern

PID_LINKS = 63  # Link-Tabelle im Kanal-Objekt

# GAs je Tastenart relativ zum Offset (docs/NOTES.md „Sender-Programmierung“)
GA_SPAN = {ART_TASTE: 6, ART_WIPPE: 3}


def wippe_als_server_taste(msg: dict[str, Any]) -> None:
    """Wippe mit HA-Lampen fragt die Server-SN: die Firmware meldet das wie eine Server-Taste (Drücken =
    Bit 0x4000). Bei der Wippe ist das Richtungsbit 0x4000/0x8000, die Haltedauer steht in den unteren 14 Bit."""
    cmd = int(msg.get("cmd", "0") or "0", 16)
    msg["richtung"] = "rechts" if cmd & 0xC000 == 0x8000 else "links"
    msg["hold_ms"] = (cmd & 0x3FFF) * 10
    msg["press"] = cmd & 0x3FFF == 0
    msg.pop("code", None)


def sensor_aktion_key(kanal: int, ein: bool) -> str:
    """Schlüssel in geraet["sensor_aktionen"]: „1_ein“ Sonnenschutz ein … „2_aus“ Dämmerung aus."""
    return f"{kanal}_{'ein' if ein else 'aus'}"


@dataclass
class Kanal:
    """Aktor-Kanal aus anlage.json."""

    geraet: dict[str, Any]
    nr: int
    art: str

    @property
    def ia(self) -> str:
        return self.geraet["ia"]

    @property
    def key(self) -> str:
        return f"{self.ia}/{self.nr}"

    @property
    def obj(self) -> int:
        """Kanal-Objekt (Link-Tabelle, Parameter); Export-Feld „obj“, sonst Regel 2 + Kanal."""
        k = next((k for k in self.geraet.get("kanaele", []) if k["nr"] == self.nr), {})
        return k.get("obj") or 2 + self.nr


@dataclass
class Taste:
    """Taste oder Wippe eines Senders aus anlage.json."""

    geraet: dict[str, Any]
    nr: int
    art: str
    offset: int

    @property
    def key(self) -> str:
        return f"{self.geraet['sn']}/{self.nr}"


@dataclass
class ParamGruppe:
    """Parameter eines Aktors: Kanal-Parameter (kanal = Nr.) oder Geräte-Parameter (kanal = None)."""

    geraet: dict[str, Any]
    kanal: int | None
    params: list[Param]
    geraet_obj: int


@dataclass
class Diagnose:
    rssi: float | None = None
    zeit: Any = None  # datetime der letzten Meldung
    versuche: int | None = None


@dataclass
class Zustand:
    on: bool = False
    percent: int = 0
    known: bool = False
    gemeldet: float = 0.0  # time.monotonic() der letzten Meldung, die keine Antwort auf eine Statusabfrage war


@dataclass
class Hub:
    hass: HomeAssistant
    entry_id: str
    bridge: Bridge
    anlage: dict[str, Any]
    kanaele: list[Kanal] = field(default_factory=list)
    tasten: list[Taste] = field(default_factory=list)
    zustand: dict[str, Zustand] = field(default_factory=dict)
    definitionen: dict[str, Any] = field(default_factory=dict)
    speicher: AnlageSpeicher | None = None
    real_server_zeit: Any = None  # datetime der letzten Meldung „echter Server“ (nur Anzeige, schaltet nichts)
    keine_bridge: bool = False  # am Port antwortet nichts auf hello (leerer ESP32, fremdes Gerät)
    flasht: bool = False
    fw_bundle: str | None = None  # Version der mitgelieferten Firmware (firmware.mitgeliefert)
    _auto_update: set = field(default_factory=set)  # (Bridge-ID, alte, neue Version): je Start nur einmal
    bridge_device_id: str | None = None
    geraet_device_id: dict[str, str] = field(default_factory=dict)
    _sync_task: asyncio.Task | None = None
    _kal_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _nachfuehren_task: asyncio.Task | None = None
    _nachgefuehrt: float = 0.0  # time.monotonic() der letzten Nachführung
    _unsub: Any = None

    def __post_init__(self) -> None:
        for g in self.anlage.get("geraete", []):
            for k in g.get("kanaele", []):
                if k["art"] in (ART_SCHALTEN, ART_DIMMEN):
                    self.kanaele.append(Kanal(g, k["nr"], k["art"]))
            for t in g.get("tasten", []):
                if t["art"] in GA_SPAN:
                    self.tasten.append(Taste(g, t["nr"], t["art"], t["offset"]))
        self.zustand = {k.key: Zustand() for k in self.kanaele}
        self.diag: dict[str, Diagnose] = {}
        self.helligkeit: dict[str, float] = {}  # Sonnensensor-SN → Helligkeit in Lux (Wert-Telegramm, DPT 9.004)
        self.sensor_aktiv: dict[str, bool] = {}  # „SN/Kanal“ → Sonnenschutz bzw. Dämmerung aktiv (Szenentelegramm)
        self._letzte_sensorszene: tuple[Any, float] = (None, 0.0)
        self.batterie_schwach: dict[str, bool] = {}  # Sender-SN → Batterie schwach (aus dem RF-Info-Bit, ab FW 0.8.8)
        self._batterie_gewarnt: dict[str, float] = {}  # Sender-SN → time.monotonic() der letzten Log-Warnung
        self.unbekannte_geraete: dict[str, dict[str, Any]] = {}  # SN → Produktnummer beim Einlernen (Diagnose)
        self.param_gruppen: list[ParamGruppe] = []
        for g in self.anlage.get("geraete", []):
            d = self.definitionen.get(g.get("typ", ""))
            sensor = g.get("typ") in SENSOR_TYPEN  # Sonnensensor: Kanäle ohne Lampe, Parameter trotzdem
            if not d or (g.get("rolle") != "aktor" and not sensor):
                continue
            gp = [Param(p) for p in d.get("geraet", [])]
            if gp := [p for p in gp if p.nutzbar]:
                self.param_gruppen.append(ParamGruppe(g, None, gp, d["geraet_obj"]))
            for k in d.get("kanaele", []):
                kp = [p for p in map(Param, k["parameter"]) if p.nutzbar]
                if kp and (sensor or any(c.geraet is g and c.nr == k["nr"] for c in self.kanaele)):
                    self.param_gruppen.append(ParamGruppe(g, k["nr"], kp, d["geraet_obj"]))
        self.params = ParamSpeicher(self.hass, self.entry_id, self.bridge, self.signal("params"))
        self.params.zustaende_merken = self._zustaende_merken
        self.params.zustaende_wiederherstellen = self._zustaende_wiederherstellen
        self.params.rahmen_meldung = self._rahmen_meldung
        self.programmiert: set[str] = set()  # IAs, deren Aktor gerade im Programmier-Rahmen ist
        self.sender_aktiv: set[str] = set()  # SNs der Sender, die gerade (gewartet oder laufend) programmiert werden
        self.szene_in_arbeit: str | None = None  # HA-Szene, deren eNet-Teil gerade übertragen wird
        self.szenen = SzenenVerwaltung(self)
        self.ha_szenen = HaSzenen(self)
        self.ha_wippe = HaWippe(self)
        self.pruefung = Pruefung(self)
        self._letzte_taste: tuple[Any, float] = (None, 0.0)
        self.bridge.nur_hoeren = self.mithoeren

    # Signale an die Entitäten
    def signal(self, name: str) -> str:
        return f"{DOMAIN}_{self.entry_id}_{name}"

    @property
    def server_on(self) -> bool:
        return bool(self.bridge.info.get("server", {}).get("on"))

    @property
    def server_soll(self) -> bool:
        """Gewünschter Server-Schalter (HA verwaltet ihn und setzt ihn auf jeder Bridge durch)."""
        return bool(self.anlage["server"].get("funktionen", True))

    @property
    def mithoeren(self) -> bool:
        """„Nur mithören“: die Bridge empfängt nur (Tasten als Ereignisse, Zustände aus Meldungen der Aktoren),
        sendet nichts – zum Betrieb neben dem echten eNet Server vor dem Umzug. Server-Funktionen sind dann aus."""
        return bool(self.anlage["server"].get("mithoeren", False))

    async def mithoeren_setzen(self, on: bool) -> None:
        self.anlage["server"]["mithoeren"] = on
        self.bridge.nur_hoeren = on
        await self.anlage_speichern()
        _LOGGER.info("Bridge: listen only %s", "on" if on else "off")
        self.pruefung.szenen_pruefen()  # Hinweis „Szenen nach dem Mithören“ ↔ „nicht umgewandelt“ sofort umstellen
        async_dispatcher_send(self.hass, self.signal("bridge"))
        if self.bridge.connected:
            await self.server_setzen(self.server_soll)
        self._schedule_sync(0.5)  # aus: Kalibrieren, Abfragen, Szenen-Abgleich nachholen; ein: passiv kalibrieren

    def neu_laden_nach_funk(self) -> None:
        """Eintrag neu laden (Entitäten neu aufbauen), aber erst, wenn keine Funk-Sitzung mehr läuft oder wartet:
        Das Neuladen schließt den seriellen Port, eine laufende Programmierung bräche mit „Bad file descriptor“ ab.
        Der Lock bedient Wartende der Reihe nach, das Neuladen kommt also nach allen schon gestarteten."""
        async def warten_und_laden() -> None:
            async with self.params.lock:
                pass
            if not self.sender_aktiv:  # sonst lädt der letzte Programmierlauf neu
                self.hass.config_entries.async_schedule_reload(self.entry_id)

        self.hass.async_create_background_task(warten_und_laden(), f"{DOMAIN} neu laden nach Funk")

    async def anlage_speichern(self) -> None:
        if self.speicher:
            await self.speicher.speichern(self.anlage)

    async def server_setzen(self, on: bool) -> None:
        """Server-Schalter setzen und merken; wirksam auf der Bridge nur, solange nicht „Nur mithören“ an ist."""
        wirksam = on and not self.mithoeren
        reply = await self.bridge.command("server", on=wirksam)
        self.bridge.info.setdefault("server", {})["on"] = bool(reply.get("on", wirksam))
        if self.server_soll != on:
            self.anlage["server"]["funktionen"] = on
            await self.anlage_speichern()
        async_dispatcher_send(self.hass, self.signal("bridge"))

    @property
    def real_server(self) -> bool:
        z = self.real_server_zeit
        return z is not None and (dt_util.now() - z).total_seconds() < REAL_SERVER_S

    @property
    def radio(self) -> dict[str, Any]:
        return self.bridge.info.get("radio", {})

    def kanal(self, geraet: dict[str, Any], nr: int | None) -> Kanal | None:
        return next((k for k in self.kanaele if k.geraet is geraet and k.nr == nr), None)

    @callback
    def _rahmen_meldung(self, ia: str, aktiv: bool, zweck: str, fehler: str | None) -> None:
        """Anzeige: Sensor „Programmierung“, Symbol der Lampen dieses Aktors und Eintrag in seiner Aktivität
        (am Knopf „Parameter lesen“ der Geräteseite)."""
        (self.programmiert.add if aktiv else self.programmiert.discard)(ia)
        self.programmierung_melden()
        if g := next((g for g in self.anlage["geraete"] if g["ia"] == ia), None):
            text = (tr("akt_wird_programmiert", zweck=zweck) if aktiv else tr("akt_programmiert", zweck=zweck)
                    if fehler is None else tr("akt_programmierung_fehler", zweck=zweck, fehler=fehler))
            entity_id = er.async_get(self.hass).async_get_entity_id("button", DOMAIN, f"{g['sn']}_parameter_lesen")
            async_log_entry(self.hass, g["name"], text, DOMAIN, entity_id)
        for k in self.kanaele:
            if k.ia == ia:
                async_dispatcher_send(self.hass, self.signal(f"state_{k.key}"))

    @callback
    def programmierung_melden(self) -> None:
        """Sensor „Programmierung“ auffrischen und – solange etwas übertragen wird – einen Hinweis oben in den
        Einstellungen zeigen (Eintrag unter Reparaturen, verschwindet von selbst; Wunsch Nutzer 2026-11-02)."""
        async_dispatcher_send(self.hass, self.signal("programmierung"))
        namen = sorted({g["name"] for g in self.anlage["geraete"] if g["ia"] in self.programmiert})
        hinweise = {"szene_wird_uebertragen": {"szene": self.szene_in_arbeit} if self.szene_in_arbeit else None,
                    "aktor_wird_programmiert": {"geraete": ", ".join(namen)}
                    if namen and not self.szene_in_arbeit else None}
        for key, platzhalter in hinweise.items():
            issue_id = f"{key}_{self.entry_id}"
            if platzhalter is None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            else:
                ir.async_create_issue(self.hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
                                      severity=ir.IssueSeverity.WARNING, translation_key=key,
                                      translation_placeholders=platzhalter)

    @callback
    def aktivitaet(self, text: str) -> None:
        """Eintrag in der Aktivität der Bridge (am Sensor „Programmierung“) – für Vorgänge, die zu keinem
        bestehenden Gerät gehören (Einlernen, Entfernen, Szenen-Abgleich)."""
        entity_id = er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, f"{self.entry_id}_programmierung")
        async_log_entry(self.hass, "eNet-Bridge", text, DOMAIN, entity_id)

    def _zustaende_merken(self, ia: str) -> dict[str, tuple[bool, int]]:
        """Bekannte Zustände der Kanäle eines Aktors vor einem Programmier-Rahmen."""
        return {k.key: (z.on, z.percent) for k in self.kanaele if k.ia == ia and (z := self.zustand[k.key]).known}

    async def _zustaende_wiederherstellen(self, ia: str, vorher: dict[str, tuple[bool, int]]) -> None:
        """Nach dem Rahmen sind die Ausgänge aus (Neustart der Anwendung): eingeschaltete Kanäle wieder setzen."""
        for k in self.kanaele:
            if k.key not in vorher or not vorher[k.key][0]:
                continue
            an, prozent = vorher[k.key]
            try:
                if k.art == ART_DIMMEN:
                    await self.bridge.request("dim", ia=k.ia, ch=k.nr, percent=max(1, prozent))
                else:
                    await self.bridge.request("switch", ia=k.ia, ch=k.nr, on=True)
            except BridgeError as err:
                _LOGGER.warning("%s: state not restored after programming: %s", k.key, err)
                continue
            z = self.zustand[k.key]
            z.on, z.percent, z.known = True, prozent, True
            async_dispatcher_send(self.hass, self.signal(f"state_{k.key}"))

    def geraet_obj(self, geraet: dict[str, Any]) -> int:
        """Geräte-Objekt (Zeitstempel beim Programmieren): aus den Gerätedefinitionen, sonst 4."""
        return self.definitionen.get(geraet.get("typ", ""), {}).get("geraet_obj") or 4

    def _geraet_versionen(self, info: dict[str, Any]) -> None:
        """Firmware, Funkchip und Kennung der Bridge am HA-Gerät (Geräteseite: „Firmware: 0.8.9“)."""
        if not getattr(self, "bridge_device_id", None):
            return
        v = self.radio.get("version")
        dr.async_get(self.hass).async_update_device(
            self.bridge_device_id, sw_version=info.get("fw"),
            hw_version=f"CC1101 0x{v:02X}" if isinstance(v, int) and v else None,
            serial_number=info.get("id"))

    def modell(self, geraet: dict[str, Any]) -> str:
        """Gerätebezeichnung für die Anzeige: deutsch aus der Anlage (Katalogname beim Import/Einlernen), sonst der
        englische Produktname aus dem Katalog."""
        if not deutsch():
            if en := self.definitionen.get(geraet.get("typ", ""), {}).get("produktname"):
                return en
        return geraet.get("beschreibung") or geraet.get("typ", "")

    def geraet_params(self, geraet: dict[str, Any]) -> list[ParamGruppe]:
        return [g for g in self.param_gruppen if g.geraet is geraet]

    async def parameter_lesen(self, geraet: dict[str, Any], nur_fehlende: bool = False) -> int:
        params = [p for g in self.geraet_params(geraet) for p in g.params]
        if nur_fehlende:
            params = self.params.fehlend(geraet["sn"], params)
        if not params:
            return 0
        return await self.geweckt(geraet, lambda: self.params.lesen(geraet["ia"], geraet["sn"], params))

    async def geweckt(self, geraet: dict[str, Any], aufgabe):
        """aufgabe() ausführen; ein schlafendes Gerät (Sonnensensor) vorher wecken wie einen Sender: Weckruf
        wiederholen, bis es nach einem Druck auf eine Testtaste antwortet, dabei wach halten, danach entlassen."""
        if geraet.get("typ") not in SENSOR_TYPEN:
            return await aufgabe()
        from .einlernen import Funk
        from .sender import Sender

        def aktivitaet(text: str) -> None:  # Logbuch am Knopf „Parameter lesen“ des Sensors
            eid = er.async_get(self.hass).async_get_entity_id("button", DOMAIN, f"{geraet['sn']}_parameter_lesen")
            async_log_entry(self.hass, geraet["name"], text, DOMAIN, eid)

        aktivitaet(tr("akt_sensor_wecken"))
        async with Funk(self) as funk:
            s = Sender(self, geraet, funk)
            await s.wecken()
            aktivitaet(tr("akt_sensor_wach"))
            halten = self.hass.async_create_background_task(s.wach_halten(), f"{DOMAIN} keep sensor awake")
            try:
                return await aufgabe()
            finally:
                halten.cancel()
                with contextlib.suppress(BridgeError):
                    await s.pw(self.geraet_obj(geraet), 0x48, b"\x03")  # entlassen

    @callback
    def _einschalthelligkeit_pruefen(self, taste: Taste, msg: dict[str, Any]) -> None:
        """Beide Tasten einer Wippe ≥ 4 s: die Dimmer speichern ihre Helligkeit als Einschalthelligkeit (OSV),
        ohne es zu melden (auch der eNet Server liest nicht nach; Mitschnitt 2026-10-30) – also neu lesen."""
        if self.mithoeren or msg.get("press") or int(msg.get("cmd", "0") or "0", 16) & 0xC000 != 0xC000 \
                or int(msg.get("hold_ms", 0)) < OSV_HALTEN_MS:
            return
        eintrag = next((t for t in taste.geraet.get("tasten", []) if t["nr"] == taste.nr), {})
        ziele = {f"{z['ia']}/{z['ch']}" for z in eintrag.get("ziele", [])}
        gruppen = [g for g in self.param_gruppen if g.kanal is not None and f"{g.geraet['ia']}/{g.kanal}" in ziele]
        if not gruppen:
            return

        async def lesen() -> None:
            await asyncio.sleep(2)  # Dimmer blinkt zur Bestätigung
            for g in gruppen:
                params = [p for p in g.params if p.id == "OSV"]
                if params:
                    try:
                        await self.params.lesen(g.geraet["ia"], g.geraet["sn"], params)
                    except BridgeError as err:
                        _LOGGER.warning("%s: switch-on brightness not read: %s", g.geraet["name"], err)

        self.hass.async_create_background_task(lesen(), f"{DOMAIN} Einschalthelligkeit lesen")

    async def _parameter_vorladen(self) -> None:
        """Fehlende Aktor-Parameter lesen (beim ersten Start alle; Neu-Lesen per Taste „Parameter lesen“)."""
        for g in {id(x.geraet): x.geraet for x in self.param_gruppen}.values():
            if g.get("typ") in SENSOR_TYPEN:
                continue  # schläft: nur auf Knopfdruck (mit Wecken)
            if n := await self.parameter_lesen(g, nur_fehlende=True):
                _LOGGER.info("%s: %d parameters read", g["name"], n)

    def taste_fuer(self, sn: str, ga: int, basis: int | None = None) -> Taste | None:
        for t in self.tasten:
            if t.geraet["sn"] != sn:
                continue
            b = t.geraet.get("ga_basis", 0) if basis is None else basis
            rel = ga - b * 256 - t.offset
            if 1 <= rel <= GA_SPAN[t.art]:
                return t
        return None

    def _batterie(self, msg: dict) -> None:
        """Batteriezustand des Senders: steht im RF-Info-Byte jedes Telegramms (wie beim eNet Server)."""
        if (b := msg.get("battery")) not in ("ok", "weak") or not (sn := msg.get("sn")):
            return
        schwach = b == "weak"
        if self.batterie_schwach.get(sn) != schwach:
            # Der Sonnensensor wechselt das Bit bei fast jedem Telegramm → höchstens eine Warnung pro Stunde je Gerät
            if schwach and time.monotonic() - self._batterie_gewarnt.get(sn, -BATTERIE_WARNUNG_S) >= BATTERIE_WARNUNG_S:
                _LOGGER.warning("Battery low: transmitter %s", sn)
                self._batterie_gewarnt[sn] = time.monotonic()
            self.batterie_schwach[sn] = schwach
            async_dispatcher_send(self.hass, self.signal(f"batterie_{sn}"))

    def _sensorwert(self, msg: dict) -> bool:
        """Wert-Telegramm eines Sonnensensors: Helligkeit (2 Byte DPT 9.004, gemessen GA Basis·256 + 4).
        Liefert True, wenn das Telegramm damit erledigt ist."""
        sn = msg.get("sn")
        if not sn or not any(g["sn"] == sn and g.get("typ") in SENSOR_TYPEN for g in self.anlage["geraete"]):
            return False
        if msg.get("kind") == "wert1" and msg.get("cmd"):
            self._sensorszene(sn, int(msg.get("ga", -1)), int(msg["cmd"], 16))
            return True
        if msg.get("kind") != "wert" or not msg.get("cmd"):
            _LOGGER.info("Sun sensor %s: telegram not evaluated yet: %s", sn, msg)
            return True
        lux = round(dpt9_dekodiere(bytes.fromhex(msg["cmd"])), 2)
        if self.helligkeit.get(sn) != lux:
            self.helligkeit[sn] = lux
            async_dispatcher_send(self.hass, self.signal(f"helligkeit_{sn}"))
        return True

    def _sensorszene(self, sn: str, ga: int, wert: int) -> None:
        """Szenentelegramm des Sonnensensors (1 Byte, Szenennummer in Bit 5–0; das erste trägt zusätzlich Bit 6,
        die Wiederholungen nicht). Kanal aus der GA: Basis·256 + 3 = K1 Sonnenschutz, + 7 = K2 Dämmerung
        (GO-Offset 0/4 + 3). Werkseinstellung: K1 Szene 48 Sonnenschutz ein / 49 aus, K2 50 Dämmerung ein / 51 aus
        (Testtasten am Sensor lösen die Ein-Szene aus: K1 → 48, K2 → 50, gemessen 2026-11-06).
        Die Dämmerungsschwelle liegt weit unter der Sonnenschutzschwelle: ein Kanal ein setzt den anderen aus."""
        kanal = {3: 1, 7: 2}.get(ga & 0xFF)
        szene = wert & 0x3F
        if kanal is None:
            _LOGGER.info("Sun sensor %s: scene %d to unknown GA %d", sn, szene, ga)
            return
        kennung, jetzt = (sn, kanal, szene), time.monotonic()
        if self._letzte_sensorszene[0] == kennung and jetzt - self._letzte_sensorszene[1] < WIEDERHOLUNG_S:
            return  # Wiederholungen des Sensors (4× nach dem ersten Telegramm)
        self._letzte_sensorszene = (kennung, jetzt)
        # Szenennummern aus dem Gerät (ASNVE/ASNVU, Kanal-Objekt 3 bzw. 4, PID 79 Byte 6/7; gelesen 2026-11-06:
        # K1 48 über / 49 unter, K2 51 über / 50 unter). Sonnenschutz ein = über, Dämmerung ein = unter der Schwelle.
        werte = self.params.werte.get(sn, {})
        obj = kanal + 2
        ueber, unter = werte.get(f"{obj}/79/6"), werte.get(f"{obj}/79/7")
        if ueber is not None and unter is not None and szene in (int(ueber, 16), int(unter, 16)):
            ein = szene == int(ueber if kanal == 1 else unter, 16)
        else:
            ein = szene % 2 == 0  # Werkseinstellung (48/49, 50/51), solange die Parameter nicht gelesen sind
        self.sensor_aktiv[f"{sn}/{kanal}"] = ein
        if ein:
            self.sensor_aktiv[f"{sn}/{3 - kanal}"] = False
        _LOGGER.debug("Sun sensor %s ch%d: scene %d (%s)", sn, kanal, szene, "ein" if ein else "aus")
        async_dispatcher_send(self.hass, self.signal(f"sensor_aktiv_{sn}"))
        async_dispatcher_send(self.hass, self.signal(f"sensor_szene_{sn}_{kanal}"), {"ein": ein, "szene": szene})
        g = next((g for g in self.anlage["geraete"] if g["sn"] == sn), {})
        aktion = g.get("sensor_aktionen", {}).get(sensor_aktion_key(kanal, ein))
        if aktion and not self.mithoeren:
            self.hass.async_create_background_task(self._sensor_aktion(g, kanal, ein, aktion), "enet Sensoraktion")

    async def _sensor_aktion(self, g: dict, kanal: int, ein: bool, aktion: str) -> None:
        """Gewählte Szene des Sonnensensors aufrufen (Auswahl auf der Geräteseite). HA-Szene über scene.turn_on –
        ihr eNet-Teil geht dabei als Szenentelegramm (HaSzenen); eNet-Szene direkt als Szenentelegramm."""
        art, _, wert = aktion.partition(":")
        name = f"{g.get('name', g['sn'])} K{kanal} {'ein' if ein else 'aus'}"
        try:
            if art == "ha":
                eid = next((st.entity_id for st in self.hass.states.async_all("scene")
                            if str(st.attributes.get("id")) == wert), None)
                if eid is None:
                    _LOGGER.warning("%s: scene %s not found", name, wert)
                    return
                _LOGGER.debug("%s → %s", name, eid)
                await self.hass.services.async_call("scene", "turn_on", {"entity_id": eid}, blocking=True)
            elif art == "enet":
                s = next((s for s in self.szenen.szenen if str(s.id) == wert), None)
                if s is None:
                    _LOGGER.warning("%s: eNet scene %s not found", name, wert)
                    return
                _LOGGER.debug("%s → eNet scene %s", name, s.name)
                await self.szenen.aufrufen(s)
        except Exception as err:  # noqa: BLE001 – Fehler der Szene nur melden, Sensor-Auswertung läuft weiter
            _LOGGER.warning("%s: scene failed: %s", name, err)

    def _sender_basis_lernen(self, sn: str, ga: int) -> Taste | None:
        """Tastendruck passt nicht zur bekannten GA-Basis: Sender schlafen (Link-Tabelle nicht lesbar), also
        die Basis aus dem Telegramm ableiten (GA = Basis·256 + Offset + 1…n), wenn sie noch nicht gesichert ist."""
        t = self.taste_fuer(sn, ga, basis=ga >> 8)
        if t is None or t.geraet.get("ga_basis_geprueft"):
            return None
        t.geraet["ga_basis"], t.geraet["ga_basis_geprueft"] = ga >> 8, True
        _LOGGER.info("%s: GA base %d taken from button press", t.geraet["name"], ga >> 8)
        self.hass.async_create_task(self.anlage_speichern())
        return t

    async def _aktor_basis_lesen(self) -> None:
        """GA-Basis der Aktoren aus dem Werkseintrag der Link-Tabelle (GO 1: Typ 01, eigene SN, GA = Basis·256
        + Offset + 1). Einmal je Gerät; ändert sich etwas, wird die Konfiguration der Bridge neu übertragen."""
        geaendert = False
        for g in self.anlage["geraete"]:
            k = next((k for k in self.kanaele if k.geraet is g), None)
            if k is None or g.get("ga_basis_geprueft"):
                continue
            for idx in range(8):
                try:
                    r = await self.bridge.request("fp_read", ia=g["ia"], obj=k.obj, pid=PID_LINKS,
                                                  data=f"000001{idx:02x}")
                except BridgeError as err:
                    _LOGGER.debug("%s: link table (entry %d): %s", g["name"], idx, err)
                    break  # Ende der Tabelle (rc 255) oder nicht erreichbar → später erneut
                v = bytes.fromhex(r.get("value", ""))
                if len(v) >= 9 and v[0] == 0x01 and v[1:7].hex() == g["sn"]:
                    basis = v[7]
                    if basis != g.get("ga_basis", 0):
                        _LOGGER.warning("%s: GA base %d (installation had %d)", g["name"], basis, g.get("ga_basis", 0))
                        geaendert = True
                    g["ga_basis"], g["ga_basis_geprueft"] = basis, True
                    await self.anlage_speichern()
                    break
        if geaendert:
            await self._config_abgleichen(self.bridge.info)

    # --- Lebenszyklus -------------------------------------------------------
    async def async_start(self) -> None:
        self.fw_bundle = await self.hass.async_add_executor_job(firmware.mitgeliefert)
        await self.params.laden()
        self._unsub = self.bridge.add_listener(self._on_message)
        self.bridge.start()
        self.ha_szenen.start()
        self._nachfuehren_task = self.hass.async_create_background_task(self._nachfuehren(),
                                                                        f"{DOMAIN} frequency tracking")

    async def async_stop(self) -> None:
        self.ha_szenen.stop()
        self.ha_wippe.stop()
        if self._unsub:
            self._unsub()
        if self._sync_task:
            self._sync_task.cancel()
        if self._nachfuehren_task:
            self._nachfuehren_task.cancel()
        await self.hass.async_add_executor_job(self.bridge.stop)

    def _schedule_sync(self, delay: float) -> None:
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
        self._sync_task = self.hass.async_create_background_task(self._sync(delay), f"{DOMAIN} sync")

    async def _sync(self, delay: float) -> None:
        """Ablauf 10.1: hello (Uhr), dann Zustände aus der Tabelle, unbekannte Kanäle abfragen."""
        await asyncio.sleep(delay)
        if self.flasht:
            return
        try:
            info = await self._hello()
            if info is None:
                return
            if self._update_noetig(info):
                self.hass.async_create_background_task(self.firmware_aktualisieren(), f"{DOMAIN} firmware")
                return
            _LOGGER.info("eNet bridge connected: firmware %s, protocol %s, configuration %s",
                         info.get("fw"), info.get("proto"), info.get("config"))
            self._geraet_versionen(info)
            async_dispatcher_send(self.hass, self.signal("bridge"))
            if not self.radio.get("ok"):
                _LOGGER.warning("Bridge: radio module (CC1101) not reachable – check the wiring")
                return
            await self._config_abgleichen(info)
            if self.server_on != (self.server_soll and not self.mithoeren):
                _LOGGER.info("Bridge: Server-Funktionen → %s (wie in HA eingestellt)", self.server_soll)
                await self.server_setzen(self.server_soll)
            if self.mithoeren:  # nichts senden: Zustände nur aus der Tabelle, Kalibrierung aus fremden Telegrammen
                self.pruefung.szenen_pruefen()  # Szenen bleiben eNet-Szenen, bis „Nur mithören“ aus ist: Hinweis
                await self.bridge.command("state")
                if not self.radio.get("calibrated"):
                    await self._passiv_kalibrieren()
                return
            kalibriert = not self.radio.get("calibrated")
            if kalibriert:
                await self.kalibrieren()
            elif self.kanaele and not await self._antwortet(list({k.ia: k for k in self.kanaele}.values())
                                                             [:KAL_TESTKANAELE]):
                # Zustände kommen sonst aus der Tabelle der Bridge, ohne dass ein Aktor antworten muss: Nach einem
                # Modultausch bliebe die alte Korrektur stehen und die Bridge taub (Nachführen braucht Empfang).
                _LOGGER.warning("No actuator answers – searching the frequency correction again")
                await self.kalibrieren()
                kalibriert = True  # nicht gleich noch einmal suchen
            for z in self.zustand.values():
                z.known = False
            await self.bridge.command("state")
            await asyncio.sleep(0.5)  # state-Ereignisse aus der Tabelle abwarten
            for versuch in range(3):
                offen = [k for k in self.kanaele if not self.zustand[k.key].known]
                if not offen:
                    break
                if versuch:
                    await asyncio.sleep(2)
                for k in offen:
                    try:
                        await self.bridge.request("poll", ia=k.ia, ch=k.nr)
                    except BridgeError as err:
                        _LOGGER.debug("poll %s (Versuch %d): %s", k.key, versuch + 1, err)
            if not kalibriert and self.kanaele and not any(self.zustand[k.key].known for k in self.kanaele):
                _LOGGER.warning("No actuator answers – searching the frequency correction again")
                if await self.kalibrieren():
                    for k in self.kanaele:
                        try:
                            await self.bridge.request("poll", ia=k.ia, ch=k.nr)
                        except BridgeError:
                            pass
            for k in self.kanaele:
                if not self.zustand[k.key].known:
                    _LOGGER.warning("%s (%s) does not answer the status request", k.geraet["name"], k.key)
            await self._aktor_basis_lesen()
            await self._parameter_vorladen()
            self.pruefung.geraete_pruefen()
            for schritt in (self.ha_szenen.umwandeln,  # eNet-Szenen aus dem Import → HA-Szenen (einmalig)
                            self.ha_szenen.abgleichen,  # nur Abweichungen gehen per Funk hinaus
                            self.pruefung.links_pruefen,  # Anlagenprüfung: liest nur
                            self.pruefung.verknuepfungen_pruefen):  # Link-Tabellen (je Aktor einmal), liest nur
                try:
                    await schritt()
                except BridgeError:
                    raise
                except Exception:  # noqa: BLE001 – unerwartete Anlage/scenes.yaml: melden, Bridge läuft weiter
                    _LOGGER.exception("Scenes: %s failed", schritt.__name__)
            self.pruefung.szenen_pruefen()
        except BridgeError as err:
            _LOGGER.warning("Synchronisation with the bridge failed: %s", err)

    async def _hello(self) -> dict[str, Any] | None:
        """hello mit Wiederholung; antwortet keine Bridge, Hinweis zur Firmware-Installation."""
        for versuch in range(HELLO_VERSUCHE):
            if versuch:
                await asyncio.sleep(2)
            try:
                info = await self.bridge.hello()
            except BridgeError as err:
                if err.err != "timeout":
                    raise
                continue
            if info.get("product", "enet-bridge") == "enet-bridge" and "proto" in info:
                self.keine_bridge = False
                return info
            break
        self.bridge.info = {}
        if not self.keine_bridge:
            self.keine_bridge = True
            _LOGGER.warning("No eNet bridge answers on port %s – install the firmware via the update entity "
                            "\"Bridge firmware\"", self.bridge.port)
        async_dispatcher_send(self.hass, self.signal("bridge"))
        return None

    def _update_noetig(self, info: dict[str, Any]) -> bool:
        """Bridge antwortet als Bridge, mitgelieferte Firmware ist neuer → automatisch. Je Bridge und
        Ausgangsversion nur einmal: meldet sie nach dem Flashen noch die alte Version, keine Schleife."""
        neu = self.fw_bundle
        if not neu or firmware.version_tupel(info.get("fw")) >= firmware.version_tupel(neu):
            return False
        versuch = (info.get("id"), info.get("fw"), neu)
        if versuch in self._auto_update:
            return False
        self._auto_update.add(versuch)
        _LOGGER.warning("Bridge firmware %s is outdated – updating to %s automatically", info.get("fw"), neu)
        return True

    async def firmware_aktualisieren(self) -> None:
        """Port freigeben, mitgelieferte Firmware flashen, wieder verbinden (danach neuer Abgleich)."""
        if self.flasht or not self.fw_bundle:
            return
        if fremdes_funkmodul(self.bridge.port):
            raise BridgeError("fremd", tr("keine_bridge_port", port=self.bridge.port))
        self.flasht = True
        async_dispatcher_send(self.hass, self.signal("bridge"))
        try:
            await self.hass.async_add_executor_job(self.bridge.release)
            # Chip aus dem hello; leerer ESP (keine Bridge) → esptool erkennt ihn
            chip = None if self.keine_bridge else self.bridge.info.get("chip", firmware.STANDARD_CHIP)
            await self.hass.async_add_executor_job(firmware.flashen, self.bridge.port, chip)
            _LOGGER.warning("Bridge firmware %s installed", self.fw_bundle)
        except Exception as err:  # noqa: BLE001 – esptool wirft beliebige Fehler
            _LOGGER.error("Flashing the bridge failed: %s", err)
            raise BridgeError("flash", str(err)) from err
        finally:
            self.flasht = False
            self.bridge.reconnect()
            async_dispatcher_send(self.hass, self.signal("bridge"))

    async def _config_abgleichen(self, info: dict[str, Any]) -> None:
        """Server-Konfiguration aus anlage.json übertragen, wenn die Bridge eine andere (oder keine) hat."""
        blob = esp_config.build(self.anlage)[0]
        crc = blob[-2:].hex()
        cfg = info.get("config", {})
        if cfg.get("valid") and cfg.get("crc") == crc:
            return
        _LOGGER.info("Bridge: transferring configuration %s → %s", cfg.get("crc"), crc)
        ergebnis = await self.bridge.config_hochladen(esp_config.teile(blob))
        self.bridge.info["config"] = {"valid": True, **{k: ergebnis.get(k) for k in
                                                         ("crc", "channels", "scenes", "keys")}}

    async def _antwortet(self, kanaele: list[Kanal]) -> bool:
        for k in kanaele:
            try:
                await self.bridge.request("poll", ia=k.ia, ch=k.nr)
                return True
            except BridgeError:
                continue
        return False

    async def kalibrieren(self) -> int | None:
        """Frequenzkorrektur suchen (nur Statusabfragen), fein abgleichen und auf der Bridge speichern.

        Grob: Kandidaten durchprobieren, bis ein Aktor antwortet. Fein: FREQEST der Antworten mitteln
        (FSCTRL0 += FREQEST). Liefert den gespeicherten Wert oder None (kein Aktor erreichbar)."""
        async with self._kal_lock:
            # je Gerät ein Kanal, damit die Grobsuche verschiedene Aktoren probiert
            test = list({k.ia: k for k in self.kanaele}.values())[:KAL_TESTKANAELE]
            if not test:
                return None
            start = self.radio.get("freq_offset", KAL_KANDIDATEN[0])
            for f in dict.fromkeys((start, *KAL_KANDIDATEN)):
                await self._radio(freq_offset=f)
                if await self._antwortet(test):
                    break
            else:
                await self._radio(freq_offset=start)
                _LOGGER.warning("Calibration: no actuator answers at any frequency correction")
                return None
            for _ in range(2):  # Telegramme für den Mittelwert sammeln
                for k in self.kanaele:
                    try:
                        await self.bridge.request("poll", ia=k.ia, ch=k.nr)
                    except BridgeError:
                        pass
            r = await self._radio()
            if r.get("freqest_n", 0) >= KAL_PROBEN:
                f = max(-128, min(127, f + round(r["freqest"])))
            await self._radio(freq_offset=f, save=True)
            _LOGGER.info("Calibration: frequency correction %d saved (previous offset %.1f from %d telegrams)",
                         f, r.get("freqest", 0.0), r.get("freqest_n", 0))
            return f

    async def _nachfuehren(self) -> None:
        """Frequenzkorrektur im Betrieb nachführen, ohne zu senden: Die Bridge mittelt FREQEST aller gültigen
        Telegramme seit der letzten Änderung. Liegt der Mittelwert um mindestens NACHFUEHREN_SCHWELLE Schritte
        daneben, FSCTRL0 um diesen Betrag verstellen und speichern (höchstens einmal je NACHFUEHREN_ABSTAND_S).
        Fängt Drift (Temperatur, Alterung) und einen Modultausch ab; ersetzt den früheren Knopf „Funk kalibrieren“."""
        while True:
            await asyncio.sleep(NACHFUEHREN_S)
            if (self.flasht or not self.bridge.connected or self._kal_lock.locked()
                    or not self.radio.get("ok") or not self.radio.get("calibrated")):
                continue
            try:
                async with self._kal_lock:
                    r = await self._radio()
                    n, est, f = r.get("freqest_n", 0), r.get("freqest"), r.get("freq_offset")
                    if (n < NACHFUEHREN_PROBEN or est is None or f is None or abs(est) < NACHFUEHREN_SCHWELLE
                            or time.monotonic() - self._nachgefuehrt < NACHFUEHREN_ABSTAND_S):
                        continue
                    neu = max(-128, min(127, f + round(est)))
                    await self._radio(freq_offset=neu, save=True)
                    self._nachgefuehrt = time.monotonic()
                    _LOGGER.info("Frequency correction adjusted: %d → %d (offset %.1f from %d telegrams)",
                                 f, neu, est, n)
            except BridgeError as err:
                _LOGGER.debug("Frequency tracking: %s", err)

    async def _passiv_kalibrieren(self) -> int | None:
        """Frequenzkorrektur ohne Senden: je Kandidat warten, bis genug fremde Telegramme (Tasten, Aktor-Meldungen,
        echter Server) empfangen sind, dann mit ihrem FREQEST fein abgleichen und speichern. Läuft, bis es klappt
        (oder „Nur mithören“ aus ist – dann übernimmt die normale Kalibrierung)."""
        start = self.radio.get("freq_offset", KAL_KANDIDATEN[0])
        _LOGGER.info("Listen only: determining frequency correction from received telegrams")
        while self.mithoeren:
            for f in dict.fromkeys((start, *KAL_KANDIDATEN)):
                await self._radio(freq_offset=f)
                ende = time.monotonic() + KAL_PASSIV_S
                while time.monotonic() < ende and self.mithoeren:
                    await asyncio.sleep(15)
                    r = await self._radio()
                    if r.get("freqest_n", 0) >= KAL_PROBEN:
                        f = max(-128, min(127, f + round(r["freqest"])))
                        await self._radio(freq_offset=f, save=True)
                        _LOGGER.info("Listen only: frequency correction %d saved (from %d telegrams)",
                                     f, r["freqest_n"])
                        return f
                if not self.mithoeren:
                    return None
        return None

    async def _radio(self, **felder: Any) -> dict[str, Any]:
        reply = await self.bridge.command("radio", **felder)
        self.bridge.info["radio"] = reply["radio"]
        try:  # Empfangszähler zur Diagnose (gültig / CRC-Fehler / unlesbar), Anzeige am Sensor Frequenzkorrektur
            self.bridge.info["stats"] = await self.bridge.command("stats")
        except BridgeError:
            pass
        async_dispatcher_send(self.hass, self.signal("bridge"))
        return reply["radio"]

    # --- Ereignisse ---------------------------------------------------------
    @callback
    def _nach_bridge_szene(self, nr: int) -> None:
        """Lokale Szene der Bridge fertig: enthielt sie einen Szenenaufruf (IA 0), wie nach einem Aufruf aus HA
        den Status der Mitglieder abfragen (die Bridge selbst fragt nur nach Einzelbefehlen nach)."""
        namen = list(self.anlage.get("esp", {}).get("szenen", {}))
        if not 0 <= nr < len(namen):
            return
        for a in self.anlage["esp"]["szenen"][namen[nr]]:
            if a["ia"] == "0.0.0":
                s = next((s for s in self.szenen.szenen if s.ga == a["handle"] and s.nr == int(a["wert"], 16)), None)
                if s:
                    self.szenen.status_abfragen(s)

    @callback
    def _bridge_neu_zeichnen(self, _now: Any = None) -> None:
        async_dispatcher_send(self.hass, self.signal("bridge"))

    @callback
    def _on_message(self, msg: dict[str, Any]) -> None:
        ev = msg.get("ev")
        if ev == "_connection":
            if msg["connected"]:
                self._schedule_sync(SYNC_DELAY_S)
            async_dispatcher_send(self.hass, self.signal("bridge"))
        elif ev == "radio":
            war_ok = self.radio.get("ok")
            self.bridge.info["radio"] = msg.get("radio", {})
            if self.radio.get("ok"):
                _LOGGER.info("Bridge: radio module reachable again")
                if not war_ok:
                    self._schedule_sync(0.5)
            else:
                _LOGGER.warning("Bridge: radio module (CC1101) stopped answering – check the wiring")
            async_dispatcher_send(self.hass, self.signal("bridge"))
        elif ev == "start":
            _LOGGER.info("Bridge gestartet (%s)", msg.get("reset_reason"))
            self._schedule_sync(0.2)
        if ev in ("state", "result") and msg.get("rssi") is not None and msg.get("ia"):
            d = self.diag.setdefault(msg["ia"], Diagnose())
            d.rssi, d.zeit = msg["rssi"], dt_util.now()
            if ev == "result":
                d.versuche = msg.get("tries")
            async_dispatcher_send(self.hass, self.signal(f"diag_{msg['ia']}"))
        if ev == "state":
            key = f"{msg.get('ia')}/{msg.get('ch')}"
            if (z := self.zustand.get(key)) is not None:
                _LOGGER.debug("State %s: %s", key, msg)
                self.pruefung.antwortet(msg.get("ia"))
                z.on, z.percent, z.known = bool(msg.get("on")), int(msg.get("percent", 0)), True
                if msg.get("source") != "poll":
                    z.gemeldet = time.monotonic()
                async_dispatcher_send(self.hass, self.signal(f"state_{key}"))
        elif ev == "key":
            self._batterie(msg)
            if self._sensorwert(msg):
                return
            taste = self.taste_fuer(msg.get("sn", ""), int(msg.get("ga", -1))) \
                or self._sender_basis_lernen(msg.get("sn", ""), int(msg.get("ga", -1)))
            # seq (Statusabfragen): neuer Druck = neue seq, Wiederholung (Sender sah keine Antwort) = gleiche seq
            kennung = (msg.get("sn"), msg.get("ga"), msg.get("cmd"), msg.get("seq"))
            jetzt = time.monotonic()
            if self._letzte_taste[0] == kennung and jetzt - self._letzte_taste[1] < WIEDERHOLUNG_S:
                _LOGGER.debug("Repeated button telegram discarded: %s", msg)
                return  # Sender wiederholt unbeantwortete Telegramme (z. B. Loslassen nach „beide Tasten“)
            self._letzte_taste = (kennung, jetzt)
            if taste is None:
                _LOGGER.debug("Button press without assignment: %s", msg)
                self.pruefung.unbekannter_sender(msg.get("sn", ""))
                return
            if taste.art == ART_WIPPE and msg.get("kind") == "server":
                wippe_als_server_taste(msg)
            async_dispatcher_send(self.hass, self.signal(f"key_{taste.key}"), msg)
            if taste.art == ART_WIPPE:
                self.ha_wippe.tastendruck(taste, msg)
            self.ha_szenen.tastendruck(msg)
            self._einschalthelligkeit_pruefen(taste, msg)
        elif ev == "scene_done":
            self._nach_bridge_szene(int(msg.get("nr", -1)))
        elif ev == "real_server":
            if not self.real_server and self.server_on:
                _LOGGER.warning("Real eNet server active – bridge and server both answer the server buttons. "
                                "Switch off the server or the bridge's \"Server functions\"")
            self.real_server_zeit = dt_util.now()
            async_dispatcher_send(self.hass, self.signal("bridge"))
            async_call_later(self.hass, REAL_SERVER_S + 1, self._bridge_neu_zeichnen)
        elif ev == "error":
            _LOGGER.warning("Bridge meldet Fehler: %s", msg)


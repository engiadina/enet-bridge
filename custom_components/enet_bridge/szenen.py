"""eNet-Szenen = KNX-Szenen in den Aktoren: verwalten, aufrufen, lernen, anlegen, löschen.

Eine Szene ist in jedem Mitglied (Aktor-Kanal) über zwei Links unter der Server-SN eingetragen:
Szenen-GA → GO 6 (Aufruf, 1 Byte Nr., + 0x80 = aktuellen Zustand lernen) und Status-GA → GO 19
(Sammelstatus). Die Szenenwerte stehen im Aktor (SCV1[Nr]); sie werden per Lernbefehl gesetzt.
Raumszenen „Licht an/aus“ teilen sich eine GA (verschiedene Nummern) – Links nur löschen, wenn keine
andere Szene des Mitglieds sie noch nutzt. Bestätigt 2026-10-28 (NOTES.md, SCHNITTSTELLE.md).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from homeassistant.helpers.storage import Store

from .bridge import BridgeError
from .const import DOMAIN
from .texte import tr

if TYPE_CHECKING:
    from .hub import Hub, Kanal

_LOGGER = logging.getLogger(__name__)

PID_LINKS = 63
GO_SZENE, GO_SAMMELSTATUS = 6, 19
# SCV1[0…37]; 16/17 = Zentraltaste „Alles an/aus“ (FT_INAOOS), 18/19 ab Werk inaktiv (SNA = 0, Bedeutung offen)
NR_FREI = [n for n in range(38) if n not in (16, 17, 18, 19)]
SCV_START = 4            # SCV1[Nr] im Kanal-Objekt, PID 79 (Schalter 00/FF, Dimmer 0–255 = 0–100 %)
WERT_TOLERANZ = 4        # gelernter Wert vs. Szenenwert (Byte; 1 % ≈ 2,55)
SPERRE_S = 4.0           # so lange nach einem Szenenaufruf: passende Einzelbefehle von HA nicht senden
GA_FREI_AB = 100         # eigene Szenen-GAs (Paare GA, GA+1), oberhalb der vom Server vergebenen
STATUS_NACH_S = 5.0      # Status der Mitglieder nach einem Szenenaufruf abfragen (Dimmrampe abwarten)


def wert_gleich(a: int | None, b: int | None) -> bool:
    """Szenenwerte gleich bis auf Rundung (Prozent ↔ Byte, gelernte Werte)."""
    return a is not None and b is not None and abs(a - b) <= WERT_TOLERANZ


@dataclass
class Szene:
    name: str
    nr: int
    ga: int
    ga_status: int | None
    mitglieder: list[str] = field(default_factory=list)  # Kanal-Keys „IA/Kanal“
    art: str = "eigen"  # eigen | raum (Raumszene des eNet Servers) | ha (eNet-Teil einer HA-Szene)
    ha_id: str | None = None  # HA-Szene (scenes.yaml „id“), deren eNet-Teil diese Szene ist
    werte: dict[str, int] = field(default_factory=dict)  # in SCV1[nr] geschriebene Werte je Mitglied
    offen: list[str] = field(default_factory=list)  # Mitglieder, deren Abgleich fehlgeschlagen ist

    @property
    def id(self) -> str:
        return f"{self.ga}_{self.nr}"


class SzenenVerwaltung:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._store: Store = Store(hub.hass, 1, f"{DOMAIN}_szenen_{hub.entry_id}")
        self.szenen: list[Szene] = []
        self.sperre: dict[str, tuple[int, float]] = {}  # Kanal → (Szenenwert, bis) nach einem Szenenaufruf
        self.letzter_aufruf: dict[str, float] = {}  # Kanal → Zeit des letzten Szenenaufrufs mit diesem Kanal

    async def laden(self) -> None:
        daten = await self._store.async_load()
        if daten is None:  # erster Start: Szenen aus dem Export (anlage.json, tools/iex.py)
            daten = [{"name": s["name"], "nr": s["nr"], "ga": s["ga"], "ga_status": s.get("ga_status"),
                      "mitglieder": [f"{m['ia']}/{m['ch']}" for m in s.get("mitglieder", [])],
                      "art": s.get("art", "eigen")} for s in self.hub.anlage.get("szenen", [])]
            self.szenen = [Szene(**d) for d in daten]
            await self._speichern()
        else:
            self.szenen = [Szene(**d) for d in daten]

    async def _speichern(self) -> None:
        await self._store.async_save([asdict(s) for s in self.szenen])

    def get(self, szenen_id: str) -> Szene | None:
        return next((s for s in self.szenen if s.id == szenen_id), None)

    # --- Ausführen ----------------------------------------------------------
    async def aufrufen(self, s: Szene) -> None:
        await self.hub.bridge.request("scene_call", ga=s.ga, nr=s.nr)
        self.status_abfragen(s)

    def sperren(self, s: Szene) -> None:
        """Gleich folgende Einzelbefehle von HA (HA-Szene schaltet Gerät für Gerät) nicht mehr senden."""
        bis = time.monotonic() + SPERRE_S
        for key in s.mitglieder:
            if key in s.werte and key not in s.offen:
                self.sperre[key] = (s.werte[key], bis)

    def gesperrt(self, key: str, an: bool, prozent: int | None) -> bool:
        """Ist dieser Befehl schon durch den Szenenaufruf erledigt? (verbraucht die Sperre)"""
        wert, bis = self.sperre.get(key, (None, 0.0))
        if wert is None or time.monotonic() > bis:
            return False
        _LOGGER.debug("Lock %s: scene value %s, command on=%s %s %%", key, wert, an, prozent)
        if an != (wert > 0) or (an and prozent is not None and abs(prozent - round(wert * 100 / 255)) > 1):
            return False
        del self.sperre[key]
        _LOGGER.debug("Single command %s (on=%s, %s %%) suppressed – done by the scene", key, an, prozent)
        return True

    async def lernen(self, s: Szene) -> None:
        """Aktuellen Zustand aller Mitglieder als Szenenwerte speichern."""
        await self.hub.bridge.request("scene_call", ga=s.ga, nr=s.nr, learn=True)

    def status_abfragen(self, s: Szene) -> None:
        """Nach dem Aufruf melden alle Mitglieder gleichzeitig ihren Status – die Telegramme stoßen zusammen.
        Deshalb wie der eNet Server (functionsToPollStatusAfterCallScene) nach der Rampe nachfragen."""
        aufruf = time.monotonic()
        for key in s.mitglieder:
            self.letzter_aufruf[key] = aufruf

        async def nachfragen() -> None:
            await asyncio.sleep(STATUS_NACH_S)
            for k in self.hub.kanaele:
                if k.key in s.mitglieder:
                    try:
                        await self.hub.bridge.request("poll", ia=k.ia, ch=k.nr)
                    except BridgeError as err:
                        _LOGGER.debug("Status after scene %s, %s: %s", s.name, k.key, err)
            if s.art == "ha":
                await self._nachkorrigieren(s, aufruf)

        self.hub.hass.async_create_background_task(nachfragen(), f"enet status after scene {s.name}")

    async def _nachkorrigieren(self, s: Szene, aufruf: float) -> None:
        """Szenentelegramm ist unquittiert: Lampen, die es nicht bekommen haben, einzeln schalten (ihren Einzelbefehl
        hatte die Sperre unterdrückt). Verloren ist es nur, wenn sich der Aktor seit dem Aufruf nicht von selbst
        gemeldet hat – sonst hat er die Szene ausgeführt oder wurde danach bewusst anders geschaltet (Editor,
        Taste, Dashboard; 2026-10-30: Korrektur hatte solche Änderungen zurückgedreht). Ebenso nicht, wenn danach
        schon die nächste Szene mit diesem Kanal kam (2026-11-02: die Korrektur von „Raum aus“ schaltete das gerade
        aufgerufene „Raum an“ wieder aus – die Statusabfrage zählt nicht als eigene Meldung)."""
        for k in self.hub.kanaele:
            if k.key not in s.werte or k.key in s.offen or self.hub.zustand[k.key].gemeldet > aufruf \
                    or self.letzter_aufruf.get(k.key, 0.0) > aufruf:
                continue
            z, soll = self.hub.zustand[k.key], s.werte[k.key]
            prozent = round(soll * 100 / 255)
            _LOGGER.debug("After scene %s: %s is %s/%s %%, expected %s", s.name, k.key, z.on, z.percent, soll)
            if self._steht_auf(k, soll):
                continue
            _LOGGER.info("Scene %s: %s not at scene value – switched individually", s.name, k.key)
            try:
                if soll and k.art == "dimmen":
                    await self.hub.bridge.request("dim", ia=k.ia, ch=k.nr, percent=max(1, prozent))
                else:
                    await self.hub.bridge.request("switch", ia=k.ia, ch=k.nr, on=soll > 0)
            except BridgeError as err:
                _LOGGER.warning("Scene %s, %s: %s", s.name, k.key, err)

    # --- eNet-Teil einer HA-Szene -----------------------------------------
    def _nr_frei(self, keys: set[str], ausser: Szene | None = None) -> int:
        """Kleinste Szenennummer, die in keinem dieser Kanäle von einer anderen Szene belegt ist."""
        belegt = {x.nr for x in self.szenen if x is not ausser and keys & set(x.mitglieder)}
        nr = next((n for n in NR_FREI if n not in belegt), None)
        if nr is None:
            raise BridgeError("full", tr("keine_szenennummer"))
        return nr

    def _steht_auf(self, k: Kanal, wert: int) -> bool:
        """Steht der Kanal (laut letzter Meldung) auf diesem Szenenwert?"""
        z = self.hub.zustand[k.key]
        if not z.known or z.on != (wert > 0):
            return False
        return not z.on or k.art != "dimmen" or abs(z.percent - round(wert * 100 / 255)) <= 1

    def ha_szene(self, ha_id: str) -> Szene | None:
        return next((s for s in self.szenen if s.ha_id == ha_id), None)

    async def ha_abgleichen(self, ha_id: str, name: str, soll: dict[str, int]) -> list[str]:
        """eNet-Teil einer HA-Szene in die Aktoren bringen: Links, Szenennummer, Werte. Liefert Fehlertexte."""
        s = self.ha_szene(ha_id)
        if s is None:
            nr = self._nr_frei(set(soll))
            ga = self._ga_frei()
            s = Szene(name, nr, ga, ga + 1, [], art="ha", ha_id=ha_id)
            self.szenen.append(s)
        s.name = name
        if s.nr in {x.nr for x in self.szenen if x is not s and set(soll) & set(x.mitglieder)}:
            # Nummer in einem (neuen) Mitglied schon belegt – oder geteilt wie bei den Raumszenen des eNet Servers
            # (alle Räume „Licht an“ = Nr. 16): eigene Nummer, sonst änderte der Wert auch die anderen Szenen
            s.nr, s.werte = self._nr_frei(set(soll), ausser=s), {}
        fehler, offen = [], []
        kanaele = {k.key: k for k in self.hub.kanaele}
        for key in [m for m in s.mitglieder if m not in soll]:
            try:
                if key in kanaele:
                    await self._links(kanaele[key], s, anlegen=False)
                s.mitglieder.remove(key)
                s.werte.pop(key, None)
            except BridgeError as err:
                fehler.append(f"{key} entfernen: {err}")
                offen.append(key)
        for key in soll:
            k = kanaele.get(key)
            if k is None or key in s.mitglieder:
                continue
            try:
                await self._links(k, s, anlegen=True)
                s.mitglieder.append(key)
                await self._speichern()
            except BridgeError as err:
                fehler.append(f"{k.geraet['name']} K{k.nr}: {err}")
                offen.append(key)
        aendern = {key: w for key, w in soll.items() if key in s.mitglieder and not wert_gleich(s.werte.get(key), w)}
        if aendern and all(self._steht_auf(kanaele[key], w) for key, w in soll.items() if key in s.mitglieder):
            # Lampen stehen schon auf der Szene (Szenen-Editor stellt sie live ein): lernen statt Rahmen –
            # ein Telegramm, kein Neustart der Aktoren, die Lampen bleiben an
            try:
                await self.hub.bridge.request("scene_call", ga=s.ga, nr=s.nr, learn=True)
                await asyncio.sleep(1)
                for key, w in list(aendern.items()):
                    k = kanaele[key]
                    r = await self.hub.bridge.request("prop_read", ia=k.ia, obj=k.obj, pid=79,
                                                      start=SCV_START + s.nr, count=1)
                    gelesen = bytes.fromhex(r.get("value", "") or "00")[0]
                    if abs(gelesen - w) <= WERT_TOLERANZ:
                        s.werte[key] = w
                        del aendern[key]
                    else:
                        _LOGGER.debug("Scene %s, %s: learned %s instead of %s – writing", s.name, key, gelesen, w)
            except BridgeError as err:
                _LOGGER.debug("Scene %s: learning failed (%s) – writing values", s.name, err)
        for key, w in aendern.items():
            k = kanaele[key]
            try:
                await self.hub.params.roh_schreiben(k.ia, self.hub.geraet_obj(k.geraet), k.obj,
                                                    SCV_START + s.nr, bytes([w]), zweck=tr("zweck_szenenwert", name=s.name))
                s.werte[key] = w
            except BridgeError as err:
                fehler.append(f"{k.geraet['name']} K{k.nr}: {err}")
                offen.append(key)
            await self._speichern()  # nach jedem Kanal: Stand stimmt auch bei Abbruch
        s.offen = offen
        await self._speichern()
        return fehler

    # --- Anlegen / Löschen -------------------------------------------------
    def _ga_frei(self) -> int:
        gas = {g for s in self.szenen for g in (s.ga, s.ga_status) if g is not None}
        gas |= {t["antwort_ga"] for g in self.hub.anlage["geraete"] for t in g.get("tasten", []) if t.get("antwort_ga")}
        ga = GA_FREI_AB
        while ga in gas or ga + 1 in gas:
            ga += 2
        return ga

    def _frei(self) -> tuple[int, int]:
        nrs = {s.nr for s in self.szenen}
        gas = {g for s in self.szenen for g in (s.ga, s.ga_status) if g is not None}
        nr = next((n for n in NR_FREI if n not in nrs), None)
        if nr is None:
            raise BridgeError("full", tr("szenenspeicher_voll"))
        ga = GA_FREI_AB
        while ga in gas or ga + 1 in gas:
            ga += 2
        return nr, ga

    async def anlegen(self, name: str, kanaele: list[Kanal]) -> Szene:
        """Neue Szene: Links in allen Mitgliedern anlegen. Werte danach per lernen() übernehmen."""
        if not kanaele:
            raise BridgeError("bad_args", tr("keine_mitglieder"))
        nr, ga = self._frei()
        s = Szene(name, nr, ga, ga + 1, [k.key for k in kanaele])
        fertig: list[Kanal] = []
        try:
            for k in kanaele:
                await self._links(k, s, anlegen=True)
                fertig.append(k)
        except BridgeError:
            for k in fertig:  # halb angelegte Szene wieder entfernen
                try:
                    await self._links(k, s, anlegen=False)
                except BridgeError as err:
                    _LOGGER.warning("%s: link not removed: %s", k.key, err)
            raise
        self.szenen.append(s)
        await self._speichern()
        return s

    async def loeschen(self, s: Szene) -> list[str]:
        """Links in allen Mitgliedern löschen und die Szene entfernen. Liefert nicht erreichbare Mitglieder."""
        offen = []
        for key in s.mitglieder:
            k = next((k for k in self.hub.kanaele if k.key == key), None)
            if k is None:
                offen.append(key)  # nicht in der Anlage (z. B. entferntes Gerät)
                continue
            try:
                await self._links(k, s, anlegen=False)
            except BridgeError as err:
                _LOGGER.warning("%s: scene %s not deleted: %s", key, s.name, err)
                offen.append(key)
        self.szenen.remove(s)
        await self._speichern()
        return offen

    async def mitglied_entfernen(self, ia: str) -> None:
        """Gerät aus allen Szenen nehmen (nach dem Entfernen des Geräts; die Links sind mit ihm gelöscht)."""
        for s in self.szenen:
            s.mitglieder = [m for m in s.mitglieder if not m.startswith(f"{ia}/")]
        await self._speichern()

    async def _links(self, k: Kanal, s: Szene, anlegen: bool) -> None:
        """Beide Links der Szene in einem Kanal anlegen bzw. löschen (im Programmier-Rahmen)."""
        andere = [x for x in self.szenen if x is not s and k.key in x.mitglieder]
        links = [(s.ga, GO_SZENE), (s.ga_status, GO_SAMMELSTATUS)]
        if not anlegen:  # GA, die eine andere Szene dieses Kanals noch nutzt, stehen lassen
            links = [(ga, go) for ga, go in links if not any(ga in (x.ga, x.ga_status) for x in andere)]
        links = [(ga, go) for ga, go in links if ga is not None]
        if not links:
            return
        sn = self.hub.anlage["server"]["sn"]
        ia = k.ia

        async def schritte() -> None:
            for ga, go in links:
                data = bytes([0 if anlegen else 2, 0]) + bytes.fromhex(sn) + ga.to_bytes(2, "big") \
                    + go.to_bytes(2, "big")
                await self.hub.bridge.request("fp_cmd", ia=ia, obj=k.obj, pid=PID_LINKS, data=data.hex())

        await self.hub.params.programmieren(ia, self.hub.geraet_obj(k.geraet), k.obj, schritte,
                                            zweck=tr("zweck_szenen_links"))

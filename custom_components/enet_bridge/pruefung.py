"""Anlagenprüfung: Abweichungen zwischen eingelesener Anlage und tatsächlicher Installation als Reparatur-
Einträge (Einstellungen → Reparaturen). Liest nur; repariert wird erst nach Bestätigung (repairs.py).

- Aktor antwortet nicht (nach dem Abgleich kein Kanal mit bekanntem Zustand) – verschwindet, sobald er sich meldet.
- eNet-Szenen, die (noch) nicht in HA-Szenen umgewandelt sind (Werte nicht lesbar, oder „Nur mithören“ ist an:
  dann wird gar nicht gelesen, die Umwandlung folgt nach dem Ausschalten).
- Szenen-Links (GA → GO 6 Aufruf, GA+1 → GO 19 Sammelstatus) fehlen in Mitgliedern einer HA-Szene.
- Tastendrücke eines Senders, der nicht in der Anlage ist.
- Auffällige Einträge in den Link-Tabellen der Aktoren (linktabelle.py): Alt-Links von Server oder Sendern und
  Links fremder Geräte. Gelesen einmal je Aktor (danach aus der Anlage gemeldet) und auf Anforderung (Dienst
  verknuepfungen_lesen, Reparatur-Dialog); entfernt oder behalten wird erst nach Bestätigung.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from . import linktabelle as lt
from .bridge import BridgeError
from .const import DOMAIN
from .szenen import GO_SAMMELSTATUS, GO_SZENE, PID_LINKS
from .texte import tr

if TYPE_CHECKING:
    from .hub import Hub, Kanal

_LOGGER = logging.getLogger(__name__)

MAX_LINKS = 24  # Einträge je GO, die höchstens gelesen werden (Tabellenende = nack rc 255)


class Pruefung:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.hass = hub.hass
        self.fehlende_links: dict[str, list[tuple[int, int]]] = {}  # Kanal → [(GA, GO)]
        self._unbekannt: set[str] = set()
        # bis 0.12.11 stand die Seriennummer im Klartext in der Issue-ID – und HA hängt die IDs an jede Diagnose
        reg = ir.async_get(self.hass)
        for domain, issue_id in list(reg.issues):
            if domain == DOMAIN and re.search(r"_[0-9a-f]{12}$", issue_id):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @staticmethod
    def _id(praefix: str, sn: str) -> str:
        """Issue-ID ohne Seriennummer im Klartext (die IDs stehen in „Diagnosedaten herunterladen“)."""
        return f"{praefix}_{hashlib.sha256(sn.encode()).hexdigest()[:8]}"

    def _melden(self, issue_id: str, key: str, platzhalter: dict[str, str], fixable: bool = False,
                daten: dict | None = None) -> None:
        ir.async_create_issue(self.hass, DOMAIN, issue_id, is_fixable=fixable, is_persistent=False,
                              severity=ir.IssueSeverity.WARNING, translation_key=key,
                              translation_placeholders=platzhalter,
                              data={"entry_id": self.hub.entry_id, **(daten or {})})

    def _erledigt(self, issue_id: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # --- Geräte ----------------------------------------------------------------
    @callback
    def geraete_pruefen(self) -> None:
        """Nach dem Abgleich mit der Bridge: Aktoren ohne einen einzigen bekannten Kanal."""
        for g in self.hub.anlage["geraete"]:
            kanaele = [k for k in self.hub.kanaele if k.geraet is g]
            if not kanaele:
                continue  # Sender schlafen, sind nicht abfragbar
            if any(self.hub.zustand[k.key].known for k in kanaele):
                self._erledigt(self._id("antwortet_nicht", g["sn"]))
            else:
                self._melden(self._id("antwortet_nicht", g["sn"]), "antwortet_nicht",
                             {"name": g["name"], "ia": g["ia"], "sn": g["sn"]}, fixable=True, daten={"sn": g["sn"]})

    @callback
    def antwortet(self, ia: str) -> None:
        g = next((g for g in self.hub.anlage["geraete"] if g["ia"] == ia), None)
        if g is not None:
            self._erledigt(self._id("antwortet_nicht", g["sn"]))

    @callback
    def unbekannter_sender(self, sn: str) -> None:
        if not sn or sn in self._unbekannt or any(g["sn"] == sn for g in self.hub.anlage["geraete"]):
            return
        self._unbekannt.add(sn)
        self._melden(self._id("unbekannter_sender", sn), "unbekannter_sender", {"sn": sn})

    # --- Szenen ----------------------------------------------------------------
    @callback
    def szenen_pruefen(self) -> None:
        offen = [s.name for s in self.hub.szenen.szenen if s.art != "ha"]
        key = "szenen_mithoeren" if self.hub.mithoeren else "szenen_nicht_umgewandelt"
        for k in ("szenen_mithoeren", "szenen_nicht_umgewandelt"):
            if k != key or not offen:
                self._erledigt(k)
        if offen:
            self._melden(key, key, {"szenen": ", ".join(offen)})

    async def _sender_abwarten(self) -> None:
        """Die Prüfungen lesen nur und haben Zeit: während ein Sender programmiert wird, pausieren. Die Bridge
        arbeitet Anfragen nacheinander ab – langsame Aktoren (bis 3 s je Link-Eintrag) bremsten sonst jeden
        Befehl an den Sender (Programmieren direkt nach dem HA-Start dauerte Minuten)."""
        while self.hub.sender_aktiv:
            await asyncio.sleep(1)

    async def _links(self, k: Kanal, go: int) -> set[int] | None:
        """GAs unter der Server-SN, die in GO go dieses Kanals eingetragen sind (None: nicht lesbar)."""
        sn = self.hub.anlage["server"]["sn"]
        gas: set[int] = set()
        for idx in range(MAX_LINKS):
            for versuch in range(3):
                await self._sender_abwarten()
                try:
                    r = await self.hub.bridge.request("fp_read", ia=k.ia, obj=k.obj, pid=PID_LINKS,
                                                      data=f"0000{go:02x}{idx:02x}")
                    break
                except BridgeError as err:
                    if err.rc == 255:  # Tabellenende
                        return gas
                    if versuch == 2:
                        return None
            v = bytes.fromhex(r.get("value", ""))
            if len(v) >= 9 and v[1:7].hex() == sn:
                gas.add(int.from_bytes(v[7:9], "big"))
        return gas

    async def links_pruefen(self) -> None:
        """Szenen-Links aller Mitglieder von HA-Szenen lesen und fehlende melden."""
        fehlend = await self._fehlende_links()
        self._fehlende_melden(fehlend)

    async def _fehlende_links(self, ia: str | None = None) -> dict[str, list[tuple[int, int]]]:
        """Fehlende Szenen-Links je Kanal (ia: nur dieses Gerät)."""
        kanaele = {k.key: k for k in self.hub.kanaele}
        bedarf: dict[str, set[tuple[int, int]]] = {}
        for s in self.hub.szenen.szenen:
            if s.art != "ha":
                continue
            for key in s.mitglieder:
                if ia is not None and not key.startswith(f"{ia}/"):
                    continue
                if key in kanaele and key not in s.offen:
                    bedarf.setdefault(key, set()).add((s.ga, GO_SZENE))
                    if s.ga_status is not None:
                        bedarf[key].add((s.ga_status, GO_SAMMELSTATUS))
        fehlend: dict[str, list[tuple[int, int]]] = {}
        for key, soll in bedarf.items():
            k = kanaele[key]
            for go in (GO_SZENE, GO_SAMMELSTATUS):
                vorhanden = await self._links(k, go)
                if vorhanden is None:
                    continue  # nicht erreichbar: meldet „antwortet nicht“
                fehlt = [(ga, g) for ga, g in soll if g == go and ga not in vorhanden]
                if fehlt:
                    fehlend.setdefault(key, []).extend(fehlt)
        return fehlend

    def _fehlende_melden(self, fehlend: dict[str, list[tuple[int, int]]]) -> None:
        kanaele = {k.key: k for k in self.hub.kanaele}
        self.fehlende_links = fehlend
        if fehlend:
            namen = sorted({f"{kanaele[key].geraet['name']} K{kanaele[key].nr}" for key in fehlend})
            szenen = sorted({s.name for s in self.hub.szenen.szenen for key, links in fehlend.items()
                             if key in s.mitglieder and any(ga in (s.ga, s.ga_status) for ga, _ in links)})
            self._melden("szenen_links_fehlen", "szenen_links_fehlen",
                         {"kanaele": ", ".join(namen), "szenen": ", ".join(szenen)}, fixable=True)
        else:
            self._erledigt("szenen_links_fehlen")

    async def links_reparieren(self) -> list[str]:
        """Fehlende Szenen-Links anlegen (Programmier-Rahmen; Zustände werden danach wiederhergestellt)."""
        kanaele = {k.key: k for k in self.hub.kanaele}
        sn = self.hub.anlage["server"]["sn"]
        fehler = []
        for key, links in self.fehlende_links.items():
            k = kanaele.get(key)
            if k is None:
                continue

            async def schritte(k=k, links=links) -> None:
                for ga, go in links:
                    data = bytes([0, 0]) + bytes.fromhex(sn) + ga.to_bytes(2, "big") + go.to_bytes(2, "big")
                    await self.hub.bridge.request("fp_cmd", ia=k.ia, obj=k.obj, pid=PID_LINKS, data=data.hex())

            try:
                await self.hub.params.programmieren(k.ia, self.hub.geraet_obj(k.geraet), k.obj, schritte,
                                                    zweck=tr("zweck_szenen_links"))
            except BridgeError as err:
                fehler.append(f"{k.geraet['name']} K{k.nr}: {err}")
        # fehlten Links, ist meist das Gerät zurückgesetzt – dann fehlen auch seine Szenenwerte
        for ia in sorted({kanaele[key].ia for key in self.fehlende_links if key in kanaele}):
            fehler += await self.hub.szenen.werte_wiederherstellen(ia)
        await self.links_pruefen()
        return fehler

    async def aktor_wiederherstellen(self, ia: str) -> list[str]:
        """Szenen eines wieder eingelernten Aktors (Werksreset) zurück ins Gerät: fehlende Links, dann Werte."""
        fehlend = await self._fehlende_links(ia)
        kanaele = {k.key: k for k in self.hub.kanaele}
        fehler = []
        for key, links in fehlend.items():
            k = kanaele[key]

            async def schritte(k=k, links=links) -> None:
                sn = self.hub.anlage["server"]["sn"]
                for ga, go in links:
                    data = bytes([0, 0]) + bytes.fromhex(sn) + ga.to_bytes(2, "big") + go.to_bytes(2, "big")
                    await self.hub.bridge.request("fp_cmd", ia=k.ia, obj=k.obj, pid=PID_LINKS, data=data.hex())

            try:
                await self.hub.params.programmieren(k.ia, self.hub.geraet_obj(k.geraet), k.obj, schritte,
                                                    zweck=tr("zweck_szenen_links"))
            except BridgeError as err:
                fehler.append(f"{k.geraet['name']} K{k.nr}: {err}")
        fehler += await self.hub.szenen.werte_wiederherstellen(ia)
        return fehler

    # --- Link-Tabellen (Verknüpfungen) -------------------------------------------
    async def tabelle_lesen(self, k: Kanal) -> list[lt.Link]:
        """Ganze Link-Tabelle eines Kanals; BridgeError, wenn sie nicht vollständig lesbar ist."""
        links: list[lt.Link] = []
        for go in range(1, lt.MAX_GO + 1):
            for idx in range(lt.MAX_EINTRAEGE):
                for versuch in range(3):
                    await self._sender_abwarten()
                    try:
                        r = await self.hub.bridge.request("fp_read", ia=k.ia, obj=k.obj, pid=PID_LINKS,
                                                          data=f"0000{go:02x}{idx:02x}")
                        break
                    except BridgeError as err:
                        if err.err == "nack":  # rc 255 = Ende der Liste; bei Eintrag 0: kein GO mehr
                            if idx == 0 and go == 1:  # GO 1 hat immer einen Werkseintrag
                                raise BridgeError(err.err, tr("linktabelle_unlesbar", key=k.key, err=err)) from err
                            if idx == 0:
                                return links
                            r = None
                            break
                        if versuch == 2:
                            raise BridgeError(err.err, tr("linktabelle_unlesbar", key=k.key, err=err)) from err
                if r is None:
                    break
                if (link := lt.Link.aus_wert(bytes.fromhex(r.get("value", "")))) is not None:
                    links.append(link)
        return links

    async def geraet_links_lesen(self, g: dict) -> dict[int, list[dict]]:
        """Alle Kanäle eines Aktors lesen und bewerten, Ergebnis (auffällige Einträge) in der Anlage merken
        und als Reparatur melden. Kanal → bewertete Einträge."""
        ergebnis: dict[int, list[dict]] = {}
        for k in [k for k in self.hub.kanaele if k.geraet is g]:
            ergebnis[k.nr] = lt.tabelle_bewerten(self.hub.anlage, self.hub.szenen.szenen, k.key,
                                                 await self.tabelle_lesen(k))
        behalten = set(g.get("links_behalten", []))
        g["links"] = {"geprueft": dt_util.now().isoformat(timespec="seconds"),
                      "auffaellig": [{"ch": ch, **e} for ch, ee in ergebnis.items() for e in ee
                                     if e["art"] in lt.AUFFAELLIG and _link_id(ch, e) not in behalten]}
        await self.hub.anlage_speichern()
        self._links_melden(g)
        return ergebnis

    @callback
    def _links_melden(self, g: dict) -> None:
        auff = g.get("links", {}).get("auffaellig", [])
        if not auff:
            self._erledigt(self._id("links_auffaellig", g["sn"]))
            return
        zeilen = [tr("link_zeile", ch=e["ch"], go=e["go"], ga=e["ga"], grund=tr(f"link_{e['grund']}", **e))
                  for e in auff]
        self._melden(self._id("links_auffaellig", g["sn"]), "links_auffaellig",
                     {"name": g["name"], "ia": g["ia"], "eintraege": "\n".join(f"- {z}" for z in zeilen)},
                     fixable=True, daten={"sn": g["sn"]})

    async def verknuepfungen_pruefen(self) -> None:
        """Nach dem Abgleich: noch nie gelesene Aktoren lesen, bei den übrigen das gemerkte Ergebnis melden."""
        for g in self.hub.anlage["geraete"]:
            if not any(k.geraet is g for k in self.hub.kanaele):
                continue
            if g.get("links", {}).get("geprueft"):
                self._links_melden(g)
                continue
            try:
                await self.geraet_links_lesen(g)
            except BridgeError as err:
                _LOGGER.info("%s: link table not checked (%s), next start again", g["name"], err)

    async def links_entfernen(self, sn: str) -> list[str]:
        """Auffällige Einträge eines Aktors frisch lesen und löschen (Programmier-Rahmen; Zustände werden danach
        wiederhergestellt), danach zur Kontrolle neu lesen. Liefert Fehlertexte."""
        g = next(g for g in self.hub.anlage["geraete"] if g["sn"] == sn)
        await self.geraet_links_lesen(g)
        fehler = []
        for k in [k for k in self.hub.kanaele if k.geraet is g]:
            weg = [lt.Link(e["typ"], e["sn"], e["ga"], e["go"]) for e in g["links"]["auffaellig"] if e["ch"] == k.nr]
            if not weg:
                continue

            async def schritte(k=k, weg=weg) -> None:
                for link in weg:
                    await self.hub.bridge.request("fp_cmd", ia=k.ia, obj=k.obj, pid=PID_LINKS,
                                                  data=link.befehl(2).hex())

            _LOGGER.info("%s: deleting links %s", k.key, [(x.sn, x.ga, x.go) for x in weg])
            try:
                await self.hub.params.programmieren(k.ia, self.hub.geraet_obj(g), k.obj, schritte,
                                                    zweck=tr("zweck_alt_links"))
            except BridgeError as err:
                fehler.append(f"{g['name']} K{k.nr}: {err}")
        await self.geraet_links_lesen(g)
        if not fehler and g["links"]["auffaellig"]:
            fehler.append(tr("links_noch_da", name=g["name"], n=len(g["links"]["auffaellig"])))
        return fehler

    async def links_behalten(self, sn: str) -> None:
        """Die gemeldeten Einträge dieses Aktors gelten als gewollt und werden nicht mehr gemeldet."""
        g = next(g for g in self.hub.anlage["geraete"] if g["sn"] == sn)
        auff = g.get("links", {}).get("auffaellig", [])
        g["links_behalten"] = sorted(set(g.get("links_behalten", [])) | {_link_id(e["ch"], e) for e in auff})
        g.setdefault("links", {})["auffaellig"] = []
        await self.hub.anlage_speichern()
        self._links_melden(g)


def _link_id(ch: int, e: dict) -> str:
    return f"{ch}_{e['sn']}_{e['ga']}_{e['go']}"

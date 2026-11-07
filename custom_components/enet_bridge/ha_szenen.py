"""HA-Szenen als Original: ihr eNet-Teil wird unsichtbar als eNet-Szene in den Aktoren gehalten.

Beim Speichern einer Szene im Szenen-Editor (HA lädt die Szenen neu → „scene_reloaded“) und beim Start
gleicht die Integration ab: eNet-Lampen der Szene bekommen Links und ihren Szenenwert (SCV1[Nr]), entfernte
Lampen und gelöschte Szenen verlieren die Links. Ruft HA die Szene auf (scene.turn_on), geht der eNet-Teil
als ein Szenentelegramm hinaus; die Einzelbefehle, die HA danach an die eNet-Lampen schickt, werden für
kurze Zeit unterdrückt (szenen.SzenenVerwaltung.sperren). Der Rest der Szene läuft normal über HA.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.logbook import async_log_entry
from homeassistant.const import ATTR_ENTITY_ID, EVENT_CALL_SERVICE, STATE_ON
from homeassistant.core import Event, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.yaml import dump, load_yaml

from .bridge import BridgeError
from .const import ART_DIMMEN, DOMAIN
from .szenen import wert_gleich
from .texte import tr

if TYPE_CHECKING:
    from .hub import Hub, Kanal

_LOGGER = logging.getLogger(__name__)

EVENT_SCENE_RELOADED = "scene_reloaded"  # homeassistant/components/homeassistant/scene.py


def szenenwert(kanal: Kanal, zustand: dict[str, Any]) -> int:
    """Zustand einer Lampe in der HA-Szene → SCV1-Byte (Schalter 00/FF, Dimmer 0–255 über ganze Prozent)."""
    if zustand.get("state") != STATE_ON:
        return 0
    if kanal.art != ART_DIMMEN or zustand.get("brightness") is None:
        return 255
    prozent = max(1, round(int(zustand["brightness"]) * 100 / 255))
    return round(prozent * 255 / 100)


def zustand_aus_wert(kanal: Kanal, wert: int) -> dict[str, Any]:
    """SCV1-Byte → Zustand einer Lampe in scenes.yaml (Umkehrung von szenenwert)."""
    if not wert:
        return {"state": "off"}
    return {"state": "on", "brightness": wert} if kanal.art == ART_DIMMEN else {"state": "on"}


class HaSzenen:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.hass = hub.hass
        self._lock = asyncio.Lock()
        self._unsubs: list = []
        self.konfig: list[dict[str, Any]] = []  # zuletzt gelesene scenes.yaml (Auswahl der Tastenaktionen)
        self.in_arbeit: set[str] = set()  # HA-Szenen, deren eNet-Teil gerade übertragen wird
        self.per_taste: set[str] = set()  # HA-Szenen, deren eNet-Teil die Bridge per Server-Taste schon aufgerufen hat

    def namen(self) -> dict[str, str]:
        """Alle HA-Szenen (auch ohne eNet-Lampe): id → Name."""
        return {str(s["id"]): s.get("name") or str(s["id"]) for s in self.konfig}

    def start(self) -> None:
        self._unsubs = [self.hass.bus.async_listen(EVENT_SCENE_RELOADED, self._neu_geladen),
                        self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._dienst)]

    def stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []

    # --- Abgleich -----------------------------------------------------------
    @callback
    def _neu_geladen(self, event: Event) -> None:
        self.abgleichen_planen()

    def abgleichen_planen(self) -> None:
        async def sicher() -> None:
            try:
                await self.abgleichen()
            except Exception:  # noqa: BLE001 – nie die Integration mitreißen
                _LOGGER.exception("HA scenes: sync failed")

        self.hass.async_create_background_task(sicher(), "enet HA-Szenen abgleichen")

    def _lampen(self) -> dict[str, Kanal]:
        """entity_id → Kanal der eNet-Lichter dieser Anlage."""
        nach_uid = {f"{k.geraet['sn']}_{k.nr}": k for k in self.hub.kanaele}
        reg = er.async_get(self.hass)
        return {e.entity_id: nach_uid[e.unique_id] for e in er.async_entries_for_config_entry(reg, self.hub.entry_id)
                if e.domain == "light" and e.unique_id in nach_uid}

    async def _konfiguration(self) -> list[dict[str, Any]]:
        pfad = self.hass.config.path("scenes.yaml")

        def lesen() -> Any:
            try:
                return load_yaml(pfad)
            except FileNotFoundError:
                return []

        daten = await self.hass.async_add_executor_job(lesen)
        self.konfig = [s for s in daten or [] if isinstance(s, dict) and s.get("id")
                       and isinstance(s.get("entities"), dict)]
        async_dispatcher_send(self.hass, self.hub.signal("ha_szenen"))  # Auswahl der Tastenaktionen neu
        return self.konfig

    async def abgleichen(self) -> None:
        async with self._lock:
            try:
                konfig = await self._konfiguration()
            except Exception as err:  # noqa: BLE001 – kaputte scenes.yaml: HA meldet das selbst
                _LOGGER.warning("scenes.yaml not readable: %s", err)
                return
            if self.hub.mithoeren:  # nichts in die Aktoren schreiben; Abgleich beim Ausschalten (Sync)
                return
            lampen = self._lampen()
            verw = self.hub.szenen
            fehler: list[str] = []
            gesehen = set()
            arbeit = []  # (ha_id, name, soll): eNet-Teil muss per Funk geändert werden
            for sz in konfig:
                soll = {lampen[e].key: szenenwert(lampen[e], z if isinstance(z, dict) else {"state": z})
                        for e, z in sz["entities"].items() if e in lampen}
                ha_id, name = str(sz["id"]), sz.get("name") or str(sz["id"])
                if not soll:
                    continue
                gesehen.add(ha_id)
                s = verw.ha_szene(ha_id)
                if s and not s.offen and set(s.mitglieder) == set(soll) \
                        and all(wert_gleich(s.werte.get(k), w) for k, w in soll.items()):
                    if s.name != name:
                        s.name = name
                        await verw._speichern()
                    continue
                arbeit.append((ha_id, name, soll))
            weg = [s for s in verw.szenen if s.ha_id and s.ha_id not in gesehen]
            # Solange übertragen wird, laufen diese Szenen (Taste wie Aufruf aus HA) einzeln über HA – der halb
            # geschriebene eNet-Teil wird nicht benutzt (Nutzer 2026-10-30: Aufruf direkt nach dem Speichern)
            self.in_arbeit = {ha_id for ha_id, _, _ in arbeit} | {s.ha_id for s in weg}
            if self.in_arbeit:
                await self._tasten_auffrischen()
            for ha_id, name, soll in arbeit:
                self.hub.szene_in_arbeit = name
                self.hub.programmierung_melden()
                self._aktivitaet(ha_id, name, tr("akt_szene_uebertragen"))
                try:
                    f = await verw.ha_abgleichen(ha_id, name, soll)
                except BridgeError as err:
                    f = [str(err)]
                fehler += [f"{name}: {x}" for x in f]
                self._aktivitaet(ha_id, name, tr("akt_szene_uebertragen_ok") if not f
                                 else tr("akt_szene_uebertragen_teil", fehler="; ".join(f)))
            for s in weg:
                offen = await verw.loeschen(s)  # HA-Szene gelöscht oder ohne eNet-Lampe
                fehler += [tr("links_in_nicht_geloescht", name=s.name, wo=k) for k in offen]
                if offen:
                    self.hub.aktivitaet(tr("akt_szene_entfernt_offen", name=s.name, geraete=", ".join(offen)))
            self.in_arbeit = set()
            self.hub.szene_in_arbeit = None
            self.hub.programmierung_melden()
            await self._tasten_auffrischen()
            if fehler:  # je Szene steht es in ihrer Aktivität; wiederholt beim nächsten Speichern bzw. Start
                _LOGGER.warning("HA-Szenen: %s", "; ".join(fehler))

    @callback
    def _aktivitaet(self, ha_id: str, name: str, text: str) -> None:
        """Eintrag in der Aktivität der HA-Szene."""
        eid = next((st.entity_id for st in self.hass.states.async_all("scene")
                    if str(st.attributes.get("id")) == ha_id), None)
        async_log_entry(self.hass, name, text, DOMAIN, eid)

    # --- Aufruf -------------------------------------------------------------
    @callback
    def _dienst(self, event: Event) -> None:
        """scene.turn_on: eNet-Teil sofort als Szenentelegramm, bevor HA die Lampen einzeln schaltet."""
        if event.data.get("domain") != "scene" or event.data.get("service") != "turn_on" or self.hub.mithoeren:
            return
        daten = event.data.get("service_data") or {}
        ids = daten.get(ATTR_ENTITY_ID) or []
        for entity_id in [ids] if isinstance(ids, str) else ids:
            zustand = self.hass.states.get(entity_id)
            ha_id = zustand.attributes.get("id") if zustand else None
            s = self.hub.szenen.ha_szene(str(ha_id)) if ha_id is not None else None
            if s is None or not s.werte or s.ha_id in self.in_arbeit:
                continue  # ohne (fertigen) eNet-Teil: HA schaltet einzeln
            if s.ha_id in self.per_taste:
                continue  # Server-Taste: die Bridge hat das Szenentelegramm schon gesendet, Sperre steht
            _LOGGER.debug("scene.turn_on %s → eNet scene %s (GA %s, no. %s, values %s)", entity_id, s.name, s.ga,
                          s.nr, s.werte)
            self.hub.szenen.sperren(s)
            self.hass.async_create_background_task(self._aufrufen(s), f"enet Szene {s.name}")

    async def _aufrufen(self, s) -> None:
        try:
            await self.hub.szenen.aufrufen(s)
        except BridgeError as err:  # Sperre aufheben; schon unterdrückte Lampen holt die Nachkorrektur
            for key in s.mitglieder:
                self.hub.szenen.sperre.pop(key, None)
            _LOGGER.warning("Scene %s: eNet call failed (%s), switching lights individually", s.name, err)
            self.hub.szenen.status_abfragen(s)

    # --- Sendertasten mit HA-Szene -------------------------------------------
    async def _tasten_auffrischen(self) -> None:
        """Server-Tasten mit HA-Szene neu setzen: Nummer/GA des eNet-Teils kann sich geändert haben, eine Szene
        kann einen eNet-Teil bekommen oder verloren haben."""
        from . import sender  # sender importiert hub
        esp = self.hub.anlage.get("esp", {})
        vorher = (repr(esp.get("server_tasten")), repr(esp.get("szenen")))
        for g in self.hub.anlage["geraete"]:
            if any(t.get("ha") for t in esp.get("server_tasten", []) if t["sender"] == g["sn"]) \
                    or any(t.get("szene") for t in esp.get("server_tasten", []) if t["sender"] == g["sn"]):
                try:
                    sender.server_tasten_setzen(self.hub, g, sender.aktionen_des_senders(self.hub, g))
                except BridgeError:
                    continue  # Belegung unbekannt
        if (repr(esp.get("server_tasten")), repr(esp.get("szenen"))) != vorher:
            await self.hub.anlage_speichern()
            await self.hub._config_abgleichen(self.hub.bridge.info)

    @callback
    def tastendruck(self, msg: dict[str, Any]) -> None:
        """Server-Taste mit HA-Szene: die Bridge hat den eNet-Teil schon aufgerufen, HA schaltet den Rest."""
        if msg.get("kind") != "server" or not msg.get("press"):
            return
        ga = int(msg.get("ga", -1)) & 0xFF
        t = next((t for t in self.hub.anlage.get("esp", {}).get("server_tasten", [])
                  if t["sender"] == msg.get("sn") and t["ga"] & 0xFF == ga and t.get("ha")), None)
        if t is not None:
            self.hass.async_create_background_task(self._rest_schalten(str(t["ha"])), "enet Szene per Taste")

    async def _rest_schalten(self, ha_id: str) -> None:
        sz = next((s for s in self.konfig if str(s["id"]) == ha_id), None)
        if sz is None:
            return
        s = self.hub.szenen.ha_szene(ha_id)
        if s is None or not s.werte or ha_id in self.in_arbeit:  # ohne (fertigen) eNet-Teil: ganz normal aufrufen
            eid = next((st.entity_id for st in self.hass.states.async_all("scene")
                        if str(st.attributes.get("id")) == ha_id), None)
            if eid:
                await self.hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: eid})
            return
        # Die Szene über scene.turn_on aufrufen, nicht nur den Rest per scene.apply: Sonst bleibt die Szenen-
        # Entität auf „nie aktiviert“ (HA setzt den Zeitpunkt nur bei turn_on). Das Szenentelegramm hat die Bridge
        # schon gesendet: _dienst überspringt es (per_taste), die Einzelbefehle an die eNet-Lampen fängt die Sperre.
        eid = next((st.entity_id for st in self.hass.states.async_all("scene")
                    if str(st.attributes.get("id")) == ha_id), None)
        if eid is None:
            lampen = self._lampen()
            rest = {e: z for e, z in sz["entities"].items() if not (e in lampen and lampen[e].key in s.werte)}
            if rest:
                await self.hass.services.async_call("scene", "apply", {"entities": rest})
            return
        self.hub.szenen.sperren(s)
        self.per_taste.add(ha_id)
        try:
            await self.hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: eid}, blocking=True)
        finally:
            self.per_taste.discard(ha_id)

    # --- Umwandeln: eNet-Szenen (Import) → HA-Szenen ---------------------------
    async def umwandeln(self) -> None:
        """eNet-Szenen und Bridge-Szenen aus dem Import als HA-Szenen anlegen.

        eNet-Szenen: Werte aus den Aktoren gelesen; die eNet-Szene bleibt in den Aktoren und wird zum eNet-Teil, die
        neue HA-Szene übernimmt die Entitäts-ID der alten eNet-Szenen-Entität. Fehlt ein Wert (Funk), bleibt die
        Szene bis zum nächsten Start unverändert.
        Bridge-Szenen (Verknüpfungen des eNet Servers mit Einzelbefehlen, z. B. „Licht an“): Zustände aus den
        Befehlen; den eNet-Teil legt danach der Abgleich an, die Tasten rufen dann „Szene: …“ auf."""
        async with self._lock:
            verw = self.hub.szenen
            kanaele = {k.key: k for k in self.hub.kanaele}
            entity = {k.key: e for e, k in self._lampen().items()}
            neu = []
            for i, s in enumerate([s for s in verw.szenen if s.art != "ha"]):
                werte: dict[str, int] = {}
                for key in s.mitglieder:
                    k = kanaele.get(key)
                    if k is None or key not in entity:
                        continue
                    for _ in range(3):
                        try:
                            r = await self.hub.bridge.request("prop_read", ia=k.ia, obj=k.obj, pid=79,
                                                              start=4 + s.nr, count=1)
                            werte[key] = bytes.fromhex(r.get("value", ""))[0]
                            break
                        except (BridgeError, IndexError) as err:
                            _LOGGER.debug("Scene %s, %s: value not read (%s)", s.name, key, err)
                if len(werte) < len([m for m in s.mitglieder if m in entity]):
                    _LOGGER.warning("Scene %s: not all scene values readable – converting on next start", s.name)
                    continue
                ha_id = f"{int(time.time() * 1000) + i}"
                eintrag = {"id": ha_id, "name": s.name,
                           "entities": {entity[key]: zustand_aus_wert(kanaele[key], w) for key, w in werte.items()},
                           "metadata": {}}
                neu.append((s, ha_id, eintrag, werte))
            bridge = self._bridge_szenen(kanaele, entity)
            if not neu and not bridge:
                return
            reg = er.async_get(self.hass)
            pfad = self.hass.config.path("scenes.yaml")

            def schreiben() -> None:
                try:
                    daten = load_yaml(pfad) or []
                except FileNotFoundError:
                    daten = []
                daten += [e for _, _, e, _ in neu] + [e for _, e in bridge]
                write_utf8_file_atomic(pfad, dump(daten))

            await self.hass.async_add_executor_job(schreiben)
            for s, ha_id, _, werte in neu:
                alt = reg.async_get_entity_id("scene", DOMAIN, f"{self.hub.entry_id}_szene_{s.id}")
                knopf = reg.async_get_entity_id("button", DOMAIN, f"{self.hub.entry_id}_szene_{s.id}_speichern")
                for eid in (alt, knopf):
                    if eid:
                        reg.async_remove(eid)
                if alt:  # Entitäts-ID der alten eNet-Szene für die HA-Szene vormerken
                    reg.async_get_or_create("scene", "homeassistant", ha_id, suggested_object_id=alt.split(".", 1)[1])
                s.art, s.ha_id, s.werte, s.offen = "ha", ha_id, werte, []
            await verw._speichern()
            if bridge:  # Tasten auf die neue HA-Szene umstellen, Bridge-Szene entfällt
                esp = self.hub.anlage["esp"]
                for name, eintrag in bridge:
                    for t in esp.get("server_tasten", []):
                        if t.get("szene") == name:
                            t.pop("szene")
                            t["ha"] = eintrag["id"]
                    del esp["szenen"][name]
                await self.hub.anlage_speichern()
                await self.hub._config_abgleichen(self.hub.bridge.info)
            namen = [s.name for s, *_ in neu] + [n for n, _ in bridge]
            _LOGGER.info("Als HA-Szenen angelegt: %s", ", ".join(namen))
        await self.hass.services.async_call("scene", "reload", blocking=True)  # löst den Abgleich aus
        self.hub.aktivitaet(tr("akt_szenen_uebernommen", namen=", ".join(namen)))

    def _bridge_szenen(self, kanaele: dict[str, Kanal], entity: dict[str, str]) -> list[tuple[str, dict]]:
        """Bridge-Szenen, die nur Einzelbefehle an bekannte Lampen enthalten → (Name, scenes.yaml-Eintrag).
        Handle = Kanal-Offset + GO (2 Schalten 00/01, 4 Dimmwert 0–255; Dimmwert 0 = aus)."""
        out = []
        for i, (name, aktionen) in enumerate(self.hub.anlage.get("esp", {}).get("szenen", {}).items()):
            zustaende: dict[str, dict] = {}
            for a in aktionen:
                if a["ia"] == "0.0.0":
                    break  # ruft eine eNet-Szene auf: bleibt Bridge-Szene
                treffer = None
                for k in kanaele.values():
                    off = next((x.get("offset", 0) for x in k.geraet.get("kanaele", []) if x["nr"] == k.nr), 0)
                    if k.ia == a["ia"] and a["handle"] - off in (2, 4):
                        treffer = k, a["handle"] - off
                        break
                if treffer is None or treffer[0].key not in entity:
                    break
                k, go = treffer
                wert = int(a["wert"], 16)
                zustaende[entity[k.key]] = zustand_aus_wert(k, 255 if go == 2 and wert else wert)
            else:
                if zustaende:
                    out.append((name, {"id": f"{int(time.time() * 1000) + 500 + i}", "name": name,
                                       "entities": zustaende, "metadata": {}}))
        return out

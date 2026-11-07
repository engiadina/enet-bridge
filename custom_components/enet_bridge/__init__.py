"""eNet-Bridge: Gira/Jung eNet-Funk ohne eNet Server (ESP32 + CC1101 an USB)."""
from __future__ import annotations

import functools
import re
import logging
import os

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import einlernen, sender
from .bridge import Bridge, BridgeError, fremdes_funkmodul
from .anlage import AnlageSpeicher
from .const import CONF_ANLAGE, CONF_PORT, DEFAULT_DEFINITIONEN, DOMAIN, MANUFACTURER
from .hub import Hub
from .texte import tr
from . import texte
from .iex import produkt_kennung
from .params import lade_definitionen

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.EVENT, Platform.LIGHT, Platform.NUMBER,
             Platform.SCENE, Platform.SELECT, Platform.SENSOR, Platform.SWITCH, Platform.UPDATE]

_LOGGER = logging.getLogger(__name__)

SCHEMA_SZENE_ANLEGEN = vol.Schema({
    vol.Required("name"): cv.string,
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional("lernen", default=True): cv.boolean,
})
SCHEMA_SZENE_LOESCHEN = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id})
SCHEMA_EINLERNEN = vol.Schema({vol.Optional("name"): cv.string})
SCHEMA_ENTFERNEN = vol.Schema({vol.Required("device_id"): cv.string})
SCHEMA_VERKNUEPFUNGEN = vol.Schema({vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string])})

type EnetConfigEntry = ConfigEntry[Hub]


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry) -> bool:
    texte.setzen(hass.config.language)
    if fremdes_funkmodul(entry.data[CONF_PORT]):
        raise ConfigEntryError(tr("fremdes_funkmodul_start", port=entry.data[CONF_PORT]))
    speicher = AnlageSpeicher(hass, entry.entry_id)
    anlage = await speicher.laden(entry)
    # Gerätedefinitionen (tools/katalog.py) liegen neben der Anlage bzw. in /config/enet
    pfad = (os.path.join(os.path.dirname(entry.data[CONF_ANLAGE]), "definitionen.json")
            if CONF_ANLAGE in entry.data else DEFAULT_DEFINITIONEN)
    # mitgelieferte Definitionen (alle Katalog-Typen), eine lokale Datei ergänzt bzw. überschreibt sie
    definitionen = await hass.async_add_executor_job(
        lade_definitionen, os.path.join(os.path.dirname(__file__), "definitionen.json"))
    definitionen |= await hass.async_add_executor_job(lade_definitionen, pfad)
    hub = Hub(hass, entry.entry_id, Bridge(hass.loop, entry.data[CONF_PORT]), anlage, definitionen=definitionen,
              speicher=speicher)
    entry.runtime_data = hub

    bridge_dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"bridge_{entry.entry_id}")},
        manufacturer="wekomm engineering GmbH",
        model="eNet-Bridge (ESP32 + CC1101)",
        name="eNet-Bridge",
    )
    hub.bridge_device_id = bridge_dev.id
    # eNet-Geräte vorab anlegen: Kanal-Geräte mehrkanaliger Aktoren hängen per via_device_id daran
    for g in anlage["geraete"]:
        hub.geraet_device_id[g["sn"]] = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, **geraet_info(hub, g)).id
    await hub.szenen.laden()
    _verwaiste_tasten_entfernen(hass, hub)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start()

    if not hass.services.has_service(DOMAIN, "release"):
        async def _release(call: ServiceCall) -> None:
            for e in hass.config_entries.async_loaded_entries(DOMAIN):
                await hass.async_add_executor_job(e.runtime_data.bridge.release)

        async def _reconnect(call: ServiceCall) -> None:
            for e in hass.config_entries.async_loaded_entries(DOMAIN):
                e.runtime_data.bridge.reconnect()

        hass.services.async_register(DOMAIN, "release", _release)
        hass.services.async_register(DOMAIN, "reconnect", _reconnect)
        hass.services.async_register(DOMAIN, "geraet_einlernen", functools.partial(_einlernen, hass),
                                     schema=SCHEMA_EINLERNEN)
        hass.services.async_register(DOMAIN, "geraet_entfernen", functools.partial(_entfernen, hass),
                                     schema=SCHEMA_ENTFERNEN)
        hass.services.async_register(DOMAIN, "sender_programmieren", functools.partial(_sender_programmieren, hass),
                                     schema=SCHEMA_ENTFERNEN)
        hass.services.async_register(DOMAIN, "verknuepfungen_lesen", functools.partial(_verknuepfungen_lesen, hass),
                                     schema=SCHEMA_VERKNUEPFUNGEN, supports_response=SupportsResponse.OPTIONAL)
        hass.services.async_register(DOMAIN, "szene_anlegen", functools.partial(_szene_anlegen, hass),
                                     schema=SCHEMA_SZENE_ANLEGEN)
        hass.services.async_register(DOMAIN, "szene_loeschen", functools.partial(_szene_loeschen, hass),
                                     schema=SCHEMA_SZENE_LOESCHEN)
    return True


def _verwaiste_tasten_entfernen(hass: HomeAssistant, hub: Hub) -> None:
    """Tasten-Entitäten (Ereignis, „Taste k: Aktion“, „Wippe n: Lampe i“) entfernen, die es nach dem Umbelegen nicht mehr gibt."""
    bleiben = {f"{t.geraet['sn']}_taste_{t.nr}" for t in hub.tasten}
    unbekannt = set()  # Belegung nicht lesbar: deren Aktions-Auswahl nicht anfassen
    for g in hub.anlage["geraete"]:
        if g.get("rolle") != "sender":
            continue
        try:
            bel = sender.geplante_belegung(g)
        except BridgeError:
            unbekannt.add(g["sn"])
            continue
        bleiben |= {f"{g['sn']}_taste{k}_aktion" for n, b in bel.items() if b["art"] == "tasten"
                    for k in (2 * n - 1, 2 * n)}
        bleiben |= {f"{g['sn']}_wippe{n}_lampe{i}" for n in range(1, sender.wippen_anzahl(hub, g) + 1)
                    for i in range(1, sender.lampen_auswahlen(g, n) + 1)}
    reg = er.async_get(hass)
    if alt := reg.async_get_entity_id("button", DOMAIN, f"{hub.entry_id}_kalibrieren"):
        reg.async_remove(alt)  # Knopf „Funk kalibrieren“ gibt es seit 0.9.6 nicht mehr (automatisch)
    for e in er.async_entries_for_config_entry(reg, hub.entry_id):
        uid = e.unique_id
        if e.domain == "event" and "_taste_" in uid or (
                e.domain == "select" and re.fullmatch(r"[0-9a-f]+_(taste\d+_aktion|wippe\d+_lampe\d+)", uid)
                and uid.split("_")[0] not in unbekannt):
            if uid not in bleiben:
                _LOGGER.info("Entity %s removed (button no longer assigned)", e.entity_id)
                reg.async_remove(e.entity_id)


async def _einlernen(hass: HomeAssistant, call: ServiceCall) -> None:
    """Gerät im Lernmodus aufnehmen (eine Anlage: die erste geladene)."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(tr("keine_anlage"))
    hub = entries[0].runtime_data
    try:
        await einlernen.aufnehmen(hub, call.data.get("name"))
    except BridgeError as err:
        hub.aktivitaet(tr("akt_einlernen_fehlgeschlagen", err=err))
        raise HomeAssistantError(tr("einlernen_fehler", err=err)) from err
    finally:
        hass.config_entries.async_schedule_reload(hub.entry_id)


async def _entfernen(hass: HomeAssistant, call: ServiceCall) -> None:
    """eNet-Gerät in den Werkszustand versetzen und aus der Anlage nehmen."""
    dev = dr.async_get(hass).async_get(call.data["device_id"])
    sn = next((i[1] for i in dev.identifiers if i[0] == DOMAIN), None) if dev else None
    for e in hass.config_entries.async_loaded_entries(DOMAIN):
        hub = e.runtime_data
        geraet = next((g for g in hub.anlage["geraete"] if g["sn"] == sn), None)
        if geraet is None:
            continue
        try:
            await einlernen.entfernen(hub, geraet)
        except BridgeError as err:
            raise HomeAssistantError(tr("entfernen_fehler", err=err)) from err
        reg = dr.async_get(hass)
        for d in dr.async_entries_for_config_entry(reg, hub.entry_id):
            if any(i[0] == DOMAIN and i[1].split("_")[0] == geraet["sn"] for i in d.identifiers):
                reg.async_remove_device(d.id)
        hass.config_entries.async_schedule_reload(hub.entry_id)
        return
    raise ServiceValidationError(tr("kein_geraet"))


def _geraet_fuer(hass: HomeAssistant, device_id: str) -> tuple[Hub, dict, dr.DeviceEntry]:
    dev = dr.async_get(hass).async_get(device_id)
    sn = next((i[1] for i in dev.identifiers if i[0] == DOMAIN), None) if dev else None
    for e in hass.config_entries.async_loaded_entries(DOMAIN):
        if g := next((g for g in e.runtime_data.anlage["geraete"] if g["sn"] == sn), None):
            return e.runtime_data, g, dev
    raise ServiceValidationError(tr("kein_enet_geraet"))


async def _sender_programmieren(hass: HomeAssistant, call: ServiceCall) -> None:
    """Sender wecken und mit seiner Belegung aus der Anlage komplett neu programmieren."""
    hub, g, _ = _geraet_fuer(hass, call.data["device_id"])
    if g.get("rolle") != "sender":
        raise ServiceValidationError(tr("kein_sender", name=g["name"]))
    try:
        await sender.programmieren(hub, g)
    except BridgeError as err:
        raise HomeAssistantError(f"{g['name']}: {err}") from err
    hub.neu_laden_nach_funk()  # Tastenzuordnung (GA-Basis, Belegung) neu, erst nach allen Funk-Sitzungen


async def _verknuepfungen_lesen(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Link-Tabellen von Aktoren (ohne Angabe: alle) lesen und bewerten; auffällige Einträge kommen unter
    Reparaturen. Antwort: je Aktor die angelegten Verknüpfungen (ohne Werkseinträge) mit Bewertung."""
    if ids := call.data.get("device_id"):
        ziele = [_geraet_fuer(hass, i)[:2] for i in ids]
    else:
        ziele = [(e.runtime_data, g) for e in hass.config_entries.async_loaded_entries(DOMAIN)
                 for g in e.runtime_data.anlage["geraete"]]
    antwort: dict = {}
    for hub, g in ziele:
        if not any(k.geraet is g for k in hub.kanaele):
            if ids:
                raise ServiceValidationError(tr("kein_aktor", name=g["name"]))
            continue
        if hub.mithoeren:
            raise ServiceValidationError(tr("nur_mithoeren_lesen"))
        try:
            ergebnis = await hub.pruefung.geraet_links_lesen(g)
        except BridgeError as err:
            antwort[g["name"]] = {"ia": g["ia"], "fehler": str(err)}
            continue
        antwort[g["name"]] = {
            "ia": g["ia"],
            "kanaele": {ch: {"werkseintraege": sum(e["art"] == "werk" for e in ee),
                             "verknuepfungen": [{k: v for k, v in e.items() if k != "typ"}
                                                for e in ee if e["art"] != "werk"]}
                        for ch, ee in ergebnis.items()},
            "auffaellig": len(g["links"]["auffaellig"]),
        }
    return antwort


def _hub_fuer(hass: HomeAssistant, entity_id: str, domain: str) -> tuple[Hub, er.RegistryEntry]:
    e = er.async_get(hass).async_get(entity_id)
    if e is None or e.platform != DOMAIN or e.domain != domain or e.config_entry_id is None:
        raise ServiceValidationError(tr("nicht_bridge_entitaet", entity_id=entity_id, domain=domain))
    entry = hass.config_entries.async_get_entry(e.config_entry_id)
    if entry is None or not hasattr(entry, "runtime_data"):
        raise ServiceValidationError(tr("bridge_nicht_geladen", entity_id=entity_id))
    return entry.runtime_data, e


async def _szene_anlegen(hass: HomeAssistant, call: ServiceCall) -> None:
    """Neue eNet-Szene aus eNet-Lampen; mit lernen = true gilt der aktuelle Zustand als Szenenwert."""
    hub, kanaele = None, []
    for eid in call.data[ATTR_ENTITY_ID]:
        h, e = _hub_fuer(hass, eid, "light")
        if hub is not None and h is not hub:
            raise ServiceValidationError(tr("lampen_eine_bridge"))
        hub = h
        k = next((k for k in h.kanaele if f"{k.geraet['sn']}_{k.nr}" == e.unique_id), None)
        if k is None:
            raise ServiceValidationError(tr("kanal_fehlt", entity_id=eid))
        kanaele.append(k)
    try:
        s = await hub.szenen.anlegen(call.data["name"], kanaele)
        if call.data["lernen"]:
            await hub.szenen.lernen(s)
    except BridgeError as err:
        raise HomeAssistantError(tr("szene_fehler", name=call.data["name"], err=err)) from err
    _LOGGER.info("eNet scene %s created (no. %d, GA %d/%d)", s.name, s.nr, s.ga, s.ga_status)
    hass.config_entries.async_schedule_reload(hub.entry_id)


async def _szene_loeschen(hass: HomeAssistant, call: ServiceCall) -> None:
    """eNet-Szene löschen: Links in allen Mitgliedern entfernen, Entitäten entfernen."""
    hub, e = _hub_fuer(hass, call.data[ATTR_ENTITY_ID], "scene")
    s = hub.szenen.get(e.unique_id.split("_szene_", 1)[-1])
    if s is None:
        raise ServiceValidationError(tr("szene_unbekannt", entity_id=e.entity_id))
    try:
        offen = await hub.szenen.loeschen(s)
    except BridgeError as err:
        raise HomeAssistantError(tr("szene_fehler", name=s.name, err=err)) from err
    reg = er.async_get(hass)
    reg.async_remove(e.entity_id)
    if btn := reg.async_get_entity_id("button", DOMAIN, f"{e.unique_id}_speichern"):
        reg.async_remove(btn)
    if offen:
        hub.aktivitaet(tr("akt_szene_geloescht_offen", name=s.name, geraete=", ".join(offen)))
    hass.config_entries.async_schedule_reload(hub.entry_id)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: EnetConfigEntry,
                                           device: dr.DeviceEntry) -> bool:
    """„Gerät löschen“ (Mülleimer): eNet-Gerät auf Werkszustand setzen, alles Zugehörige aufräumen und aus der Anlage
    nehmen (wie „Konfigurieren → Gerät entfernen“, nur ohne Übersicht). Nicht gelöscht wird, wenn die Bridge fehlt
    oder ein Sender nicht aufwacht – sonst stünde ein Gerät im Funk, das HA nicht mehr kennt.
    Meldungen fest auf Deutsch: HA übersetzt Fehlermeldungen in der Serversprache, nicht in der Oberfläche."""
    kennung = next((i[1] for i in device.identifiers if i[0] == DOMAIN), None) or ""
    hub = entry.runtime_data if entry.state is ConfigEntryState.LOADED else None
    if kennung.startswith(("bridge_", "szenen_")):
        raise HomeAssistantError(tr("loeschen_bridge_szenen"))
    sn, _, kanal = kennung.partition("_")  # Kanal-Geräte mehrkanaliger Aktoren: „SN_Kanal“
    geraet = next((g for g in hub.anlage["geraete"] if g["sn"] == sn), None) if hub else None
    if hub and geraet is None:
        return True  # verwaist (nicht mehr in der Anlage)
    if hub is None or not hub.bridge.connected:
        raise HomeAssistantError(tr("loeschen_nicht_verbunden"))
    if kanal:
        raise HomeAssistantError(tr("loeschen_kanal", name=geraet["name"]))
    try:
        await einlernen.entfernen(hub, geraet)
    except BridgeError as err:
        raise HomeAssistantError(tr("nicht_geloescht", name=geraet["name"], err=err)) from err
    reg = dr.async_get(hass)
    for d in dr.async_entries_for_config_entry(reg, entry.entry_id):  # Kanal-Geräte mit entfernen
        if d.id != device.id and any(i[0] == DOMAIN and i[1].split("_")[0] == sn for i in d.identifiers):
            reg.async_remove_device(d.id)
    hass.config_entries.async_schedule_reload(entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnetConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
    return ok


HERSTELLER = {"gira": "Gira", "jung": "Jung"}
# Jung-Artikelnummern für Geräte, die der Katalog nur unter Gira führt (Jung meldet dieselbe Kennung mit …3)
JUNG_ARTIKEL = {"DVT_SF1S": "FM FS 1 S"}


def _hersteller(hub: Hub, geraet: dict) -> tuple[str, str | None]:
    """Hersteller und Artikelnummer aus den Gerätedefinitionen (sonst allgemein eNet, Typkennung). Maßgeblich ist
    die vom Gerät gemeldete Kennung: letzte Ziffer 3 = Jung, auch wenn der Katalogeintrag von Gira stammt."""
    typ = geraet.get("typ", "")
    d = hub.definitionen.get(typ, {})
    hersteller, artikel = d.get("hersteller") or "", d.get("artikel")
    if produkt_kennung(geraet.get("produkt") or "").endswith("3") and hersteller != "jung":
        hersteller, artikel = "jung", JUNG_ARTIKEL.get(typ)
    return HERSTELLER.get(hersteller, MANUFACTURER), artikel or typ


def geraet_info(hub: Hub, geraet: dict) -> dr.DeviceInfo:
    """HA-Gerät für ein eNet-Gerät (über die Bridge)."""
    hersteller, artikel = _hersteller(hub, geraet)
    return dr.DeviceInfo(
        identifiers={(DOMAIN, geraet["sn"])},
        name=geraet["name"],
        manufacturer=hersteller,
        model=hub.modell(geraet),
        model_id=artikel,
        serial_number=geraet["sn"],
        suggested_area=geraet.get("raum"),  # optional in anlage.json; gilt nur beim ersten Anlegen
        via_device_id=hub.bridge_device_id,
    )


def kanal_info(hub: Hub, kanal) -> dr.DeviceInfo:
    """HA-Gerät für einen Kanal: bei einkanaligen Aktoren das Gerät selbst, sonst ein Child-Gerät „Kanal n“ des
    Aktors, damit jeder Kanal in der Bedienung als eigenständige Lampe erscheint."""
    g = kanal.geraet
    if sum(1 for k in hub.kanaele if k.geraet is g) == 1:
        return geraet_info(hub, g)
    return _teil_info(hub, g, f"{g['sn']}_{kanal.nr}", f"Kanal {kanal.nr}")


def wippe_info(hub: Hub, geraet: dict, n: int) -> dr.DeviceInfo:
    """HA-Gerät für Wippe n eines Senders: bei einer Wippe der Sender selbst, sonst ein Child-Gerät „Wippe n“,
    damit Betriebsart, Lampen bzw. Tastenaktionen jeder Wippe auf einer eigenen Seite stehen."""
    if sender.wippen_anzahl(hub, geraet) <= 1:
        return geraet_info(hub, geraet)
    return _teil_info(hub, geraet, f"{geraet['sn']}_wippe{n}", tr("wippe_geraet", n=n))


def _teil_info(hub: Hub, geraet: dict, kennung: str, name: str) -> dr.ChildDeviceInfo:
    """Child-Gerät (HA ab 2026.10): Teil des eNet-Geräts, auf dessen Seite aufgeführt; der Name wird in HA
    mit dem des Geräts zusammengesetzt, der Bereich vom Gerät übernommen."""
    return dr.ChildDeviceInfo(identifiers={(DOMAIN, kennung)}, name=name,
                              parent_device_id=hub.geraet_device_id[geraet["sn"]])


def szenen_info(hub: Hub) -> dr.DeviceInfo:
    """Sammel-Gerät für die eNet-Szenen (Szenen-Entitäten und „speichern“-Knöpfe)."""
    return dr.DeviceInfo(identifiers={(DOMAIN, f"szenen_{hub.entry_id}")}, name=tr("geraet_szenen"),
                         manufacturer=MANUFACTURER, model="KNX-Szenen in den Aktoren",
                         via_device_id=hub.bridge_device_id)


def bridge_info(hub: Hub) -> dr.DeviceInfo:
    return dr.DeviceInfo(identifiers={(DOMAIN, f"bridge_{hub.entry_id}")},
                         sw_version=hub.bridge.info.get("fw"))

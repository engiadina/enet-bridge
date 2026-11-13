"""eNet-Bridge: Gira/Jung eNet radio without eNet Server (ESP32 + CC1101 on USB)."""
from __future__ import annotations

import functools
import re
import logging
import os
import shutil

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import pair, ha_rocker, sender
from . import visibility
from .bridge import Bridge, BridgeError, foreign_radio
from .installation import InstallationStore
from .const import CONF_INSTALLATION, CONF_PORT, DEFAULT_DEFINITIONS, DOMAIN, MANUFACTURER
from .hub import Hub
from .texts import tr
from . import texts
from .iex import product_ident
from .params import load_definitions
from . import layouts

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.COVER, Platform.EVENT, Platform.LIGHT, Platform.NUMBER,
             Platform.SCENE, Platform.SELECT, Platform.SENSOR, Platform.SWITCH, Platform.UPDATE]

_LOGGER = logging.getLogger(__name__)

SCHEMA_SCENE_CREATE = vol.Schema({
    vol.Required("name"): cv.string,
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional("lernen", default=True): cv.boolean,
})
SCHEMA_SCENE_DELETE = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id})
SCHEMA_PAIR = vol.Schema({vol.Optional("name"): cv.string})
SCHEMA_REMOVE = vol.Schema({vol.Required("device_id"): cv.string})
SCHEMA_LINKS = vol.Schema({vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string])})

type EnetConfigEntry = ConfigEntry[Hub]


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry) -> bool:
    texts.set_language(hass.config.language)
    if foreign_radio(entry.data[CONF_PORT]):
        raise ConfigEntryError(tr("fremdes_funkmodul_start", port=entry.data[CONF_PORT]))
    store = InstallationStore(hass, entry.entry_id)
    installation = await store.laden(entry)
    # device definitions (tools/katalog.py) live next to the installation file or in /config/enet
    path_ = (os.path.join(os.path.dirname(entry.data[CONF_INSTALLATION]), "definitionen.json")
            if CONF_INSTALLATION in entry.data else DEFAULT_DEFINITIONS)
    # bundled definitions (all catalog types); a local file supplements or overrides them
    definitions = await hass.async_add_executor_job(
        load_definitions, os.path.join(os.path.dirname(__file__), "definitionen.json"))
    definitions |= await hass.async_add_executor_job(load_definitions, path_)
    channel_layouts = await hass.async_add_executor_job(
        layouts.load, os.path.join(os.path.dirname(__file__), "definitionen.json"))
    layouts.set_layouts(channel_layouts | await hass.async_add_executor_job(layouts.load, path_))
    await hass.async_add_executor_job(_install_blueprints, hass.config.path("blueprints", "automation", DOMAIN))
    hub = Hub(hass, entry.entry_id, Bridge(hass.loop, entry.data[CONF_PORT]), installation, definitions=definitions,
              store=store)
    # a running travel time measurement survives the reloads it triggers (calibration.py)
    hub.calibrations = hass.data.setdefault(f"{DOMAIN}_einmessen", {}).setdefault(entry.entry_id, {})
    entry.runtime_data = hub

    bridge_dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"bridge_{entry.entry_id}")},
        manufacturer="wekomm engineering GmbH",
        model="eNet-Bridge (ESP32 + CC1101)",
        name="eNet-Bridge",
    )
    hub.bridge_device_id = bridge_dev.id
    # create eNet devices up front: channel devices of multi-channel actuators attach to them via via_device_id
    for g in installation["geraete"]:
        hub.device_ids[g["sn"]] = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, **device_info_for(hub, g)).id
    await hub.scenes.laden()
    await hub.params.laden()  # stored parameter values decide which settings exist (visibility.py)
    _remove_orphaned_keys(hass, hub)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await visibility.sync_registry(hass, hub)
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
        hass.services.async_register(DOMAIN, "geraet_einlernen", functools.partial(_pair, hass),
                                     schema=SCHEMA_PAIR)
        hass.services.async_register(DOMAIN, "geraet_entfernen", functools.partial(_remove, hass),
                                     schema=SCHEMA_REMOVE)
        hass.services.async_register(DOMAIN, "sender_programmieren", functools.partial(_program_sender, hass),
                                     schema=SCHEMA_REMOVE)
        hass.services.async_register(DOMAIN, "verknuepfungen_lesen", functools.partial(_read_links, hass),
                                     schema=SCHEMA_LINKS, supports_response=SupportsResponse.OPTIONAL)
        hass.services.async_register(DOMAIN, "szene_anlegen", functools.partial(_scene_create, hass),
                                     schema=SCHEMA_SCENE_CREATE)
        hass.services.async_register(DOMAIN, "szene_loeschen", functools.partial(_scene_delete, hass),
                                     schema=SCHEMA_SCENE_DELETE)
    return True


def _install_blueprints(target: str) -> None:
    """Copy bundled blueprints to blueprints/automation/enet_bridge – only missing ones; an existing file (possibly
    changed by the user) is kept. HA reads blueprints only when the list is opened, no reload needed."""
    source_ = os.path.join(os.path.dirname(__file__), "blueprints")
    try:
        for name in sorted(os.listdir(source_)):
            path_ = os.path.join(target, name)
            if name.endswith(".yaml") and not os.path.exists(path_):
                os.makedirs(target, exist_ok=True)
                shutil.copyfile(os.path.join(source_, name), path_)
                _LOGGER.info("Blueprint %s installed", path_)
    except OSError as err:
        _LOGGER.warning("Blueprints not installed: %s", err)


def _remove_orphaned_keys(hass: HomeAssistant, hub: Hub) -> None:
    """Remove key entities (event, "Key k: action", "Rocker n: lamp i") that no longer exist after reassignment."""
    remain = {f"{t.enet_device['sn']}_taste_{t.nr}" for t in hub.keys_}
    unknown = set()  # assignment not readable: don't touch their action selects
    for g in hub.installation["geraete"]:
        if g.get("rolle") != "sender":
            continue
        try:
            assigned = sender.planned_assignment(g)
        except BridgeError:
            unknown.add(g["sn"])
            continue
        remain |= {f"{g['sn']}_taste{k}_aktion" for n, b in assigned.items() if b["art"] == "tasten"
                    for k in (2 * n - 1, 2 * n)}
        remain |= {f"{g['sn']}_wippe{n}_lampe{i}" for n in range(1, sender.rocker_count(hub, g) + 1)
                    for i in range(1, sender.lamp_choices(g, n) + 1)}
        if sender.pending(g):
            remain |= {f"{g['sn']}_wippe{n}_sender_programmieren" for n in range(1, sender.rocker_count(hub, g) + 1)}
        remain |= {f"{g['sn']}_wippe{n}_abdimmen" for n, b in assigned.items()
                    if b["art"] == "wippe" and ha_rocker.dim_down_useful(hub, b)}
    reg = er.async_get(hass)
    if old := reg.async_get_entity_id("button", DOMAIN, f"{hub.entry_id}_kalibrieren"):
        reg.async_remove(old)  # the "calibrate radio" button no longer exists since 0.9.6 (automatic)
    for e in er.async_entries_for_config_entry(reg, hub.entry_id):
        uid = e.unique_id
        if e.domain == "event" and "_taste_" in uid or (
                e.domain == "button" and re.fullmatch(r"[0-9a-f]+_wippe\d+_sender_programmieren", uid)) or (
                e.domain == "select" and re.fullmatch(r"[0-9a-f]+_(taste\d+_aktion|wippe\d+_lampe\d+|wippe\d+_abdimmen)", uid)
                and uid.split("_")[0] not in unknown):
            if uid not in remain:
                _LOGGER.info("Entity %s removed (button no longer assigned)", e.entity_id)
                reg.async_remove(e.entity_id)


async def _pair(hass: HomeAssistant, call: ServiceCall) -> None:
    """Add a device in learning mode (one installation: the first loaded one)."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(tr("keine_anlage"))
    hub = entries[0].runtime_data
    try:
        await pair.add(hub, call.data.get("name"))
    except BridgeError as err:
        hub.activity(tr("akt_einlernen_fehlgeschlagen", err=err))
        raise HomeAssistantError(tr("einlernen_fehler", err=err)) from err
    finally:
        hass.config_entries.async_schedule_reload(hub.entry_id)


async def _remove(hass: HomeAssistant, call: ServiceCall) -> None:
    """Factory-reset an eNet device and remove it from the installation."""
    dev = dr.async_get(hass).async_get(call.data["device_id"])
    sn = next((i[1] for i in dev.identifiers if i[0] == DOMAIN), None) if dev else None
    for e in hass.config_entries.async_loaded_entries(DOMAIN):
        hub = e.runtime_data
        enet_device = next((g for g in hub.installation["geraete"] if g["sn"] == sn), None)
        if enet_device is None:
            continue
        try:
            await pair.remove(hub, enet_device)
        except BridgeError as err:
            raise HomeAssistantError(tr("entfernen_fehler", err=err)) from err
        reg = dr.async_get(hass)
        for d in dr.async_entries_for_config_entry(reg, hub.entry_id):
            if any(i[0] == DOMAIN and i[1].split("_")[0] == enet_device["sn"] for i in d.identifiers):
                reg.async_remove_device(d.id)
        hass.config_entries.async_schedule_reload(hub.entry_id)
        return
    raise ServiceValidationError(tr("kein_geraet"))


def _device_for(hass: HomeAssistant, device_id: str) -> tuple[Hub, dict, dr.DeviceEntry]:
    dev = dr.async_get(hass).async_get(device_id)
    sn = next((i[1] for i in dev.identifiers if i[0] == DOMAIN), None) if dev else None
    for e in hass.config_entries.async_loaded_entries(DOMAIN):
        if g := next((g for g in e.runtime_data.installation["geraete"] if g["sn"] == sn), None):
            return e.runtime_data, g, dev
    raise ServiceValidationError(tr("kein_enet_geraet"))


async def _program_sender(hass: HomeAssistant, call: ServiceCall) -> None:
    """Wake a transmitter and fully reprogram it with its assignment from the installation."""
    hub, g, _ = _device_for(hass, call.data["device_id"])
    if g.get("rolle") != "sender":
        raise ServiceValidationError(tr("kein_sender", name=g["name"]))
    try:
        await sender.program(hub, g)
    except BridgeError as err:
        raise HomeAssistantError(f"{g['name']}: {err}") from err
    hub.reload_after_radio()  # rebuild key mapping (GA base, assignment), only after all radio sessions


async def _read_links(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Read and assess the link tables of actuators (none given: all); suspicious entries go to Repairs.
    Response: per actuator the created links (without factory entries) with assessment."""
    if ids := call.data.get("device_id"):
        targets = [_device_for(hass, i)[:2] for i in ids]
    else:
        targets = [(e.runtime_data, g) for e in hass.config_entries.async_loaded_entries(DOMAIN)
                 for g in e.runtime_data.installation["geraete"]]
    answer: dict = {}
    for hub, g in targets:
        if not any(k.enet_device is g for k in hub.channels_):
            if ids:
                raise ServiceValidationError(tr("kein_aktor", name=g["name"]))
            continue
        if hub.listen_only:
            raise ServiceValidationError(tr("nur_mithoeren_lesen"))
        try:
            outcome = await hub.check.read_device_links(g)
        except BridgeError as err:
            answer[g["name"]] = {"ia": g["ia"], "fehler": str(err)}
            continue
        answer[g["name"]] = {
            "ia": g["ia"],
            "kanaele": {ch: {"werkseintraege": sum(e["art"] == "werk" for e in ee),
                             "verknuepfungen": [{k: v for k, v in e.items() if k != "typ"}
                                                for e in ee if e["art"] != "werk"]}
                        for ch, ee in outcome.items()},
            "auffaellig": len(g["links"]["auffaellig"]),
        }
    return answer


def _hub_for(hass: HomeAssistant, entity_id: str, domain: str) -> tuple[Hub, er.RegistryEntry]:
    e = er.async_get(hass).async_get(entity_id)
    if e is None or e.platform != DOMAIN or e.domain != domain or e.config_entry_id is None:
        raise ServiceValidationError(tr("nicht_bridge_entitaet", entity_id=entity_id, domain=domain))
    entry = hass.config_entries.async_get_entry(e.config_entry_id)
    if entry is None or not hasattr(entry, "runtime_data"):
        raise ServiceValidationError(tr("bridge_nicht_geladen", entity_id=entity_id))
    return entry.runtime_data, e


async def _scene_create(hass: HomeAssistant, call: ServiceCall) -> None:
    """New eNet scene from eNet lamps; with `lernen` = true the current state becomes the scene value."""
    hub, channels_ = None, []
    for eid in call.data[ATTR_ENTITY_ID]:
        h, e = _hub_for(hass, eid, "light")
        if hub is not None and h is not hub:
            raise ServiceValidationError(tr("lampen_eine_bridge"))
        hub = h
        k = next((k for k in h.channels_ if f"{k.enet_device['sn']}_{k.nr}" == e.unique_id), None)
        if k is None:
            raise ServiceValidationError(tr("kanal_fehlt", entity_id=eid))
        channels_.append(k)
    try:
        s = await hub.scenes.create(call.data["name"], channels_)
        if call.data["lernen"]:
            await hub.scenes.learn(s)
    except BridgeError as err:
        raise HomeAssistantError(tr("szene_fehler", name=call.data["name"], err=err)) from err
    _LOGGER.info("eNet scene %s created (no. %d, GA %d/%d)", s.name, s.nr, s.ga, s.ga_status)
    hass.config_entries.async_schedule_reload(hub.entry_id)


async def _scene_delete(hass: HomeAssistant, call: ServiceCall) -> None:
    """Delete an eNet scene: remove the links in all members, remove the entities."""
    hub, e = _hub_for(hass, call.data[ATTR_ENTITY_ID], "scene")
    s = hub.scenes.get(e.unique_id.split("_szene_", 1)[-1])
    if s is None:
        raise ServiceValidationError(tr("szene_unbekannt", entity_id=e.entity_id))
    try:
        open_items = await hub.scenes.delete(s)
    except BridgeError as err:
        raise HomeAssistantError(tr("szene_fehler", name=s.name, err=err)) from err
    reg = er.async_get(hass)
    reg.async_remove(e.entity_id)
    if btn := reg.async_get_entity_id("button", DOMAIN, f"{e.unique_id}_speichern"):
        reg.async_remove(btn)
    if open_items:
        hub.activity(tr("akt_szene_geloescht_offen", name=s.name, geraete=", ".join(open_items)))
    hass.config_entries.async_schedule_reload(hub.entry_id)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: EnetConfigEntry,
                                           device: dr.DeviceEntry) -> bool:
    """"Delete device" (trash can): factory-reset the eNet device, clean up everything belonging to it and remove it
    from the installation (like "Configure → Remove device", just without the overview). Nothing is deleted if the
    bridge is missing or a transmitter does not wake up – otherwise a device would remain on the radio that HA no
    longer knows. Messages fixed in German: HA translates error messages in the server language, not the UI one."""
    ident = next((i[1] for i in device.identifiers if i[0] == DOMAIN), None) or ""
    hub = entry.runtime_data if entry.state is ConfigEntryState.LOADED else None
    if ident.startswith(("bridge_", "szenen_")):
        raise HomeAssistantError(tr("loeschen_bridge_szenen"))
    sn, _, channel = ident.partition("_")  # channel devices of multi-channel actuators: "SN_channel"
    enet_device = next((g for g in hub.installation["geraete"] if g["sn"] == sn), None) if hub else None
    if hub and enet_device is None:
        return True  # orphaned (no longer in the installation)
    if hub is None or not hub.bridge.connected:
        raise HomeAssistantError(tr("loeschen_nicht_verbunden"))
    if channel:
        raise HomeAssistantError(tr("loeschen_kanal", name=enet_device["name"]))
    try:
        await pair.remove(hub, enet_device)
    except BridgeError as err:
        raise HomeAssistantError(tr("nicht_geloescht", name=enet_device["name"], err=err)) from err
    reg = dr.async_get(hass)
    for d in dr.async_entries_for_config_entry(reg, entry.entry_id):  # remove the channel devices too
        if d.id != device.id and any(i[0] == DOMAIN and i[1].split("_")[0] == sn for i in d.identifiers):
            reg.async_remove_device(d.id)
    hass.config_entries.async_schedule_reload(entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnetConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
    return ok


MANUFACTURERS = {"gira": "Gira", "jung": "Jung"}
# Jung article numbers for devices the catalog lists only under Gira (Jung reports the same ident ending in …3)
JUNG_ARTICLES = {"DVT_SF1S": "FM FS 1 S"}


def _manufacturer(hub: Hub, enet_device: dict) -> tuple[str, str | None]:
    """Manufacturer and article number from the device definitions (otherwise generic eNet, type ident). The ident
    reported by the device is authoritative: last digit 3 = Jung, even if the catalog entry comes from Gira."""
    typ = enet_device.get("typ", "")
    d = hub.definitions.get(typ, {})
    manufacturer_, article = d.get("hersteller") or "", d.get("artikel")
    if product_ident(enet_device.get("produkt") or "").endswith("3") and manufacturer_ != "jung":
        manufacturer_, article = "jung", JUNG_ARTICLES.get(typ)
    return MANUFACTURERS.get(manufacturer_, MANUFACTURER), article or typ


def device_info_for(hub: Hub, enet_device: dict) -> dr.DeviceInfo:
    """HA device for an eNet device (via the bridge)."""
    manufacturer_, article = _manufacturer(hub, enet_device)
    return dr.DeviceInfo(
        identifiers={(DOMAIN, enet_device["sn"])},
        name=enet_device["name"],
        manufacturer=manufacturer_,
        model=hub.model(enet_device),
        model_id=article,
        serial_number=enet_device["sn"],
        suggested_area=enet_device.get("raum"),  # optional in anlage.json; applies only on first creation
        via_device_id=hub.bridge_device_id,
    )


def channel_info(hub: Hub, channel) -> dr.DeviceInfo:
    """HA device for a channel: for single-channel actuators the device itself, otherwise a child device "Kanal n"
    of the actuator, so that each channel appears as a separate lamp in the UI."""
    g = channel.enet_device
    if sum(1 for k in hub.all_channels if k.enet_device is g) == 1:
        return device_info_for(hub, g)
    return _part_info(hub, g, f"{g['sn']}_{channel.nr}", f"Kanal {channel.nr}")


def rocker_info(hub: Hub, enet_device: dict, n: int) -> dr.DeviceInfo:
    """HA device for rocker n of a transmitter: with one rocker the transmitter itself, otherwise a child device
    "Rocker n", so that the mode, lamps or key actions of each rocker are on their own page."""
    if sender.rocker_count(hub, enet_device) <= 1:
        return device_info_for(hub, enet_device)
    return _part_info(hub, enet_device, f"{enet_device['sn']}_wippe{n}", tr("wippe_geraet", n=n))


def _part_info(hub: Hub, enet_device: dict, ident: str, name: str) -> dr.ChildDeviceInfo:
    """Child device (HA 2026.10+): part of the eNet device, listed on its page; HA combines the name with the
    device's name and takes over the area from the device."""
    return dr.ChildDeviceInfo(identifiers={(DOMAIN, ident)}, name=name,
                              parent_device_id=hub.device_ids[enet_device["sn"]])


def scenes_device_info(hub: Hub) -> dr.DeviceInfo:
    """Collective device for the eNet scenes (scene entities and "save" buttons)."""
    return dr.DeviceInfo(identifiers={(DOMAIN, f"szenen_{hub.entry_id}")}, name=tr("geraet_szenen"),
                         manufacturer=MANUFACTURER, model="KNX-Szenen in den Aktoren",
                         via_device_id=hub.bridge_device_id)


def bridge_info(hub: Hub) -> dr.DeviceInfo:
    return dr.DeviceInfo(identifiers={(DOMAIN, f"bridge_{hub.entry_id}")},
                         sw_version=hub.bridge.info.get("fw"))

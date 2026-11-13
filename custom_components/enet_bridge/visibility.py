"""Which entities exist right now – instead of hiding, entities that are not needed are not created and their registry
entries are removed (HA lists hidden and disabled entities on the device page anyway):

- parameters that are inactive with the current values of the parameters they depend on (catalogue dependency, e.g. the
  slat time only for the operating mode BLINDS); writing a controlling parameter reloads the entry,
- while the travel time of a blind is measured (calibration.py), its device shows only "End position reached" and
  "Cancel measuring"; all other entities of the device are removed and come back afterwards. Their settings (entity
  ID, name, icon, area, labels, aliases, hidden/disabled) are saved before and restored after.
"""
from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

SAVED_FIELDS = ("name", "icon", "area_id", "labels", "aliases", "hidden_by", "disabled_by")


def device_sn(entity: Any) -> str | None:
    """SN of the eNet device an entity belongs to (from its device identifier "SN" or "SN_channel")."""
    info = getattr(entity, "_attr_device_info", None) or {}
    for domain, ident in info.get("identifiers") or ():
        if domain == DOMAIN:
            return str(ident).split("_")[0]
    return None


def shown(hub: Hub, entity: Any) -> bool:
    measuring = hub.measuring_sns()
    only = getattr(entity, "measuring_only", None)  # measuring buttons: True = only while measuring
    sn = device_sn(entity)
    if only is not None:
        return (sn in measuring) == only
    if sn is not None and sn in measuring:
        return False
    group, param = getattr(entity, "param_group", None), getattr(entity, "param", None)
    if group is not None and param is not None and not hub.param_active(group, param):
        return False
    return True


def filtered(setup):
    """Decorator for the platforms' async_setup_entry: pass on only the entities shown right now."""
    @functools.wraps(setup)
    async def wrapper(hass: HomeAssistant, entry, async_add_entities) -> None:
        hub = entry.runtime_data

        def add(entities, update_before_add: bool = False) -> None:
            keep = [e for e in entities if shown(hub, e)]
            hub.added.update(e.unique_id for e in keep if e.unique_id)
            async_add_entities(keep, update_before_add)

        await setup(hass, entry, add)
    return wrapper


def _store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, 1, f"{DOMAIN}_ausgeblendet_{entry_id}")


async def sync_registry(hass: HomeAssistant, hub: Hub) -> None:
    """After the platforms are set up: remove registry entries of entities not created this time (device being
    measured: settings saved first; inactive parameters; measuring buttons), restore saved settings of devices no longer
    being measured."""
    reg = er.async_get(hass)
    store = _store(hass, hub.entry_id)
    saved: dict[str, dict[str, Any]] = await store.async_load() or {}
    measuring = hub.measuring_sns()
    changed = False
    for e in list(er.async_entries_for_config_entry(reg, hub.entry_id)):
        uid = e.unique_id
        if uid in hub.added:
            if e.hidden_by is er.RegistryEntryHider.INTEGRATION and uid not in saved:
                reg.async_update_entity(e.entity_id, hidden_by=None)  # older versions hid instead of removing
            continue
        sn = uid.split("_")[0]
        if sn in measuring:
            if uid not in saved:
                saved[uid] = {"entity_id": e.entity_id, **{f: _plain(getattr(e, f)) for f in SAVED_FIELDS}}
                changed = True
            reg.async_remove(e.entity_id)
            hass.states.async_remove(e.entity_id)  # no "unavailable → unknown" entry when it comes back
        elif "_p_" in uid or uid.endswith(("_einmessen_endlage", "_einmessen_abbrechen", "_einmessen_start", "_einmessen",
                                                                "_einmessen_laufzeit")):
            reg.async_remove(e.entity_id)
            hass.states.async_remove(e.entity_id)
    for uid, s in list(saved.items()):
        if uid.split("_")[0] in measuring:
            continue
        entity_id = next((e.entity_id for e in er.async_entries_for_config_entry(reg, hub.entry_id)
                          if e.unique_id == uid), None)
        if entity_id is not None:
            _restore(reg, entity_id, s)
        del saved[uid]
        changed = True
    if changed:
        await store.async_save(saved)


def _plain(v: Any) -> Any:
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    if v is not None and hasattr(v, "value"):
        return v.value  # enum (hidden_by / disabled_by)
    return v


def _restore(reg: er.EntityRegistry, entity_id: str, s: dict[str, Any]) -> None:
    changes: dict[str, Any] = {}
    if s.get("entity_id") and s["entity_id"] != entity_id and reg.async_get(s["entity_id"]) is None:
        changes["new_entity_id"] = s["entity_id"]
    for f in ("name", "icon", "area_id"):
        changes[f] = s.get(f)
    changes["labels"] = set(s.get("labels") or ())
    changes["aliases"] = set(s.get("aliases") or ())
    changes["hidden_by"] = er.RegistryEntryHider(s["hidden_by"]) if s.get("hidden_by") else None
    if s.get("disabled_by"):
        changes["disabled_by"] = er.RegistryEntryDisabler(s["disabled_by"])
    try:
        reg.async_update_entity(entity_id, **changes)
    except ValueError as err:  # e.g. the old entity ID is taken in the meantime
        _LOGGER.warning("Settings of %s not fully restored: %s", entity_id, err)

"""Reparatur-Dialoge der Anlagenprüfung (pruefung.py)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .bridge import BridgeError
from .const import DOMAIN


def _hub(hass: HomeAssistant, data: dict[str, Any] | None):
    entry = hass.config_entries.async_get_entry((data or {}).get("entry_id", ""))
    return getattr(entry, "runtime_data", None) if entry else None


class AntwortetNicht(RepairsFlow):
    """Aktor antwortet nicht: erneut abfragen oder aus der Anlage nehmen."""

    def __init__(self, issue_id: str, data: dict[str, Any] | None) -> None:
        self.issue_id, self.data = issue_id, data or {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        hub = _hub(self.hass, self.data)
        g = next((g for g in hub.anlage["geraete"] if g["sn"] == self.data.get("sn")), None) if hub else None
        if g is None:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_abort(reason="erledigt")
        self._geraet = g
        return self.async_show_menu(step_id="init", menu_options=["erneut", "entfernen"],
                                    description_placeholders={"name": g["name"]})

    async def async_step_erneut(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        hub, g = _hub(self.hass, self.data), self._geraet
        for k in [k for k in hub.kanaele if k.geraet is g]:
            try:
                await hub.bridge.request("poll", ia=k.ia, ch=k.nr)
            except BridgeError:
                continue
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_create_entry(data={})
        return self.async_abort(reason="antwortet_weiter_nicht", description_placeholders={"name": g["name"]})

    async def async_step_entfernen(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="entfernen", description_placeholders={"name": self._geraet["name"]})
        dev = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, self._geraet["sn"])})
        if dev is not None:
            await self.hass.services.async_call(DOMAIN, "geraet_entfernen", {"device_id": dev.id}, blocking=True)
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(data={})


class LinksFehlen(RepairsFlow):
    """Fehlende Szenen-Links nach Bestätigung anlegen."""

    def __init__(self, issue_id: str, data: dict[str, Any] | None) -> None:
        self.issue_id, self.data = issue_id, data or {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="init")
        hub = _hub(self.hass, self.data)
        if hub is None:
            return self.async_abort(reason="erledigt")
        fehler = await hub.pruefung.links_reparieren()
        if fehler:
            return self.async_abort(reason="nicht_repariert", description_placeholders={"fehler": "; ".join(fehler)})
        return self.async_create_entry(data={})


class LinksAuffaellig(RepairsFlow):
    """Auffällige Einträge in der Link-Tabelle eines Aktors: entfernen oder behalten."""

    def __init__(self, issue_id: str, data: dict[str, Any] | None) -> None:
        self.issue_id, self.data = issue_id, data or {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        hub = _hub(self.hass, self.data)
        g = next((g for g in hub.anlage["geraete"] if g["sn"] == self.data.get("sn")), None) if hub else None
        if g is None:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_abort(reason="erledigt")
        self._geraet = g
        return self.async_show_menu(step_id="init", menu_options=["entfernen", "behalten"],
                                    description_placeholders={"name": g["name"]})

    async def async_step_entfernen(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="entfernen", description_placeholders={"name": self._geraet["name"]})
        hub = _hub(self.hass, self.data)
        try:
            fehler = await hub.pruefung.links_entfernen(self._geraet["sn"])
        except BridgeError as err:
            fehler = [str(err)]
        if fehler:
            return self.async_abort(reason="nicht_entfernt", description_placeholders={"fehler": "; ".join(fehler)})
        return self.async_create_entry(data={})

    async def async_step_behalten(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="behalten", description_placeholders={"name": self._geraet["name"]})
        await _hub(self.hass, self.data).pruefung.links_behalten(self._geraet["sn"])
        return self.async_create_entry(data={})


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    if issue_id.startswith("antwortet_nicht_"):
        return AntwortetNicht(issue_id, data)
    if issue_id.startswith("links_auffaellig_"):
        return LinksAuffaellig(issue_id, data)
    return LinksFehlen(issue_id, data)

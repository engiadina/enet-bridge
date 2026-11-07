"""Gemeinsame Basis der eNet-Entitäten."""
from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .bridge import BridgeError
from .hub import Hub
from .params import kanal_name


class EnetEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    @property
    def available(self) -> bool:
        return self.hub.bridge.connected

    def _listen(self, name: str, handler=None) -> None:
        """Bei einem Hub-Signal neu zeichnen (oder handler aufrufen)."""
        self.async_on_remove(async_dispatcher_connect(
            self.hass, self.hub.signal(name), handler or self.async_write_ha_state))

    async def async_added_to_hass(self) -> None:
        self._listen("bridge")


class EnetParamEntity(EnetEntity):
    """Ein Geräteparameter (Konfiguration) bzw. ein Zähler (Diagnose) eines Aktors."""

    def __init__(self, hub: Hub, gruppe, param) -> None:
        from . import geraet_info, kanal_info  # Import hier: __init__ importiert die Plattformen

        super().__init__(hub)
        self.gruppe, self.param = gruppe, param
        g = gruppe.geraet
        kanal = hub.kanal(g, gruppe.kanal) if gruppe.kanal is not None else None
        self._attr_device_info = kanal_info(hub, kanal) if kanal else geraet_info(hub, g)
        self._attr_unique_id = f"{g['sn']}_p_{param.key}"
        vorsatz = kanal_name(g.get("typ", ""), gruppe.kanal)
        self._attr_name = f"{vorsatz} {param.name}" if vorsatz else param.name
        self._attr_entity_category = EntityCategory.DIAGNOSTIC if param.nur_lesen else EntityCategory.CONFIG
        self._attr_entity_registry_enabled_default = param.standard_aktiv
        self._attr_extra_state_attributes = {"parameter": param.id, "adresse": param.key}

    @property
    def wert(self):
        return self.hub.params.wert(self.gruppe.geraet["sn"], self.param)

    @property
    def available(self) -> bool:
        return super().available and self.wert is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen("params", self._on_params)

    @callback
    def _on_params(self, sn: str) -> None:
        if sn == self.gruppe.geraet["sn"]:
            self.async_write_ha_state()

    async def schreiben(self, wert) -> None:
        g = self.gruppe.geraet
        try:
            await self.hub.geweckt(g, lambda: self.hub.params.schreiben(g["ia"], g["sn"], self.gruppe.geraet_obj,
                                                                        self.param, wert))
        except BridgeError as err:
            raise HomeAssistantError(f"{g['name']} – {self.param.name}: {err}") from err


def param_entitaeten(hub: Hub, cls, bedingung):
    """Alle Parameter, für die bedingung(param) gilt, als Entitäten der Klasse cls."""
    return [cls(hub, gr, p) for gr in hub.param_gruppen for p in gr.params if bedingung(p)]

"""Bridge: Server-Funktionen ein/aus (Tasten beantworten, lokale Szenen, Status-Polling)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, bridge_info
from .bridge import BridgeError
from .entity import EnetEntity, EnetParamEntity, param_entitaeten
from .texte import tr


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities([ServerSwitch(hub), Mithoeren(hub)] +
                       param_entitaeten(hub, EnetParamSwitch, lambda p: p.art == "bool" and not p.nur_lesen))


class ServerSwitch(EnetEntity, SwitchEntity):
    _attr_translation_key = "server"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry_id}_server"
        self._attr_device_info = bridge_info(hub)

    @property
    def is_on(self) -> bool:
        return self.hub.server_on

    async def _set(self, on: bool) -> None:
        try:
            await self.hub.server_setzen(on)
        except BridgeError as err:
            raise HomeAssistantError(tr("server_funktionen_fehler", err=err)) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class Mithoeren(EnetEntity, SwitchEntity):
    """„Nur mithören“: Bridge empfängt nur, sendet nichts (Betrieb neben dem echten Server vor dem Umzug)."""

    _attr_translation_key = "mithoeren"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:ear-hearing"

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry_id}_mithoeren"
        self._attr_device_info = bridge_info(hub)

    @property
    def available(self) -> bool:
        return True  # auch ohne Bridge umschaltbar (gilt beim nächsten Verbinden)

    @property
    def is_on(self) -> bool:
        return self.hub.mithoeren

    async def _set(self, on: bool) -> None:
        try:
            await self.hub.mithoeren_setzen(on)
        except BridgeError as err:
            raise HomeAssistantError(tr("mithoeren_fehler", err=err)) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class EnetParamSwitch(EnetParamEntity, SwitchEntity):
    """Ein/Aus-Parameter eines Aktors."""

    @property
    def is_on(self) -> bool | None:
        return self.wert

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.schreiben(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.schreiben(False)

"""Bridge: server functions on/off (answer key presses, local scenes, status polling)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, bridge_info
from .bridge import BridgeError
from .entity import EnetEntity, EnetParamEntity, param_entities
from .texts import tr


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities([ServerSwitch(hub), ListenOnly(hub)] +
                       param_entities(hub, EnetParamSwitch, lambda p: p.art == "bool" and not p.read_only))


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
            await self.hub.set_server(on)
        except BridgeError as err:
            raise HomeAssistantError(tr("server_funktionen_fehler", err=err)) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class ListenOnly(EnetEntity, SwitchEntity):
    """"Listen only": the bridge only receives and sends nothing (operation alongside the real server before
    migrating)."""

    _attr_translation_key = "mithoeren"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:ear-hearing"

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry_id}_mithoeren"
        self._attr_device_info = bridge_info(hub)

    @property
    def available(self) -> bool:
        return True  # can be toggled even without a bridge (applies on the next connection)

    @property
    def is_on(self) -> bool:
        return self.hub.listen_only

    async def _set(self, on: bool) -> None:
        try:
            await self.hub.set_listen_only(on)
        except BridgeError as err:
            raise HomeAssistantError(tr("mithoeren_fehler", err=err)) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class EnetParamSwitch(EnetParamEntity, SwitchEntity):
    """On/off parameter of an actuator."""

    @property
    def is_on(self) -> bool | None:
        return self.value

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.write(False)

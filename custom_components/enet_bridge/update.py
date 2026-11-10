"""Bridge: install firmware from the integration's bundle.

If the bridge answers (hello, product "enet-bridge") with older firmware, the hub updates automatically.
If no bridge answers on the port (blank ESP32), installation happens here only after confirmation.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, bridge_info
from .bridge import BridgeError
from .entity import EnetEntity
from .texts import tr


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([BridgeFirmware(entry.runtime_data)])


class BridgeFirmware(EnetEntity, UpdateEntity):
    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry_id}_firmware"
        self._attr_device_info = bridge_info(hub)

    @property
    def available(self) -> bool:
        return self.hub.flashing or super().available

    @property
    def installed_version(self) -> str | None:
        # Blank ESP: "0.0.0" instead of None – with None, HA sets the state to "unknown" and offers no install
        return "0.0.0" if self.hub.no_bridge else self.hub.bridge.info.get("fw")

    @property
    def latest_version(self) -> str | None:
        if self.hub.no_bridge:  # blank ESP32: offer as an update
            return self.hub.fw_bundle
        return self.hub.fw_bundle or self.installed_version

    @property
    def in_progress(self) -> bool:
        return self.hub.flashing

    @property
    def release_summary(self) -> str | None:
        if self.hub.no_bridge:
            return tr("keine_bridge_am_port")
        return None

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        try:
            await self.hub.update_firmware()
        except BridgeError as err:
            raise HomeAssistantError(tr("firmware_fehler", err=err)) from err

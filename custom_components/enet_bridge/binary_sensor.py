"""Bridge: real eNet Server detected, radio module reachable. Transmitter: battery low."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EnetConfigEntry, bridge_info, device_info_for
from .measurements import KIND_MOTION
from .const import SENSOR_TYPES
from .entity import EnetEntity


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    sender = [g for g in hub.installation["geraete"] if g.get("rolle") == "sender" or g.get("typ") in SENSOR_TYPES]
    sensors = [g for g in hub.installation["geraete"] if g.get("typ") in SENSOR_TYPES]
    async_add_entities([RealServer(hub), RadioModule(hub)] + [Battery(hub, g) for g in sender]
                       + [Threshold(hub, g, k) for g in sensors for k in (1, 2)]
                       + [Motion(hub, g, k) for g, k in hub.measurements.channels_(KIND_MOTION)])


class Motion(EnetEntity, BinarySensorEntity):
    """Motion detector (measurements.py, untested): on when triggered or retriggered, off after the transmitted
    on-duration without a new trigger."""

    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_translation_key = "bewegung"

    def __init__(self, hub, enet_device, channel) -> None:
        super().__init__(hub)
        from .sensor import meas_device_info

        self.sn, self.nr = enet_device["sn"], channel["nr"]
        self._attr_unique_id = f"{self.sn}_{self.nr}_bewegung"
        self._attr_device_info = meas_device_info(hub, enet_device, self.nr)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"bewegung_{self.sn}_{self.nr}")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.hub.measurements.motion.get(f"{self.sn}/{self.nr}", False)


class RealServer(EnetEntity, BinarySensorEntity):
    _attr_translation_key = "real_server"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry_id}_real_server"
        self._attr_device_info = bridge_info(hub)

    @property
    def is_on(self) -> bool:
        return self.hub.real_server


class RadioModule(EnetEntity, BinarySensorEntity):
    """CC1101 responds (the bridge checks this continuously and reports failures)."""

    _attr_translation_key = "funkmodul"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry_id}_funkmodul"
        self._attr_device_info = bridge_info(hub)

    @property
    def is_on(self) -> bool:
        return bool(self.hub.radio.get("ok"))

    @property
    def extra_state_attributes(self):
        return {"chip_version": self.hub.radio.get("version"), "bridge_id": self.hub.bridge.info.get("id")}


class Battery(EnetEntity, BinarySensorEntity, RestoreEntity):
    """Transmitter battery low (on) or OK (off). The transmitter reports this with every key press in the RF info
    byte; until the first key press the state from before the restart applies."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_translation_key = "batterie"  # state "OK"/"Low" instead of "Normal"/"Low"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, enet_device) -> None:
        super().__init__(hub)
        self.sn = enet_device["sn"]
        self._attr_unique_id = f"{self.sn}_batterie"
        self._attr_device_info = device_info_for(hub, enet_device)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.sn not in self.hub.battery_low and (old := await self.async_get_last_state()) \
                and old.state in ("on", "off"):
            self.hub.battery_low[self.sn] = old.state == "on"
        self._listen(f"batterie_{self.sn}")

    @property
    def available(self) -> bool:
        return True  # state comes from the transmitter, not from the connection to the bridge

    @property
    def is_on(self) -> bool | None:
        return self.hub.battery_low.get(self.sn)


class Threshold(EnetEntity, BinarySensorEntity, RestoreEntity):
    """Sun sensor: K1 "sun protection", K2 "twilight" – on after the channel's on-scene (48 or 50), off after the
    off-scene (49 or 51) or when the other channel switches on. The sensor only reports changes; until then the state
    from before the restart applies."""

    def __init__(self, hub, enet_device, channel: int) -> None:
        super().__init__(hub)
        self.sn, self.channel = enet_device["sn"], channel
        self._attr_translation_key = "sonnenschutz" if channel == 1 else "daemmerung"
        self._attr_unique_id = f"{self.sn}_schwelle_{channel}"
        self._attr_device_info = device_info_for(hub, enet_device)

    @property
    def _key(self) -> str:
        return f"{self.sn}/{self.channel}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._key not in self.hub.sensor_active and (old := await self.async_get_last_state()) \
                and old.state in ("on", "off"):
            self.hub.sensor_active[self._key] = old.state == "on"
        self._listen(f"sensor_aktiv_{self.sn}")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool | None:
        return self.hub.sensor_active.get(self._key)

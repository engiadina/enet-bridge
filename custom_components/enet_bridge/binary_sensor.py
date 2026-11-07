"""Bridge: echter eNet Server erkannt, Funkmodul erreichbar. Sender: Batterie schwach."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EnetConfigEntry, bridge_info, geraet_info
from .const import SENSOR_TYPEN
from .entity import EnetEntity


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    sender = [g for g in hub.anlage["geraete"] if g.get("rolle") == "sender" or g.get("typ") in SENSOR_TYPEN]
    sensoren = [g for g in hub.anlage["geraete"] if g.get("typ") in SENSOR_TYPEN]
    async_add_entities([RealServer(hub), Funkmodul(hub)] + [Batterie(hub, g) for g in sender]
                       + [Schwelle(hub, g, k) for g in sensoren for k in (1, 2)])


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


class Funkmodul(EnetEntity, BinarySensorEntity):
    """CC1101 antwortet (die Bridge prüft das laufend und meldet Ausfälle)."""

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


class Batterie(EnetEntity, BinarySensorEntity, RestoreEntity):
    """Batterie des Senders schwach (an) bzw. in Ordnung (aus). Der Sender meldet das mit jedem Tastendruck im
    RF-Info-Byte; bis zum ersten Tastendruck gilt der Zustand vor dem Neustart."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_translation_key = "batterie"  # Zustand „OK“/„Schwach“ statt „Normal“/„Niedrig“
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, geraet) -> None:
        super().__init__(hub)
        self.sn = geraet["sn"]
        self._attr_unique_id = f"{self.sn}_batterie"
        self._attr_device_info = geraet_info(hub, geraet)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.sn not in self.hub.batterie_schwach and (alt := await self.async_get_last_state()) \
                and alt.state in ("on", "off"):
            self.hub.batterie_schwach[self.sn] = alt.state == "on"
        self._listen(f"batterie_{self.sn}")

    @property
    def available(self) -> bool:
        return True  # Zustand stammt vom Sender, nicht von der Verbindung zur Bridge

    @property
    def is_on(self) -> bool | None:
        return self.hub.batterie_schwach.get(self.sn)


class Schwelle(EnetEntity, BinarySensorEntity, RestoreEntity):
    """Sonnensensor: K1 „Sonnenschutz“, K2 „Dämmerung“ – an nach der Ein-Szene des Kanals (48 bzw. 50), aus nach
    der Aus-Szene (49 bzw. 51) oder wenn der andere Kanal einschaltet. Der Sensor meldet nur Wechsel; bis dahin gilt
    der Zustand vor dem Neustart."""

    def __init__(self, hub, geraet, kanal: int) -> None:
        super().__init__(hub)
        self.sn, self.kanal = geraet["sn"], kanal
        self._attr_translation_key = "sonnenschutz" if kanal == 1 else "daemmerung"
        self._attr_unique_id = f"{self.sn}_schwelle_{kanal}"
        self._attr_device_info = geraet_info(hub, geraet)

    @property
    def _key(self) -> str:
        return f"{self.sn}/{self.kanal}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._key not in self.hub.sensor_aktiv and (alt := await self.async_get_last_state()) \
                and alt.state in ("on", "off"):
            self.hub.sensor_aktiv[self._key] = alt.state == "on"
        self._listen(f"sensor_aktiv_{self.sn}")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool | None:
        return self.hub.sensor_aktiv.get(self._key)

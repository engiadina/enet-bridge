"""Actuators: diagnostics (signal, last report) and counters from the device parameters.
Bridge: frequency correction."""
from __future__ import annotations

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import (LIGHT_LUX, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory,
                                 UnitOfApparentPower, UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfEnergy,
                                 UnitOfPower, UnitOfReactivePower, UnitOfTime)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, _part_info, bridge_info, device_info_for
from .measurements import KIND_MOTION, KIND_ENERGY, VALUES
from .const import SENSOR_TYPES
from .entity import EnetEntity, EnetParamEntity, param_entities
from .visibility import filtered


@filtered
async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    actuators = [g for g in hub.installation["geraete"] if g.get("rolle") in ("aktor", "sensor")]
    entities: list = [cls(hub, g) for g in actuators for cls in (Signal, LastMessage)]
    entities += param_entities(hub, Counter, lambda p: p.art == "zahl" and p.read_only)
    entities += param_entities(hub, ParamText, lambda p: p.art == "auswahl" and p.read_only)
    entities += [Illuminance(hub, g) for g in hub.installation["geraete"] if g.get("typ") in SENSOR_TYPES]
    entities += [MeasuredValue(hub, g, k, w.key) for g, k in hub.measurements.channels_(KIND_ENERGY)
                 for w in VALUES[KIND_ENERGY].values()]
    entities += [MeasuredValue(hub, g, k, "helligkeit") for g, k in hub.measurements.channels_(KIND_MOTION)]
    entities += [FrequencyCorrection(hub), Programming(hub)]
    async_add_entities(entities)



class _Diag(EnetEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, enet_device, key: str) -> None:
        super().__init__(hub)
        self.enet_device = enet_device
        self._attr_translation_key = key
        self._attr_unique_id = f"{enet_device['sn']}_{key}"
        self._attr_device_info = device_info_for(hub, enet_device)

    @property
    def _d(self):
        return self.hub.diag.get(self.enet_device["ia"])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"diag_{self.enet_device['ia']}")


class Signal(_Diag):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub, enet_device) -> None:
        super().__init__(hub, enet_device, "signal")

    @property
    def native_value(self):
        return self._d.rssi if self._d else None

    @property
    def extra_state_attributes(self):
        return {"versuche": self._d.attempts} if self._d and self._d.attempts else None


class LastMessage(_Diag):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hub, enet_device) -> None:
        super().__init__(hub, enet_device, "letzte_meldung")

    @property
    def native_value(self):
        return self._d.time_ if self._d else None


class Illuminance(EnetEntity, RestoreSensor):
    """Brightness from the sun sensor. It sends it by itself on a significant change; until then the last value from
    before the restart applies."""

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "helligkeit"

    def __init__(self, hub, enet_device) -> None:
        super().__init__(hub)
        self.sn = enet_device["sn"]
        self._attr_unique_id = f"{self.sn}_helligkeit"
        self._attr_device_info = device_info_for(hub, enet_device)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.sn not in self.hub.illuminance and (old := await self.async_get_last_sensor_data()) \
                and old.native_value is not None:
            self.hub.illuminance[self.sn] = float(old.native_value)
        self._listen(f"helligkeit_{self.sn}")

    @property
    def available(self) -> bool:
        return True  # value comes from the sensor, not from the connection to the bridge

    @property
    def native_value(self) -> float | None:
        return self.hub.illuminance.get(self.sn)


def meas_device_info(hub, enet_device: dict, nr: int):
    """Single channel: the device itself; multi-channel (e.g. energy sensor DIN-rail 4-gang): child device "Kanal n"."""
    if sum(1 for g, _k in hub.measurements.channels_() if g is enet_device) <= 1:
        return device_info_for(hub, enet_device)
    return _part_info(hub, enet_device, f"{enet_device['sn']}_{nr}", f"Kanal {nr}")


# key → (device class, unit, state class, decimal places)
MEASUREMENT_DISPLAY = {
    "spannung": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, SensorStateClass.MEASUREMENT, 1),
    "strom": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE, SensorStateClass.MEASUREMENT, 3),
    "wirkleistung": (SensorDeviceClass.POWER, UnitOfPower.WATT, SensorStateClass.MEASUREMENT, 1),
    "blindleistung": (SensorDeviceClass.REACTIVE_POWER, UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
                      SensorStateClass.MEASUREMENT, 1),
    "scheinleistung": (SensorDeviceClass.APPARENT_POWER, UnitOfApparentPower.VOLT_AMPERE,
                       SensorStateClass.MEASUREMENT, 1),
    "energie": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, SensorStateClass.TOTAL_INCREASING, 3),
    "helligkeit": (SensorDeviceClass.ILLUMINANCE, LIGHT_LUX, SensorStateClass.MEASUREMENT, 0),
}


class MeasuredValue(EnetEntity, RestoreSensor):
    """Measured value of an energy sensor or motion detector (measurements.py, untested). Until the first value arrives,
    the last one from before the restart applies."""

    def __init__(self, hub, enet_device, channel, key: str) -> None:
        super().__init__(hub)
        self.sn, self.nr, self.key = enet_device["sn"], channel["nr"], key
        self._attr_translation_key = key
        self._attr_unique_id = f"{self.sn}_{self.nr}_{key}"
        self._attr_device_info = meas_device_info(hub, enet_device, self.nr)
        (self._attr_device_class, self._attr_native_unit_of_measurement, self._attr_state_class,
         self._attr_suggested_display_precision) = MEASUREMENT_DISPLAY[key]
        if key in ("blindleistung", "scheinleistung"):
            self._attr_entity_registry_enabled_default = False

    @property
    def _store_key(self) -> str:
        return f"{self.sn}/{self.nr}/{self.key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._store_key not in self.hub.measurements.values_ and (old := await self.async_get_last_sensor_data()) \
                and old.native_value is not None:
            self.hub.measurements.values_[self._store_key] = float(old.native_value)
        self._listen(f"messwert_{self.sn}_{self.nr}")

    @property
    def available(self) -> bool:
        return True  # value comes from the device, not from the connection to the bridge

    @property
    def native_value(self) -> float | None:
        return self.hub.measurements.values_.get(self._store_key)


class Counter(EnetParamEntity, SensorEntity):
    """Read-only numeric parameters: switching cycles and operating hours of the actuators (counters); for the sun
    sensor the lux threshold (set on the device) and the battery charging voltage."""

    def __init__(self, hub, param_group, param) -> None:
        super().__init__(hub, param_group, param)
        self._attr_entity_registry_enabled_default = True
        unit = param.typ.get("einheit")
        if "COUNTER" in param.id:
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        if unit == "h":
            self._attr_native_unit_of_measurement = UnitOfTime.HOURS
            self._attr_device_class = SensorDeviceClass.DURATION
        elif unit == "Lux":
            self._attr_native_unit_of_measurement = LIGHT_LUX
            self._attr_device_class = SensorDeviceClass.ILLUMINANCE
        elif unit == "V":
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2  # otherwise HA rounds 2.62 V to "3 V"

    @property
    def native_value(self):
        return self.value


class ParamText(EnetParamEntity, SensorEntity):
    """Read-only selection parameters (e.g. function of a sun sensor channel), as text."""

    @property
    def native_value(self):
        from .params import choice
        w = self.value
        return None if w is None else choice(w)


class FrequencyCorrection(EnetEntity, SensorEntity):
    """The bridge's FSCTRL0 (per board in NVS, found by calibration), steps of ~1.59 kHz."""

    _attr_translation_key = "frequenzkorrektur"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False  # runs automatically; enable only for troubleshooting
    _attr_native_unit_of_measurement = "kHz"
    _attr_suggested_display_precision = 1

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry_id}_frequenzkorrektur"
        self._attr_device_info = bridge_info(hub)

    @property
    def native_value(self):
        f = self.hub.radio.get("freq_offset")
        return None if f is None else round(f * 26000 / 16384, 1)

    @property
    def extra_state_attributes(self):
        r, s = self.hub.radio, self.hub.bridge.info.get("stats", {})
        return {"fsctrl0": r.get("freq_offset"), "kalibriert": r.get("calibrated"),
                "abweichung_schritte": r.get("freqest"), "telegramme": r.get("freqest_n"),
                "empfangen": s.get("rx_ok"), "crc_fehler": s.get("crc_error"), "unlesbar": s.get("invalid"),
                # firmware 0.8.18 or later: noise floor (dBm), share of "channel busy" (%), sent despite busy
                "grundrauschen": s.get("noise_dbm"), "kanal_belegt_prozent": s.get("busy_pct"),
                "gesendet_trotz_belegt": s.get("tx_busy")}


class Programming(EnetEntity, SensorEntity):
    """What is currently being programmed over the air: ready / scene being transferred / actuator being programmed.
    An actuator's lamps briefly go off during this (restart after the frame) and are restored."""

    _attr_translation_key = "programmierung"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["bereit", "szene", "aktor"]

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry_id}_programmierung"
        self._attr_device_info = bridge_info(hub)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen("programmierung")

    @property
    def native_value(self) -> str:
        if self.hub.scene_in_progress:
            return "szene"
        return "aktor" if self.hub.programmed else "bereit"

    @property
    def icon(self) -> str:
        return "mdi:check-circle-outline" if self.native_value == "bereit" else "mdi:progress-wrench"

    @property
    def extra_state_attributes(self) -> dict:
        names_ = sorted({g["name"] for g in self.hub.installation["geraete"] if g["ia"] in self.hub.programmed})
        return {"szene": self.hub.scene_in_progress, "geraete": names_}

"""Aktoren: Diagnose (Signal, letzte Meldung) und Zähler aus den Geräteparametern. Bridge: Frequenzkorrektur."""
from __future__ import annotations

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import (LIGHT_LUX, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory,
                                 UnitOfElectricPotential, UnitOfTime)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, bridge_info, geraet_info
from .const import SENSOR_TYPEN
from .entity import EnetEntity, EnetParamEntity, param_entitaeten


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    aktoren = [g for g in hub.anlage["geraete"] if g.get("rolle") == "aktor"]
    entities: list = [cls(hub, g) for g in aktoren for cls in (Signal, LetzteMeldung)]
    entities += param_entitaeten(hub, Zaehler, lambda p: p.art == "zahl" and p.nur_lesen)
    entities += param_entitaeten(hub, ParamText, lambda p: p.art == "auswahl" and p.nur_lesen)
    entities += [Helligkeit(hub, g) for g in hub.anlage["geraete"] if g.get("typ") in SENSOR_TYPEN]
    entities += [Frequenzkorrektur(hub), Programmierung(hub)]
    async_add_entities(entities)


class _Diag(EnetEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, geraet, key: str) -> None:
        super().__init__(hub)
        self.geraet = geraet
        self._attr_translation_key = key
        self._attr_unique_id = f"{geraet['sn']}_{key}"
        self._attr_device_info = geraet_info(hub, geraet)

    @property
    def _d(self):
        return self.hub.diag.get(self.geraet["ia"])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"diag_{self.geraet['ia']}")


class Signal(_Diag):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub, geraet) -> None:
        super().__init__(hub, geraet, "signal")

    @property
    def native_value(self):
        return self._d.rssi if self._d else None

    @property
    def extra_state_attributes(self):
        return {"versuche": self._d.versuche} if self._d and self._d.versuche else None


class LetzteMeldung(_Diag):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hub, geraet) -> None:
        super().__init__(hub, geraet, "letzte_meldung")

    @property
    def native_value(self):
        return self._d.zeit if self._d else None


class Helligkeit(EnetEntity, RestoreSensor):
    """Helligkeit des Sonnensensors. Er sendet sie selbst bei deutlicher Änderung; bis dahin gilt der letzte Wert
    vor dem Neustart."""

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "helligkeit"

    def __init__(self, hub, geraet) -> None:
        super().__init__(hub)
        self.sn = geraet["sn"]
        self._attr_unique_id = f"{self.sn}_helligkeit"
        self._attr_device_info = geraet_info(hub, geraet)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.sn not in self.hub.helligkeit and (alt := await self.async_get_last_sensor_data()) \
                and alt.native_value is not None:
            self.hub.helligkeit[self.sn] = float(alt.native_value)
        self._listen(f"helligkeit_{self.sn}")

    @property
    def available(self) -> bool:
        return True  # Wert stammt vom Sensor, nicht von der Verbindung zur Bridge

    @property
    def native_value(self) -> float | None:
        return self.hub.helligkeit.get(self.sn)


class Zaehler(EnetParamEntity, SensorEntity):
    """Nur lesbare Zahlen-Parameter: Schaltspiele und Betriebsstunden der Aktoren (Zähler), beim Sonnensensor
    Lux-Schwelle (am Gerät eingestellt) und Ladespannung des Akkus."""

    def __init__(self, hub, gruppe, param) -> None:
        super().__init__(hub, gruppe, param)
        self._attr_entity_registry_enabled_default = True
        einheit = param.typ.get("einheit")
        if "COUNTER" in param.id:
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        if einheit == "h":
            self._attr_native_unit_of_measurement = UnitOfTime.HOURS
            self._attr_device_class = SensorDeviceClass.DURATION
        elif einheit == "Lux":
            self._attr_native_unit_of_measurement = LIGHT_LUX
            self._attr_device_class = SensorDeviceClass.ILLUMINANCE
        elif einheit == "V":
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2  # sonst rundet HA 2,62 V auf „3 V“

    @property
    def native_value(self):
        return self.wert


class ParamText(EnetParamEntity, SensorEntity):
    """Nur lesbare Auswahl-Parameter (z. B. Funktion eines Sonnensensor-Kanals), als Text."""

    @property
    def native_value(self):
        from .params import auswahl
        w = self.wert
        return None if w is None else auswahl(w)


class Frequenzkorrektur(EnetEntity, SensorEntity):
    """FSCTRL0 der Bridge (je Platine im NVS, per Kalibrierung gefunden), Schritte zu ~1,59 kHz."""

    _attr_translation_key = "frequenzkorrektur"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False  # läuft automatisch; nur zur Fehlersuche einschalten
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
                # ab Firmware 0.8.18: Grundrauschen (dBm), Anteil „Kanal belegt“ (%), trotz belegt gesendet
                "grundrauschen": s.get("noise_dbm"), "kanal_belegt_prozent": s.get("busy_pct"),
                "gesendet_trotz_belegt": s.get("tx_busy")}


class Programmierung(EnetEntity, SensorEntity):
    """Was gerade per Funk programmiert wird: bereit / Szene wird übertragen / Aktor wird programmiert.
    Lampen eines Aktors gehen dabei kurz aus (Neustart nach dem Rahmen) und werden wiederhergestellt."""

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
        if self.hub.szene_in_arbeit:
            return "szene"
        return "aktor" if self.hub.programmiert else "bereit"

    @property
    def icon(self) -> str:
        return "mdi:check-circle-outline" if self.native_value == "bereit" else "mdi:progress-wrench"

    @property
    def extra_state_attributes(self) -> dict:
        namen = sorted({g["name"] for g in self.hub.anlage["geraete"] if g["ia"] in self.hub.programmiert})
        return {"szene": self.hub.szene_in_arbeit, "geraete": namen}

"""Numeric parameters of the actuators (brightness levels, times)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import LIGHT_LUX, PERCENTAGE, UnitOfElectricPotential, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry
from .entity import EnetParamEntity, param_entities

UNITS = {"%": PERCENTAGE, "s": UnitOfTime.SECONDS, "ms": UnitOfTime.MILLISECONDS, "h": UnitOfTime.HOURS,
             "Lux": LIGHT_LUX, "V": UnitOfElectricPotential.VOLT}


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(param_entities(entry.runtime_data, EnetParamNumber,
                                        lambda p: p.art == "zahl" and not p.read_only))


class EnetParamNumber(EnetParamEntity, NumberEntity):
    def __init__(self, hub, param_group, param) -> None:
        super().__init__(hub, param_group, param)
        t = param.typ
        self._attr_native_min_value = t.get("min", 0)
        self._attr_native_max_value = t.get("max", 255)
        self._attr_native_step = t.get("schritt") or (0.1 if t.get("kodierung") == "dpt9" else 1)
        self._attr_native_unit_of_measurement = UNITS.get(t.get("einheit", ""), t.get("einheit") or None)
        self._attr_mode = NumberMode.SLIDER if t.get("einheit") == "%" else NumberMode.BOX

    @property
    def native_value(self):
        return self.value

    async def async_set_native_value(self, value: float) -> None:
        await self.write(value)

"""eNet-Szenen (KNX-Szenen in den Aktoren) als Szenen-Entitäten: Aufruf führen die Aktoren selbst aus."""
from __future__ import annotations

from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, szenen_info
from .bridge import BridgeError
from .entity import EnetEntity
from .szenen import Szene
from .texte import tr


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(EnetSzene(hub, s) for s in hub.szenen.szenen if s.art != "ha")  # HA-Szenen: eigene Entität


def szenen_attribute(hub, s: Szene) -> dict[str, Any]:
    namen = {k.key: (f"{k.geraet['name']} {k.nr}" if len(k.geraet.get("kanaele", [])) > 1 else k.geraet["name"])
             for k in hub.kanaele}
    return {"szenen_nr": s.nr, "ga": s.ga, "ga_status": s.ga_status, "art": s.art,
            "mitglieder": [namen.get(m, m) for m in s.mitglieder]}


class EnetSzene(EnetEntity, Scene):
    def __init__(self, hub, szene: Szene) -> None:
        super().__init__(hub)
        self.szene = szene
        self._attr_unique_id = f"{hub.entry_id}_szene_{szene.id}"
        self._attr_name = szene.name
        self._attr_device_info = szenen_info(hub)
        self._attr_extra_state_attributes = szenen_attribute(hub, szene)

    async def async_activate(self, **kwargs: Any) -> None:
        try:
            await self.hub.szenen.aufrufen(self.szene)
        except BridgeError as err:
            raise HomeAssistantError(tr("szene_fehler", name=self.szene.name, err=err)) from err

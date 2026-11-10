"""eNet scenes (KNX scenes stored in the actuators) as scene entities: the actuators execute the recall themselves."""
from __future__ import annotations

from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnetConfigEntry, scenes_device_info
from .bridge import BridgeError
from .entity import EnetEntity
from .scenes import SceneDef
from .texts import tr


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(EnetScene(hub, s) for s in hub.scenes.scenes if s.art != "ha")  # HA scenes: separate entity


def scene_attributes(hub, s: SceneDef) -> dict[str, Any]:
    names_ = {k.key: (f"{k.enet_device['name']} {k.nr}" if len(k.enet_device.get("kanaele", [])) > 1 else k.enet_device["name"])
             for k in hub.channels_}
    return {"szenen_nr": s.nr, "ga": s.ga, "ga_status": s.ga_status, "art": s.art,
            "mitglieder": [names_.get(m, m) for m in s.members]}


class EnetScene(EnetEntity, Scene):
    def __init__(self, hub, scene: SceneDef) -> None:
        super().__init__(hub)
        self.scene = scene
        self._attr_unique_id = f"{hub.entry_id}_szene_{scene.id}"
        self._attr_name = scene.name
        self._attr_device_info = scenes_device_info(hub)
        self._attr_extra_state_attributes = scene_attributes(hub, scene)

    async def async_activate(self, **kwargs: Any) -> None:
        try:
            await self.hub.scenes.call_scene(self.scene)
        except BridgeError as err:
            raise HomeAssistantError(tr("szene_fehler", name=self.scene.name, err=err)) from err

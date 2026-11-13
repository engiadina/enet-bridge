"""Actuator: re-read parameters from the device. eNet scene: store the current state as scene values.
Transmitter: program the pending assignment. (Frequency correction: automatic, hub._track_frequency.)"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


from . import EnetConfigEntry, channel_info, device_info_for, sender, scenes_device_info, rocker_info
from .bridge import BridgeError
from .calibration import calibration
from .entity import EnetEntity
from .texts import tr


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    devices_ = {g.enet_device["sn"]: g.enet_device for g in hub.param_groups}
    sender_ = [g for g in hub.installation["geraete"] if g.get("rolle") == "sender"]
    async_add_entities([ParamRead(hub, g) for g in devices_.values()] +
                       [SceneStore(hub, s) for s in hub.scenes.scenes if s.art != "ha"] +
                       [ProgramSender(hub, g) for g in sender_] +
                       [cls(hub, k) for k in hub.blinds for cls in (MeasureStart, MeasureEnd, MeasureCancel)] +
                       # on the rocker pages only while something is pending (main page: always, e.g. after a reset)
                       [ProgramSender(hub, g, n) for g in sender_ if (w := sender.rocker_count(hub, g)) > 1
                        and sender.pending(g) for n in range(1, w + 1)])


class ParamRead(EnetEntity, ButtonEntity):
    _attr_translation_key = "parameter_lesen"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, enet_device) -> None:
        super().__init__(hub)
        self.enet_device = enet_device
        self._attr_unique_id = f"{enet_device['sn']}_parameter_lesen"
        self._attr_device_info = device_info_for(hub, enet_device)

    async def async_press(self) -> None:
        try:
            await self.hub.read_parameters(self.enet_device)
        except BridgeError as err:
            raise HomeAssistantError(f"{self.enet_device['name']}: {err}") from err


class SceneStore(EnetEntity, ButtonEntity):
    """Learn command: the members store their current state as the values of this scene."""

    _attr_translation_key = "szene_speichern"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, scene) -> None:
        super().__init__(hub)
        self.scene = scene
        self._attr_unique_id = f"{hub.entry_id}_szene_{scene.id}_speichern"
        self._attr_translation_placeholders = {"szene": scene.name}
        self._attr_device_info = scenes_device_info(hub)

    async def async_press(self) -> None:
        try:
            await self.hub.scenes.learn(self.scene)
        except BridgeError as err:
            raise HomeAssistantError(tr("szene_fehler", name=self.scene.name, err=err)) from err


class ProgramSender(EnetEntity, ButtonEntity):
    """Write the pending operating modes (and the rest of the assignment) to the transmitter. Waits until finished so
    that an error shows up as a message in the UI; "press a key on the transmitter" appears in the activity log.
    Also present on each rocker page (likewise writes the whole transmitter)."""

    _attr_translation_key = "sender_programmieren"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, enet_device, rocker: int | None = None) -> None:
        super().__init__(hub)
        self.enet_device = enet_device
        if rocker is None:
            self._attr_unique_id = f"{enet_device['sn']}_sender_programmieren"
            self._attr_device_info = device_info_for(hub, enet_device)
        else:
            self._attr_unique_id = f"{enet_device['sn']}_wippe{rocker}_sender_programmieren"
            self._attr_device_info = rocker_info(hub, enet_device, rocker)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen(f"sender_aktiv_{self.enet_device['sn']}")

    @property
    def available(self) -> bool:
        # locked (greyed out) while programming so that nobody presses it a second time
        return super().available and self.enet_device["sn"] not in self.hub.senders_active

    async def async_press(self) -> None:
        # Second press (also via the button of a rocker page) during a run: reject, do not queue –
        # otherwise it waits for the radio and gets cut off by the reload at the end of the first run.
        if self.enet_device["sn"] in self.hub.senders_active:
            raise HomeAssistantError(tr("programmierung_laeuft_knopf", name=self.enet_device["name"]))
        try:
            await sender.planned_programming(self.hub, self.enet_device)
        except BridgeError as err:
            raise HomeAssistantError(f"{self.enet_device['name']}: {err}") from err
        finally:
            self.hub.reload_after_radio()


class _Measure(EnetEntity, ButtonEntity):
    """Travel time measurement of a blind channel (calibration.py)."""

    _attr_entity_category = EntityCategory.CONFIG
    _suffix = ""

    def __init__(self, hub, channel) -> None:
        super().__init__(hub)
        self.channel = channel
        self._attr_unique_id = f"{channel.enet_device['sn']}_{channel.nr}_{self._suffix}"
        self._attr_device_info = channel_info(hub, channel)

    async def _run(self, job) -> None:
        try:
            await job()
        except BridgeError as err:
            raise HomeAssistantError(f"{self.channel.enet_device['name']}: {err}") from err


class MeasureStart(_Measure):
    _attr_translation_key = "fahrzeit_einmessen"
    _suffix = "einmessen_start"

    async def async_press(self) -> None:
        await self._run(calibration(self.hub, self.channel).start)


class MeasureEnd(_Measure):
    _attr_translation_key = "endlage_erreicht"
    _suffix = "einmessen_endlage"

    async def async_press(self) -> None:
        await self._run(calibration(self.hub, self.channel).end_reached)


class MeasureCancel(_Measure):
    _attr_translation_key = "einmessen_abbrechen"
    _suffix = "einmessen_abbrechen"

    async def async_press(self) -> None:
        await self._run(calibration(self.hub, self.channel).cancel)

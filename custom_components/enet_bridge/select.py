"""Selection parameters of the actuators (operating mode, load type, behaviour on power restoration …).
Transmitter: operating mode and up to two lamps per rocker (pending until "program transmitter") and action per
single key (immediate). Sun sensor: scene per channel and direction (immediate)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_added_domain, async_track_state_removed_domain

from . import EnetConfigEntry, device_info_for, ha_rocker, sender, rocker_info
from .bridge import BridgeError
from .const import DOMAIN, SENSOR_TYPES
from .entity import EnetEntity, EnetParamEntity, param_entities
from .hub import sensor_action_key
from .params import choice
from .texts import tr


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(param_entities(hub, EnetParamSelect, lambda p: p.art == "auswahl" and not p.read_only))
    new = []
    for g in hub.installation["geraete"]:
        if g.get("rolle") != "sender":
            continue
        try:
            assigned = sender.planned_assignment(g)
        except BridgeError:
            continue  # assignment unknown (installation without "ziele"): only via the dialog
        new += [RockerMode(hub, g, n) for n in range(1, sender.rocker_count(hub, g) + 1)]
        new += [RockerLamp(hub, g, n, i) for n in range(1, sender.rocker_count(hub, g) + 1)
                for i in range(1, sender.lamp_choices(g, n) + 1)]
        new += [RockerDimDown(hub, g, n) for n, b in sorted(assigned.items()) if b["art"] == "wippe" and ha_rocker.dim_down_useful(hub, b)]
        new += [KeyAction(hub, g, k) for n, b in sorted(assigned.items()) if b["art"] == "tasten"
                for k in (2 * n - 1, 2 * n)]
    new += [SensorAction(hub, g, channel, on) for g in hub.installation["geraete"] if g.get("typ") in SENSOR_TYPES
            for channel in (1, 2) for on in (True, False)]
    async_add_entities(new)


class EnetParamSelect(EnetParamEntity, SelectEntity):
    def __init__(self, hub, param_group, param) -> None:
        super().__init__(hub, param_group, param)
        self._store_key = {choice(k): k for k in param.typ.get("werte", {})}
        self._attr_options = list(self._store_key)

    @property
    def current_option(self) -> str | None:
        w = self.value
        return choice(w) if w is not None else None

    async def async_select_option(self, option: str) -> None:
        await self.write(self._store_key[option])


class RockerMode(EnetEntity, SelectEntity):
    """Rocker (no lamps = not used) / single keys – only written with "program transmitter"."""

    _attr_translation_key = "wippe_betriebsart"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(sender.OPERATING_MODES)

    def __init__(self, hub, enet_device, n: int) -> None:
        super().__init__(hub)
        self.enet_device, self.n = enet_device, n
        self._attr_unique_id = f"{enet_device['sn']}_wippe{n}_betriebsart"
        self._attr_device_info = rocker_info(hub, enet_device, n)

    @property
    def current_option(self) -> str:
        return sender.mode(self.enet_device, self.n)

    @property
    def extra_state_attributes(self) -> dict:
        return {"vorgemerkt": str(self.n) in self.enet_device.get("geplant", {}),
                "mehrere_lampen": sender.complex_(self.enet_device, self.n)}

    async def async_select_option(self, option: str) -> None:
        if option != self.current_option and sender.complex_(self.enet_device, self.n):
            raise HomeAssistantError(tr("wippe_komplex", name=self.enet_device["name"], n=self.n))
        if option == "tasten" and str(self.n) in self.enet_device.get("geplant_lampen", {}):
            sender.queue_lamps(self.enet_device, self.n, sender.rocker_lamps(dict(self.enet_device, geplant_lampen={}), self.n))
        planned = self.enet_device.setdefault("geplant", {})
        planned[str(self.n)] = option
        g = dict(self.enet_device, geplant={})
        if sender.mode(g, self.n) == option:  # back to the programmed state
            del planned[str(self.n)]
        if not planned:
            self.enet_device.pop("geplant")
        await self.hub.save_installation()
        _pending(self.hub, self.enet_device)
        self.hass.config_entries.async_schedule_reload(self.hub.entry_id)  # key/lamp selection rebuilt




class RockerDimDown(EnetEntity, SelectEntity):
    """Only for rockers with HA lamps: switch the switch off when dimming down (like eNet switching actuators) or leave
    it on. Takes effect immediately (Home Assistant only, nothing on the transmitter)."""

    _attr_translation_key = "wippe_abdimmen"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["aus", "an"]

    def __init__(self, hub, enet_device, n: int) -> None:
        super().__init__(hub)
        self.enet_device, self.n = enet_device, n
        self._attr_unique_id = f"{enet_device['sn']}_wippe{n}_abdimmen"
        self._attr_device_info = rocker_info(hub, enet_device, n)

    @property
    def current_option(self) -> str:
        return "an" if ha_rocker.dim_down_enabled(self.enet_device, self.n) else "aus"

    async def async_select_option(self, option: str) -> None:
        on_ = set(self.enet_device.get("abdimmen_an", [])) - {str(self.n)} | ({str(self.n)} if option == "an" else set())
        if on_:
            self.enet_device["abdimmen_an"] = sorted(on_)
        else:
            self.enet_device.pop("abdimmen_an", None)
        await self.hub.save_installation()
        self.async_write_ha_state()


def _pending(hub, enet_device) -> None:
    if sender.pending(enet_device):
        sender.activity(hub, enet_device, tr("akt_vorgemerkt"))


class RockerLamp(EnetEntity, SelectEntity):
    """Lamp 1/2 of a rocker – pending until "program transmitter". If the rocker has more lamps (from the dialog), it
    shows "several" and cannot be changed here."""

    _attr_translation_key = "wippe_lampe"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def NONE_CHOICE(self) -> str:  # noqa: N802 – shown in the configured language
        return tr("lampe_keine")

    @property
    def SEVERAL(self) -> str:  # noqa: N802
        return tr("lampe_mehrere")

    def __init__(self, hub, enet_device, n: int, i: int) -> None:
        super().__init__(hub)
        self.enet_device, self.n, self.i = enet_device, n, i
        self._attr_unique_id = f"{enet_device['sn']}_wippe{n}_lampe{i}"
        self._attr_translation_placeholders = {"lampe": str(i)}
        self._attr_device_info = rocker_info(hub, enet_device, n)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # HA only delivers the option list together with the state: if a lamp from another integration is added (e.g. a
        # newly added Shelly) or removed, rewrite the state – otherwise it is missing until the next reload
        self.async_on_remove(async_track_state_added_domain(self.hass, ha_rocker.DOMAINS, self._lamps_changed))
        self.async_on_remove(async_track_state_removed_domain(self.hass, ha_rocker.DOMAINS, self._lamps_changed))

    @callback
    def _lamps_changed(self, _event: Event) -> None:
        self.async_write_ha_state()

    def _channels(self) -> dict[str, str]:
        """Display → lamp key: eNet channels, then lights/switches of other integrations (HA switches)."""
        ha = ha_lamps(self.hass)
        names_ = list(ha.values())
        # short "Name (HA)" so the selection fits on the card; entity_id only for duplicate names
        return {**{f"{k.enet_device['name']} K{k.nr} ({k.key})": k.key for k in self.hub.channels_},
                **{tr("lampe_ha_eid" if names_.count(name) > 1 else "lampe_ha", name=name, eid=eid): eid
                   for eid, name in ha.items()}}

    @property
    def options(self) -> list[str]:
        if sender.complex_(self.enet_device, self.n):
            return [self.SEVERAL]
        return [self.NONE_CHOICE, *self._channels()]

    @property
    def current_option(self) -> str | None:
        if sender.complex_(self.enet_device, self.n):
            return self.SEVERAL
        lamps = sender.rocker_lamps(self.enet_device, self.n)
        if len(lamps) < self.i:
            return self.NONE_CHOICE
        return next((a for a, k in self._channels().items() if k == lamps[self.i - 1]), None)

    @property
    def extra_state_attributes(self) -> dict:
        return {"vorgemerkt": str(self.n) in self.enet_device.get("geplant_lampen", {}),
                "mehrere_lampen": sender.complex_(self.enet_device, self.n)}

    async def async_select_option(self, option: str) -> None:
        if sender.complex_(self.enet_device, self.n):
            raise HomeAssistantError(tr("wippe_komplex", name=self.enet_device["name"], n=self.n))
        lamps = sender.rocker_lamps(self.enet_device, self.n)
        new = [*lamps[:self.i - 1], *([self._channels()[option]] if option != self.NONE_CHOICE else []), *lamps[self.i:]]
        sender.queue_lamps(self.enet_device, self.n, new)
        await self.hub.save_installation()
        _pending(self.hub, self.enet_device)
        self.hass.config_entries.async_schedule_reload(self.hub.entry_id)  # lamp 2 may move up


def ha_lamps(hass: HomeAssistant) -> dict[str, str]:
    """Lights and switches not provided by this integration: entity_id → name."""
    reg = er.async_get(hass)
    out = {}
    for st in sorted(hass.states.async_all(ha_rocker.DOMAINS), key=lambda st: st.name.lower()):
        e = reg.async_get(st.entity_id)
        if e is None or (e.platform != DOMAIN and not e.disabled and not e.hidden and e.entity_category is None):
            out[st.entity_id] = st.name
    return out


class KeyAction(EnetEntity, SelectEntity):
    """What the single key triggers (server key in the bridge): takes effect immediately, without programming.
    No entity category, so that the assignment is shown on the device page separately from the configuration
    (operating mode)."""

    _attr_translation_key = "taste_aktion"

    def __init__(self, hub, enet_device, k: int) -> None:
        super().__init__(hub)
        self.enet_device, self.k = enet_device, k
        self._attr_unique_id = f"{enet_device['sn']}_taste{k}_aktion"
        self._attr_translation_placeholders = {"nr": str(k)}
        self._attr_device_info = rocker_info(hub, enet_device, (k + 1) // 2)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen("ha_szenen")  # HA only loads the option list with the state: rewrite it after loading

    def _options(self) -> dict[str, str]:
        return {display: value for value, display in sender.action_options(self.hub)}

    @property
    def options(self) -> list[str]:
        return list(self._options())

    @property
    def current_option(self) -> str | None:
        value = sender.key_action(self.hub, self.enet_device, self.k)
        return next((a for a, w in self._options().items() if w == value), None)

    async def async_select_option(self, option: str) -> None:
        try:
            await sender.set_action(self.hub, self.enet_device, self.k, self._options()[option])
        except BridgeError as err:
            raise HomeAssistantError(tr("taste_fehler", name=self.enet_device["name"], k=self.k, err=err)) from err
        self.async_write_ha_state()


class SensorAction(EnetEntity, SelectEntity):
    """Sonnensensor: Szene bei Sonnenschutz bzw. Dämmerung ein/aus. Die Integration ruft sie auf, wenn das
    Szenentelegramm des Sensors kommt (Hub._sensor_aktion) – sofort wirksam, am Sensor wird nichts programmiert."""

    def __init__(self, hub, enet_device, channel: int, on: bool) -> None:
        super().__init__(hub)
        self.enet_device, self.key = enet_device, sensor_action_key(channel, on)
        self._attr_translation_key = f"{'sonnenschutz' if channel == 1 else 'daemmerung'}_{'ein' if on else 'aus'}_aktion"
        self._attr_unique_id = f"{enet_device['sn']}_sensor_{self.key}_aktion"
        self._attr_device_info = device_info_for(hub, enet_device)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen("ha_szenen")

    def _options(self) -> dict[str, str]:
        # not bridge scenes (esp:): only the firmware runs those, for server keys
        return {display: value for value, display in sender.action_options(self.hub) if not value.startswith("esp:")}

    @property
    def options(self) -> list[str]:
        return list(self._options())

    @property
    def current_option(self) -> str | None:
        value = self.enet_device.get("sensor_aktionen", {}).get(self.key, sender.REPORT)
        if value.startswith("enet:"):  # eNet scene that has since become the eNet part of an HA scene
            s = next((s for s in self.hub.scenes.scenes if f"enet:{s.id}" == value), None)
            if s and s.ha_id:
                value = f"ha:{s.ha_id}"
        return next((a for a, w in self._options().items() if w == value), None)

    async def async_select_option(self, option: str) -> None:
        value = self._options()[option]
        actions = self.enet_device.setdefault("sensor_aktionen", {})
        if value == sender.REPORT:
            actions.pop(self.key, None)
        else:
            actions[self.key] = value
        if not actions:
            self.enet_device.pop("sensor_aktionen")
        await self.hub.save_installation()
        self.async_write_ha_state()

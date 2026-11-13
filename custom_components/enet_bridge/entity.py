"""Common base of the eNet entities."""
from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .bridge import BridgeError
from .hub import Hub
from .params import channel_name
from .texts import tr


class EnetEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    @property
    def available(self) -> bool:
        return self.hub.bridge.connected

    def _listen(self, name: str, handler=None) -> None:
        """Redraw on a hub signal (or call handler)."""
        self.async_on_remove(async_dispatcher_connect(
            self.hass, self.hub.signal(name), handler or self.async_write_ha_state))

    async def async_added_to_hass(self) -> None:
        self._listen("bridge")


class EnetParamEntity(EnetEntity):
    """A device parameter (configuration) or a counter (diagnostics) of an actuator."""

    def __init__(self, hub: Hub, param_group, param) -> None:
        from . import device_info_for, channel_info  # import here: __init__ imports the platforms

        super().__init__(hub)
        self.param_group, self.param = param_group, param
        g = param_group.enet_device
        channel = hub.channel(g, param_group.channel) if param_group.channel is not None else None
        self._attr_device_info = channel_info(hub, channel) if channel else device_info_for(hub, g)
        self._attr_unique_id = f"{g['sn']}_p_{param.key}"
        prefix_ = channel_name(g.get("typ", ""), param_group.channel)
        name = param.name
        if param.id == "MSMT":  # one parameter: slat reversing time (venetian blind) or fabric tensioning (awning)
            mode = hub._param_by_id(param_group, "OPMODE")
            if mode is not None and hub.params.value(g["sn"], mode) == "MARQUEE":
                name = tr("tuchstraffzeit")
        self._attr_name = f"{prefix_} {name}" if prefix_ else name
        self._attr_entity_category = EntityCategory.DIAGNOSTIC if param.read_only else EntityCategory.CONFIG
        self._attr_entity_registry_enabled_default = param.default_active
        self._attr_extra_state_attributes = {"parameter": param.id, "adresse": param.key}

    @property
    def value(self):
        return self.hub.params.value(self.param_group.enet_device["sn"], self.param)

    @property
    def available(self) -> bool:
        return super().available and self.value is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen("params", self._on_params)

    @callback
    def _on_params(self, sn: str) -> None:
        if sn == self.param_group.enet_device["sn"]:
            self.async_write_ha_state()

    async def write(self, value) -> None:
        g = self.param_group.enet_device
        # priority of a special function: PSN[n] (call) and PSN[n+1] (cancel) are kept equal, as the catalogue makes
        # each depend on the other
        partner = None
        if self.param.id.startswith("PSN["):
            from .params import Param

            n = int(self.param.id[4:-1])
            channels_ = self.hub.definitions.get(g.get("typ", ""), {}).get("kanaele", [])
            k = next((k for k in channels_ if k["nr"] == self.param_group.channel), {})
            d = next((d for d in k.get("parameter", []) if d["id"] == f"PSN[{n + 1}]"), None)
            partner = Param(d) if d else None  # no setting of its own (not in the parameter groups)

        async def job() -> None:
            await self.hub.params.write(g["ia"], g["sn"], self.param_group.device_obj, self.param, value)
            if partner is not None:
                await self.hub.params.write(g["ia"], g["sn"], self.param_group.device_obj, partner, value)

        try:
            await self.hub.woken(g, job)
        except BridgeError as err:
            raise HomeAssistantError(f"{g['name']} – {self.param.name}: {err}") from err
        if self.hub.controls_others(self.param_group, self.param):
            # dependent settings that are now fixed get their constant (e.g. slat time 0 for roller shutters)
            for grp, p, const in self.hub.dependent_constants(self.param_group):
                try:
                    await self.hub.params.write(g["ia"], g["sn"], grp.device_obj, p, const)
                except BridgeError as err:
                    raise HomeAssistantError(f"{g['name']} – {p.name}: {err}") from err
            # other settings appear or disappear with this value (visibility.py)
            self.hass.config_entries.async_schedule_reload(self.hub.entry_id)


def param_entities(hub: Hub, cls, condition):
    """All parameters for which condition(param) holds, as entities of class cls."""
    return [cls(hub, gr, p) for gr in hub.param_groups for p in gr.params if condition(p)]


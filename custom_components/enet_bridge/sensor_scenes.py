"""Sun sensor → actuators directly ("eNet direct"), as with the eNet Server: the actuators react to the sensor's
radio telegram themselves, even without Home Assistant.

A sensor channel sends both of its scenes to one group address (GA base·256 + 3 sun protection, + 7 twilight):
"on" (48 / 50 by default) and "off" (49 / 51). An actuator channel is linked to that GA (its scene object, like a
scene of the server) and stores one value for each of the two special-function scenes (SCV1[nr], "… call" and
"… cancel" in the device manuals; with priority 0 they act like normal scenes). There is no "no change" value – a
linked actuator always reacts to both. Hence:

- an eNet light that is part of BOTH scenes chosen for a channel ("… on: scene" and "… off: scene") is programmed
  directly: link + both values;
- everything else of the two scenes (other lights, eNet lights in only one of them) is switched by HA when the
  sensor reports (Hub._sensor_action), the directly programmed lights are left out there.

Programmed state per sensor in geraet["sensor_direkt"]: {"1"|"2": {channel key: [value on, value off]}}.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .bridge import BridgeError
from .ha_scenes import scene_value
from .scenes import GO_SCENE, PID_LINKS, SCV_START
from .texts import tr

if TYPE_CHECKING:
    from .hub import Channel, Hub

_LOGGER = logging.getLogger(__name__)

GA_OFFSET = {1: 3, 2: 7}  # sensor channel → GA offset of its scene telegrams (GO offset 0/4 + 3)


def action_key(channel: int, on: bool) -> str:
    return f"{channel}_{'ein' if on else 'aus'}"


def scene_numbers(hub: Hub, g: dict[str, Any], channel: int) -> tuple[int, int]:
    """(on, off) scene numbers of a sensor channel from its parameters (ASNVE/ASNVU, read 2026-11-06: K1 on =
    exceeded 48, K2 on = below 50); factory setting while they have not been read."""
    values_ = hub.params.values_.get(g["sn"], {})
    obj = channel + 2
    above, below = values_.get(f"{obj}/79/6"), values_.get(f"{obj}/79/7")
    if above is None or below is None:
        return (48, 49) if channel == 1 else (50, 51)
    a, b = int(above, 16), int(below, 16)
    return (a, b) if channel == 1 else (b, a)


def enet_part(hub: Hub, action: str | None) -> dict[str, int]:
    """eNet lights of a chosen scene: channel key → scene value (SCV1 byte)."""
    art, _, value = (action or "").partition(":")
    if art == "ha":
        sc_ = next((s for s in hub.ha_scenes.cfg if str(s["id"]) == value), None)
        if sc_ is None:
            return {}
        lamps = hub.ha_scenes._lamps()
        return {lamps[e].key: scene_value(lamps[e], z if isinstance(z, dict) else {"state": z})
                for e, z in sc_["entities"].items() if e in lamps}
    if art == "enet":
        s = next((s for s in hub.scenes.scenes if str(s.id) == value), None)
        return dict(s.values_) if s else {}
    return {}


def target(hub: Hub, g: dict[str, Any], channel: int) -> dict[str, list[int]]:
    """Lights to program directly for a sensor channel: in both chosen scenes → [value on, value off]."""
    actions = g.get("sensor_aktionen", {})
    on = enet_part(hub, actions.get(action_key(channel, True)))
    off = enet_part(hub, actions.get(action_key(channel, False)))
    return {key: [on[key], off[key]] for key in on if key in off}


def programmed(g: dict[str, Any], channel: int) -> dict[str, list[int]]:
    return g.get("sensor_direkt", {}).get(str(channel), {})


def differs(hub: Hub, g: dict[str, Any]) -> bool:
    """Do the chosen scenes (their current content) differ from what is programmed in the actuators?"""
    return any(target(hub, g, ch) != programmed(g, ch) for ch in GA_OFFSET)


async def _link(hub: Hub, k: Channel, sn: str, ga: int, create: bool) -> None:
    data = bytes([0 if create else 2, 0]) + bytes.fromhex(sn) + ga.to_bytes(2, "big") \
        + k.go("SC", GO_SCENE).to_bytes(2, "big")

    async def steps() -> None:
        await hub.bridge.request("fp_cmd", ia=k.ia, obj=k.obj, pid=PID_LINKS, data=data.hex())

    await hub.params.program(k.ia, hub.device_obj(k.enet_device), k.obj, steps,
                             purpose=tr("zweck_sensor_links"))


async def _values(hub: Hub, k: Channel, nr_on: int, nr_off: int, values_: list[int]) -> None:
    purpose = tr("zweck_sensor_werte")
    obj = hub.device_obj(k.enet_device)
    if nr_off == nr_on + 1:  # both in one frame (actuator restarts once)
        await hub.params.write_raw(k.ia, obj, k.obj, SCV_START + nr_on, bytes(values_), purpose=purpose)
    else:
        for nr, w in ((nr_on, values_[0]), (nr_off, values_[1])):
            await hub.params.write_raw(k.ia, obj, k.obj, SCV_START + nr, bytes([w]), purpose=purpose)


async def program(hub: Hub, g: dict[str, Any]) -> list[str]:
    """Bring the actuators to the target: links to the sensor channel, both scene values. The state is saved after
    every actuator, so an abort leaves it correct. Returns error texts."""
    sn, base = g["sn"], g.get("ga_basis", 0) * 256
    channels_ = {k.key: k for k in hub.channels_}
    stored = g.setdefault("sensor_direkt", {})
    errors: list[str] = []
    for ch, offset in GA_OFFSET.items():
        ga = base + offset
        nr_on, nr_off = scene_numbers(hub, g, ch)
        old, new = programmed(g, ch), target(hub, g, ch)
        done = dict(old)
        for key in sorted(set(old) | set(new)):
            k = channels_.get(key)
            if k is None:
                errors.append(tr("sensor_aktor_fehlt", kanal=key))
                continue
            try:
                if key not in new:
                    await _link(hub, k, sn, ga, create=False)
                    done.pop(key, None)
                else:
                    if key not in old:
                        await _link(hub, k, sn, ga, create=True)
                        done[key] = [-1, -1]  # linked, values not yet written
                    if done[key] != new[key]:
                        await _values(hub, k, nr_on, nr_off, new[key])
                    done[key] = new[key]
            except BridgeError as err:
                errors.append(f"{k.enet_device['name']} K{k.nr}: {err}")
            stored[str(ch)] = done
            if not done:
                stored.pop(str(ch))
            await hub.save_installation()
        _LOGGER.info("%s channel %d: directly linked %s", g["name"], ch, sorted(done))
    if not stored:
        g.pop("sensor_direkt", None)
        await hub.save_installation()
    return errors

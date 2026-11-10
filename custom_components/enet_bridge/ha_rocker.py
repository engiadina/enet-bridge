"""Rocker → HA lamps: Home Assistant switches and dims lamps of other integrations like an eNet actuator.

The rocker sends its key value on press and release (0x40xx left key = on/brighter, 0x80xx right = off/darker,
lower 14 bits hold time in 10 ms; 0 = pressed). A short press switches on release; held (≥ DIM_FROM_S) it dims in
steps until released. If the release is lost, dimming ends after DIM_MAX_S; if the press is lost, the hold time
in the release counts.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_SUPPORTED_COLOR_MODES, ColorMode
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON

from .const import KIND_DIMMER

if TYPE_CHECKING:
    from .hub import Hub, Key

_LOGGER = logging.getLogger(__name__)

DIM_FROM_S = 0.5      # held this long: dim instead of switch
DIM_INTERVAL_S = 0.3    # interval between dimming steps
DIM_STEP = 7     # percent per step (0 → 100 % in ≈ 4.5 s)
DIM_MAX_S = 10.0    # safety in case the release does not arrive
MIN_BRIGHTNESS = 3     # do not dim below this brightness (0–255): darker does not switch off (like eNet)
DOMAINS = ("light", "switch")


def dim_down_enabled(enet_device: dict, n: int) -> bool:
    """Setting per rocker: switches (non-dimmable HA lamps) stay on when dimming down. Stored with the device, not in
    the key entry, so it survives reprogramming the transmitter. Default: off, like an eNet switch actuator."""
    return str(n) in enet_device.get("abdimmen_an", [])


def dimmable(hass, eid: str, unknown: bool = False) -> bool:
    """Is the HA lamp dimmable? Without a state (e.g. at startup, integration not loaded yet) `unknown` applies."""
    if not eid.startswith("light."):
        return False
    if (st := hass.states.get(eid)) is None:
        return unknown
    modes = st.attributes.get(ATTR_SUPPORTED_COLOR_MODES) or []
    return any(m not in (ColorMode.ONOFF, ColorMode.UNKNOWN) for m in modes)


def dim_down_useful(hub: Hub, assignment: dict) -> bool:
    """Offer "switches when dimming down" only if the rocker controls an HA switch AND something dimmable (eNet dimmer
    or dimmable HA lamp) – otherwise there is no dimming down or no switch for it to act on."""
    ha = assignment.get("ha_ziele") or []
    if not any(not dimmable(hub.hass, e, unknown=True) for e in ha):
        return False
    enet_dimmer = any(k.art == KIND_DIMMER for z in assignment.get("ziele") or []
                      for k in hub.channels_ if k.ia == z["ia"] and k.nr == z["ch"])
    return enet_dimmer or any(dimmable(hub.hass, e, unknown=True) for e in ha)


def ha_targets(key_obj: Key) -> list[str]:
    entry_ = next((t for t in key_obj.enet_device.get("tasten", []) if t["nr"] == key_obj.nr), {})
    return list(entry_.get("ha_ziele", []))


class HaRocker:
    def __init__(self, hub: Hub) -> None:
        self.hub, self.hass = hub, hub.hass
        self._running: dict[str, tuple[asyncio.Task, bool]] = {}  # key → (press task, left)
        self._dimming: set[str] = set()  # keys whose press is already dimming (release then does not switch)

    def stop(self) -> None:
        for task, _ in self._running.values():
            task.cancel()
        self._running.clear()

    def key_press(self, key_obj: Key, msg: dict[str, Any]) -> None:
        targets = [e for e in ha_targets(key_obj) if e.split(".")[0] in DOMAINS]
        if not targets:
            return
        cmd = int(msg.get("cmd", "0") or "0", 16)
        direction = cmd & 0xC000
        if direction not in (0x4000, 0x8000):
            return  # both keys (store switch-on brightness) – only affects eNet dimmers
        links = direction == 0x4000
        hold = (cmd & 0x3FFF) * 10
        running = self._running.pop(key_obj.key, None)
        if hold == 0:  # press
            if running:
                running[0].cancel()
                self._dimming.discard(key_obj.key)
            keep_on = dim_down_enabled(key_obj.enet_device, (key_obj.nr + 1) // 2)
            task = self.hass.async_create_background_task(self._pressed(key_obj.key, targets, links, keep_on),
                                                          "enet Wippe HA-Lampen")
            self._running[key_obj.key] = (task, links)
            return
        # release: still within the wait time = short press; if already dimming, it stops
        if running and not running[0].done():
            running[0].cancel()
            if running[1] == links and key_obj.key not in self._dimming:
                self._switch(targets, links)
        elif running is None and hold < DIM_FROM_S * 1000:
            self._switch(targets, links)  # press not received

    def _switch(self, targets: list[str], on: bool) -> None:
        self.hass.async_create_background_task(
            self.hass.services.async_call("homeassistant", "turn_on" if on else "turn_off", {ATTR_ENTITY_ID: targets}),
            "enet Wippe schalten")

    async def _pressed(self, key: str, targets: list[str], brighter: bool, keep_on: bool = False) -> None:
        await asyncio.sleep(DIM_FROM_S)
        task = asyncio.current_task()
        self._dimming.add(key)  # from here on: release only ends the dimming
        dimmables = [e for e in targets if dimmable(self.hass, e)]
        switch_ = [e for e in targets if e not in dimmables]
        try:
            # switches: dimming up switches on, dimming down switches off like an eNet switch actuator
            # – unless "keep on"
            if switch_ and (brighter or not keep_on):
                self._switch(switch_, brighter)
            if not dimmables:
                return
            for _ in range(int(DIM_MAX_S / DIM_INTERVAL_S)):
                if brighter:
                    step = dimmables
                else:  # darker only while the lamp is on and above the minimum brightness
                    step = [e for e in dimmables if (st := self.hass.states.get(e)) is not None
                               and st.state == STATE_ON
                               and (st.attributes.get(ATTR_BRIGHTNESS) or 0) > MIN_BRIGHTNESS + DIM_STEP * 255 / 100]
                if not step:
                    return
                await self.hass.services.async_call(
                    "light", "turn_on", {ATTR_ENTITY_ID: step,
                                         "brightness_step_pct": DIM_STEP if brighter else -DIM_STEP})
                await asyncio.sleep(DIM_INTERVAL_S)
        except Exception:  # noqa: BLE001 – a foreign lamp must not disturb key processing
            _LOGGER.exception("Dimming %s failed", dimmables)
        finally:
            self._dimming.discard(key)
            if self._running.get(key, (None,))[0] is task:
                self._running.pop(key, None)

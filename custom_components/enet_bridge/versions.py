"""Firmware version of the eNet devices (shown in HA as "Firmware" and "Hardware" on the device page).

Object 0 of every device (as read by the eNet Server):
  PID 0xD5 software ID (array, element 1 = 6 bytes, e.g. ffff45062651 → "45062651", catalogue DeviceSoftware.xml)
  PID 0xD6 stack version, 0xD7 application version, 0xD8 update stack version: 4 bytes, the last three as hex
           digits (ff010258 → "1.2.58")
  PID 0xDB radio PCB, SMT number (6 bytes, ffff73356201 → "73356201")
Stored per device as "firmware": {"kennung", "anwendung", "stack", "update_stack", "platine"}; only the fields that
were read. From a server export the ID and the application version come along.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr

from .bridge import BridgeError
from .const import DOMAIN, SENSOR_TYPES

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

# key → (PID, decoder)
_PIDS = {"kennung": 0xD5, "anwendung": 0xD7, "stack": 0xD6, "update_stack": 0xD8, "platine": 0xDB}


def _version(data: bytes) -> str | None:
    """ff010258 → 1.2.58 (hex digits, as the catalogue writes them)."""
    if len(data) < 4 or data[1:4] == b"\xff\xff\xff":
        return None
    return f"{data[1]:x}.{data[2]:x}.{data[3]:x}"  # 04 00 03 → 4.0.3, 00 02 16 → 0.2.16


def _number(data: bytes) -> str | None:
    """ffff45062651 → 45062651 (the number without the ff fill bytes)."""
    s = data.hex().lstrip("f")
    return s if s and set(s) - {"f", "e"} else None


def decode(key: str, data: bytes) -> str | None:
    return _version(data) if key in ("anwendung", "stack", "update_stack") else _number(data)


def sw_version(enet_device: dict[str, Any]) -> str | None:
    """'1.2.58 (45062651)' for the device page."""
    fw = enet_device.get("firmware") or {}
    app, sw_id = fw.get("anwendung"), fw.get("kennung")
    if app and sw_id:
        return f"{app} ({sw_id})"
    return app or sw_id


def hw_version(enet_device: dict[str, Any]) -> str | None:
    return (enet_device.get("firmware") or {}).get("platine")


async def read(hub: Hub, ia: str, pr: Callable[[int, int], Awaitable[bytes]] | None = None) -> dict[str, str]:
    """Read the versions of a device (actuator or awake transmitter). Missing answers are left out. pr: property
    read of a running radio session (transmitter while it is kept awake), otherwise a plain request."""
    async def plain(obj: int, pid: int) -> bytes:
        r = await hub.bridge.request("prop_read", ia=ia, obj=obj, pid=pid, start=1, count=1)
        return bytes.fromhex(r.get("value", ""))

    pr = pr or plain
    result: dict[str, str] = {}
    for key, pid in _PIDS.items():
        try:
            data = await pr(0, pid)
        except BridgeError as err:
            _LOGGER.debug("%s: PID 0x%02X not read: %s", ia, pid, err)
            if not result:  # first one unanswered: the device is not reachable, don't wait for the others
                return result
            continue
        if value := decode(key, data):
            result[key] = value
    return result


def update_registry(hub: Hub, enet_device: dict[str, Any]) -> None:
    """Show the versions on the existing HA device (new devices get them via device_info_for)."""
    reg = dr.async_get(hub.hass)
    if device := reg.async_get_device(identifiers={(DOMAIN, enet_device["sn"])}):
        reg.async_update_device(device.id, sw_version=sw_version(enet_device), hw_version=hw_version(enet_device))


async def backfill(hub: Hub) -> None:
    """Once per device: read the versions of actuators that don't have them yet (transmitters and the sun sensor
    sleep – they get them when paired again). Read only."""
    missing = [g for g in hub.installation["geraete"]
               if g.get("rolle") != "sender" and g.get("typ") not in SENSOR_TYPES
               and "platine" not in (g.get("firmware") or {})]
    changed = False
    for g in missing:
        versions = await read(hub, g["ia"])
        if not versions:
            continue
        g["firmware"] = {**(g.get("firmware") or {}), **versions}
        update_registry(hub, g)
        changed = True
        _LOGGER.info("%s (%s): firmware %s", g["name"], g["ia"], sw_version(g))
        await asyncio.sleep(0.2)
    if changed:
        await hub.save_installation()

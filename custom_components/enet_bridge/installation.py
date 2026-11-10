"""The installation (server identity, devices, scenes, ESP actions) in Home Assistant's store.

HA owns the data: the installation is created during setup (server export, new installation or existing
anlage.json) and afterwards lives only in .storage/enet_anlage_<entry>. anlage.json is import only.
Format as in anlage.json (tools/iex.py); additionally `server.funktionen` = desired server switch of the bridge.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_INSTALLATION, CONF_INSTALLATION_DATA, DOMAIN
from .texts import tr

SERVER_IA = "7.15.255"  # fixed address of the eNet Server (NOTES.md)


def new_installation() -> dict[str, Any]:
    """Empty installation with its own domain and server identity (same form as the real server: 000a + 4 bytes)."""
    return {
        "server": {"ia": SERVER_IA, "domain": "000a" + secrets.token_hex(4), "sn": "000a" + secrets.token_hex(4),
                   "verschluesselt": False, "funktionen": True},
        "geraete": [],
        "szenen": [],
    }


def verify(installation: dict[str, Any]) -> dict[str, Any]:
    if "geraete" not in installation or "server" not in installation:
        raise ValueError(tr("keine_enet_anlage"))
    return installation


def load_file(path_: str) -> dict[str, Any]:
    """Read anlage.json (runs in the executor)."""
    with open(path_, encoding="utf-8") as f:
        return verify(json.load(f))


class InstallationStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._store: Store = Store(hass, 1, f"{DOMAIN}_anlage_{entry_id}")

    async def laden(self, entry: ConfigEntry) -> dict[str, Any]:
        """From the store; on first start from setup (entry data) or from anlage.json."""
        installation = await self._store.async_load()
        if installation is not None:
            return verify(installation)
        if CONF_INSTALLATION_DATA in entry.data:
            installation = verify(entry.data[CONF_INSTALLATION_DATA])
        else:
            installation = await self.hass.async_add_executor_job(load_file, entry.data[CONF_INSTALLATION])
        await self.save(installation)
        if CONF_INSTALLATION_DATA in entry.data:  # only once: afterwards the store is authoritative
            self.hass.config_entries.async_update_entry(
                entry, data={k: v for k, v in entry.data.items() if k != CONF_INSTALLATION_DATA})
        return installation

    async def save(self, installation: dict[str, Any]) -> None:
        await self._store.async_save(installation)

"""Die Anlage (Server-Kennung, Geräte, Szenen, ESP-Aktionen) im Speicher von Home Assistant.

HA ist Herr der Daten: Die Anlage entsteht bei der Einrichtung (Server-Export, neue Anlage oder bestehende
anlage.json) und liegt danach nur noch in .storage/enet_anlage_<entry>. anlage.json ist reiner Import.
Format wie anlage.json (tools/iex.py); zusätzlich server.funktionen = gewünschter Server-Schalter der Bridge.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_ANLAGE, CONF_ANLAGE_DATEN, DOMAIN
from .texte import tr

SERVER_IA = "7.15.255"  # feste Adresse des eNet Servers (NOTES.md)


def neue_anlage() -> dict[str, Any]:
    """Leere Anlage mit eigener Domäne und Server-Kennung (Form wie beim echten Server: 000a + 4 Byte)."""
    return {
        "server": {"ia": SERVER_IA, "domain": "000a" + secrets.token_hex(4), "sn": "000a" + secrets.token_hex(4),
                   "verschluesselt": False, "funktionen": True},
        "geraete": [],
        "szenen": [],
    }


def pruefen(anlage: dict[str, Any]) -> dict[str, Any]:
    if "geraete" not in anlage or "server" not in anlage:
        raise ValueError(tr("keine_enet_anlage"))
    return anlage


def lade_datei(pfad: str) -> dict[str, Any]:
    """anlage.json lesen (läuft im Executor)."""
    with open(pfad, encoding="utf-8") as f:
        return pruefen(json.load(f))


class AnlageSpeicher:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._store: Store = Store(hass, 1, f"{DOMAIN}_anlage_{entry_id}")

    async def laden(self, entry: ConfigEntry) -> dict[str, Any]:
        """Aus dem Speicher; beim ersten Start aus der Einrichtung (Eintragsdaten) oder aus anlage.json."""
        anlage = await self._store.async_load()
        if anlage is not None:
            return pruefen(anlage)
        if CONF_ANLAGE_DATEN in entry.data:
            anlage = pruefen(entry.data[CONF_ANLAGE_DATEN])
        else:
            anlage = await self.hass.async_add_executor_job(lade_datei, entry.data[CONF_ANLAGE])
        await self.speichern(anlage)
        if CONF_ANLAGE_DATEN in entry.data:  # nur einmal: danach ist der Speicher maßgeblich
            self.hass.config_entries.async_update_entry(
                entry, data={k: v for k, v in entry.data.items() if k != CONF_ANLAGE_DATEN})
        return anlage

    async def speichern(self, anlage: dict[str, Any]) -> None:
        await self._store.async_save(anlage)

"""„Diagnosedaten herunterladen“: Anlage, Zustände, Prüfergebnisse und die letzten Bridge-Nachrichten als JSON.

Für Fehlerberichte anderer Nutzer. Domäne und Seriennummern erlauben es, Geräte per Funk anzusprechen – sie werden
überall ersetzt (auch in Funktelegrammen, wo sie als Hex-Teilstrings stehen), ebenso Namen, Räume und die
entity_ids anderer Integrationen. Die Ersetzung ist innerhalb einer Datei eindeutig (SN1, SN2 …), damit die
Bezüge zwischen Geräten, Tasten und Telegrammen lesbar bleiben.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import re
from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import EnetConfigEntry
from .bridge import BridgeError
from .const import DOMAIN

NAMENS_SCHLUESSEL = {"name", "raum", "szene", "titel"}
ENTITY_ID = re.compile(r"^(light|switch|scene|script|automation)\.[a-z0-9_]+$")


class _Anonym:
    def __init__(self) -> None:
        self.geheim: dict[str, str] = {}  # Hex-Kennung (klein) → Ersatz
        self.namen: dict[str, str] = {}
        self._muster: re.Pattern | None = None

    def kennung(self, wert: Any, art: str) -> None:
        if isinstance(wert, str) and len(wert) >= 4 and wert.lower() not in self.geheim:
            n = sum(1 for v in self.geheim.values() if v.startswith(art)) + 1
            self.geheim[wert.lower()] = f"{art}{n}"

    def name(self, wert: str) -> str:
        if wert not in self.namen:
            self.namen[wert] = f"Name{len(self.namen) + 1}"
        return self.namen[wert]

    def namen_sammeln(self, obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in NAMENS_SCHLUESSEL and isinstance(v, str) and v:
                    self.name(v)
                else:
                    self.namen_sammeln(v)
        elif isinstance(obj, list | tuple):
            for v in obj:
                self.namen_sammeln(v)

    def _text(self, s: str) -> str:
        if ENTITY_ID.match(s):
            return f"{s.split('.')[0]}.{self.name(s)}"
        if self._muster is None:
            self._muster = re.compile("|".join(re.escape(k) for k in sorted(self.geheim, key=len, reverse=True))
                                      or r"(?!)", re.IGNORECASE)
        s = self._muster.sub(lambda m: f"<{self.geheim[m.group(0).lower()]}>", s)
        for original in sorted(self.namen, key=len, reverse=True):
            if len(original) >= 3 and original in s:
                s = s.replace(original, self.namen[original])
        return s

    def anwenden(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {self._text(k) if isinstance(k, str) else k: self._wert(k, v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.anwenden(v) for v in obj]
        if isinstance(obj, str):
            return self._text(obj)
        return obj

    def _wert(self, schluessel: Any, v: Any) -> Any:
        if schluessel in NAMENS_SCHLUESSEL and isinstance(v, str) and v:
            return self.name(v)
        return self.anwenden(v)


def _daten(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _daten(getattr(obj, f.name)) for f in dataclasses.fields(obj)
                if not f.name.startswith("_") and f.name != "geraet"}
    if isinstance(obj, dict):
        return {str(k): _daten(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [_daten(v) for v in obj]
    if isinstance(obj, str | int | float | bool) or obj is None:
        return obj
    return str(obj)


def _seriennummern(obj: Any, anonym: _Anonym) -> None:
    """Alle Seriennummern der Anlage (Geräte, entfernte Geräte, Sender der Server-Tasten)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("sn", "sender") and isinstance(v, str):
                anonym.kennung(v, "SN")
            else:
                _seriennummern(v, anonym)
    elif isinstance(obj, list):
        for v in obj:
            _seriennummern(v, anonym)


async def _rauschen(hub) -> dict[str, Any] | None:
    """Grundrauschen messen (Firmware-Befehl diag: 6 s RSSI min/max und Anteil Carrier Sense; Empfang so lange aus)."""
    if not hub.bridge.connected or hub.keine_bridge:
        return None
    fertig = asyncio.get_running_loop().create_future()

    def warten(msg: dict[str, Any]) -> None:
        if msg.get("ev") == "diag" and not fertig.done():
            fertig.set_result(msg)

    abmelden = hub.bridge.add_listener(warten)
    try:
        await hub.bridge.command("diag")
        ergebnis = dict(await asyncio.wait_for(fertig, 10.0))
    except (BridgeError, asyncio.TimeoutError) as err:
        return {"fehler": str(err) or type(err).__name__}
    finally:
        abmelden()
    ergebnis.pop("ev", None)
    return ergebnis


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EnetConfigEntry) -> dict[str, Any]:
    hub = entry.runtime_data
    anlage = copy.deepcopy(hub.anlage)
    integration = await async_get_integration(hass, DOMAIN)

    roh = {
        "versionen": {
            "integration": str(integration.version),
            "home_assistant": HA_VERSION,
            "firmware_mitgeliefert": hub.fw_bundle,
        },
        "bridge": {
            "verbunden": hub.bridge.connected,
            "info": copy.deepcopy(hub.bridge.info),
            "mithoeren": hub.mithoeren,
            "keine_bridge": hub.keine_bridge,
        },
        "anlage": anlage,
        "szenen": _daten(hub.szenen.szenen),
        "pruefung": {"fehlende_links": _daten(hub.pruefung.fehlende_links)},
        "zustand": _daten(hub.zustand),
        "funk": _daten(hub.diag),
        "messung": hub.messung.diagnose(),
        "rauschen": await _rauschen(hub),
        "parameter": copy.deepcopy(hub.params.werte),
        "sensoren": {"helligkeit": dict(hub.helligkeit), "aktiv": dict(hub.sensor_aktiv),
                     "batterie_schwach": dict(hub.batterie_schwach)},
        "unbekannte_geraete": copy.deepcopy(hub.unbekannte_geraete),
        "laufend": {"sender_programmieren": sorted(hub.sender_aktiv), "aktoren_programmieren": sorted(hub.programmiert)},
        "bridge_verlauf": [f"{zeit} {richtung} {text}" for zeit, richtung, text in hub.bridge.verlauf],
    }

    anonym = _Anonym()
    server = anlage.get("server", {})
    anonym.kennung(server.get("domain"), "DOMAENE")
    anonym.kennung(server.get("sn"), "SERVER")
    _seriennummern(anlage, anonym)
    for sn in [*hub.params.werte, *hub.unbekannte_geraete]:
        anonym.kennung(sn, "SN")
    anonym.namen_sammeln(roh)
    return anonym.anwenden(roh)

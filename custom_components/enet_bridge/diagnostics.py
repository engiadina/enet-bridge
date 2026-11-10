"""Download diagnostics: installation, states, check results and the last bridge messages as JSON.

For bug reports from other users. Domain and serial numbers allow addressing devices over the radio – they are
replaced everywhere (also in radio telegrams, where they appear as hex substrings), as are names, rooms and the
entity_ids of other integrations. The replacement is unique within one file (SN1, SN2 …), so the references
between devices, keys and telegrams stay readable.
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

NAME_KEYS = {"name", "raum", "szene", "titel"}
ENTITY_ID = re.compile(r"^(light|switch|scene|script|automation)\.[a-z0-9_]+$")


class _Anonymizer:
    def __init__(self) -> None:
        self.secret: dict[str, str] = {}  # hex identifier (lower case) → replacement
        self.names_: dict[str, str] = {}
        self._pattern: re.Pattern | None = None

    def ident(self, value: Any, art: str) -> None:
        if isinstance(value, str) and len(value) >= 4 and value.lower() not in self.secret:
            n = sum(1 for v in self.secret.values() if v.startswith(art)) + 1
            self.secret[value.lower()] = f"{art}{n}"

    def name(self, value: str) -> str:
        if value not in self.names_:
            self.names_[value] = f"Name{len(self.names_) + 1}"
        return self.names_[value]

    def collect_names(self, obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in NAME_KEYS and isinstance(v, str) and v:
                    self.name(v)
                else:
                    self.collect_names(v)
        elif isinstance(obj, list | tuple):
            for v in obj:
                self.collect_names(v)

    def _text(self, s: str) -> str:
        if ENTITY_ID.match(s):
            return f"{s.split('.')[0]}.{self.name(s)}"
        if self._pattern is None:
            self._pattern = re.compile("|".join(re.escape(k) for k in sorted(self.secret, key=len, reverse=True))
                                      or r"(?!)", re.IGNORECASE)
        s = self._pattern.sub(lambda m: f"<{self.secret[m.group(0).lower()]}>", s)
        for original in sorted(self.names_, key=len, reverse=True):
            if len(original) >= 3 and original in s:
                s = s.replace(original, self.names_[original])
        return s

    def apply(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {self._text(k) if isinstance(k, str) else k: self._value(k, v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.apply(v) for v in obj]
        if isinstance(obj, str):
            return self._text(obj)
        return obj

    def _value(self, key_: Any, v: Any) -> Any:
        if key_ in NAME_KEYS and isinstance(v, str) and v:
            return self.name(v)
        return self.apply(v)


def _data(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _data(getattr(obj, f.name)) for f in dataclasses.fields(obj)
                if not f.name.startswith("_") and f.name != "geraet"}
    if isinstance(obj, dict):
        return {str(k): _data(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [_data(v) for v in obj]
    if isinstance(obj, str | int | float | bool) or obj is None:
        return obj
    return str(obj)


def _serial_numbers(obj: Any, anon: _Anonymizer) -> None:
    """All serial numbers of the installation (devices, removed devices, transmitters of the server keys)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("sn", "sender") and isinstance(v, str):
                anon.ident(v, "SN")
            else:
                _serial_numbers(v, anon)
    elif isinstance(obj, list):
        for v in obj:
            _serial_numbers(v, anon)


async def _noise(hub) -> dict[str, Any] | None:
    """Measure the noise floor (firmware command diag: 6 s RSSI min/max and carrier sense share;
    no reception meanwhile)."""
    if not hub.bridge.connected or hub.no_bridge:
        return None
    done = asyncio.get_running_loop().create_future()

    def wait(msg: dict[str, Any]) -> None:
        if msg.get("ev") == "diag" and not done.done():
            done.set_result(msg)

    unsubscribe = hub.bridge.add_listener(wait)
    try:
        await hub.bridge.command("diag")
        outcome = dict(await asyncio.wait_for(done, 10.0))
    except (BridgeError, asyncio.TimeoutError) as err:
        return {"fehler": str(err) or type(err).__name__}
    finally:
        unsubscribe()
    outcome.pop("ev", None)
    return outcome


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EnetConfigEntry) -> dict[str, Any]:
    hub = entry.runtime_data
    installation = copy.deepcopy(hub.installation)
    integration = await async_get_integration(hass, DOMAIN)

    raw_ = {
        "versionen": {
            "integration": str(integration.version),
            "home_assistant": HA_VERSION,
            "firmware_mitgeliefert": hub.fw_bundle,
        },
        "bridge": {
            "verbunden": hub.bridge.connected,
            "info": copy.deepcopy(hub.bridge.info),
            "mithoeren": hub.listen_only,
            "keine_bridge": hub.no_bridge,
        },
        "anlage": installation,
        "szenen": _data(hub.scenes.scenes),
        "pruefung": {"fehlende_links": _data(hub.check.missing_links)},
        "zustand": _data(hub.state_),
        "funk": _data(hub.diag),
        "messung": hub.measurements.diagnose(),
        "rauschen": await _noise(hub),
        "parameter": copy.deepcopy(hub.params.values_),
        "sensoren": {"helligkeit": dict(hub.illuminance), "aktiv": dict(hub.sensor_active),
                     "batterie_schwach": dict(hub.battery_low)},
        "unbekannte_geraete": copy.deepcopy(hub.unknown_devices),
        "laufend": {"sender_programmieren": sorted(hub.senders_active), "aktoren_programmieren": sorted(hub.programmed)},
        "bridge_verlauf": [f"{time_} {direction} {text}" for time_, direction, text in hub.bridge.history],
    }

    anon = _Anonymizer()
    server = installation.get("server", {})
    anon.ident(server.get("domain"), "DOMAENE")
    anon.ident(server.get("sn"), "SERVER")
    _serial_numbers(installation, anon)
    for sn in [*hub.params.values_, *hub.unknown_devices]:
        anon.ident(sn, "SN")
    anon.collect_names(raw_)
    return anon.apply(raw_)

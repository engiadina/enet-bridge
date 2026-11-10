"""Group object numbers per channel type.

The catalog (ChannelTypes.xml, `kanaltypen` in definitionen.json) numbers the group objects of a channel per channel
type. Installations often switch a channel from its default type to a variant – e.g. the switch channels of a DIN-rail
switch/blind actuator run as CT_1F05 ("Switching Actuator, reduced GO"), where the status is GO 17 instead of 20. So
the GO of a function is looked up by the channel's type; channels stored without a type (installations before 0.14)
use the default type of their kind, which is what the integration assumed before.
"""
from __future__ import annotations

import json
from typing import Any

# kind of a stored channel → default channel type
DEFAULT_TYPES = {"schalten": "CT_1F01", "dimmen": "CT_1F02", "jalousie": "CT_1F03", "energie": "CT_1F19",
                 "bewegung": "CT_1F1B", "repeater": "CT_1F06", "wippe": "CT_1F11", "taste": "CT_1F13"}

# Fallback when definitionen.json has no `kanaltypen` (a local file of an older version): the layouts the integration
# assumed before (catalog fw 2.21).
_FALLBACK: dict[str, dict[str, int]] = {
    "CT_1F01": {"GBR": 1, "SOO": 2, "ASC": 4, "SC": 6, "SSGET": 19, "IOO": 20, "SSGI": 22},
    "CT_1F02": {"GBR": 1, "SOO": 2, "ASC": 4, "SC": 6, "SSGET": 19, "IOO": 20, "ADV": 21, "SSGI": 22},
    "CT_1F03": {"GBR": 1, "MUD": 2, "SSUD": 3, "STOP": 4, "SAPBP": 5, "SAPSP": 6, "SC": 7, "SSGET": 9,
                "IMUDS": 10, "SSGI": 11, "CAPBP": 12, "CAPSP": 13},
}

_layouts: dict[str, dict[str, int]] = {}


def load(path_: str) -> dict[str, dict[str, int]]:
    """Read the channel type layouts from definitionen.json (executor)."""
    try:
        with open(path_, encoding="utf-8") as f:
            types_ = json.load(f).get("kanaltypen", {})
    except FileNotFoundError:
        return {}
    return {ct: v["go"] for ct, v in types_.items() if isinstance(v, dict) and "go" in v}


def set_layouts(layouts: dict[str, dict[str, int]]) -> None:
    _layouts.clear()
    _layouts.update(layouts)


def channel_type(channel: dict[str, Any]) -> str:
    """Channel type of a stored channel (`kanaltyp`, otherwise the default type of its kind)."""
    return channel.get("kanaltyp") or DEFAULT_TYPES.get(channel.get("art", ""), "")


def layout(ct: str) -> dict[str, int]:
    return _layouts.get(ct) or _FALLBACK.get(ct, {})


def go(channel: dict[str, Any], function: str) -> int | None:
    """GO number of a device function (e.g. "IOO") in a stored channel, None if the type has no such function."""
    return layout(channel_type(channel)).get(function)


def go_of_type(ct: str, function: str) -> int | None:
    return layout(ct).get(function)

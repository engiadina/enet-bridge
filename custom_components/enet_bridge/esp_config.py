"""Build the bridge's server configuration (format version 3, src/server.h) from anlage.json.

No Home Assistant dependencies: also used by tools/server_cfg.py.
Channels come from all actuator channels of the installation; local scenes and server keys come from anlage["esp"]
(from the links of the server export, iex.py, or by hand: `"szenen"` {name: [{ia, handle, wert}]},
`"server_tasten"` [{name, sender, ga, szene}]). A key without `"szene"` is only reported – HA decides then.
"""
from __future__ import annotations

from typing import Any

try:
    from .texts import tr
except ImportError:  # tools/server_cfg.py loads the module without a package
    from texts import tr

PART_HEX = 400
# Channel flags in the blob (src/server.h): bit 0 dimmer, bit 1 blind (since firmware 0.8.20)
FLAGS = {"schalten": 0, "dimmen": 1, "jalousie": 2}  # hex characters per config command (line < 512 bytes)


def crc16(data: bytes) -> int:
    """Same as knxrf::crc16: polynomial 0x3D65, init 0, inverted."""
    r = 0
    for b in data:
        r ^= b << 8
        for _ in range(8):
            r = ((r << 1) ^ 0x3D65) & 0xFFFF if r & 0x8000 else (r << 1) & 0xFFFF
    return r ^ 0xFFFF


def ia_int(s: str) -> int:
    a, b, c = (int(x) for x in s.split("."))
    return a << 12 | b << 8 | c


def build(installation: dict[str, Any], esp: dict[str, Any] | None = None):
    """Return (blob, channels, scene names, keys). esp overrides anlage["esp"]."""
    esp = installation.get("esp", {}) if esp is None else esp
    srv = installation["server"]
    channels = []
    for d in installation["geraete"]:
        for k in d["kanaele"]:
            if k["art"] not in FLAGS:
                continue
            channels.append((ia_int(d["ia"]), d["sn"], k["nr"], k["offset"], k.get("obj", 2 + k["nr"]),
                             FLAGS[k["art"]], d.get("ga_basis", 0)))
    scene_names = list(esp.get("szenen", {}))
    actions, scenes = [], []
    for name in scene_names:
        first = len(actions)
        for a in esp["szenen"][name]:
            actions.append((ia_int(a["ia"]), a["handle"], bytes.fromhex(a["wert"])))
        scenes.append((first, len(actions) - first))
    keys = []
    for k in esp.get("server_tasten", []):
        sc = scene_names.index(k["szene"]) if k.get("szene") else -1
        keys.append((k["sender"], k["ga"], sc, k.get("name", "")))

    b = bytearray(b"E\x03") + bytes.fromhex(srv["domain"]) + bytes.fromhex(srv["sn"])
    b += ia_int(srv["ia"]).to_bytes(2, "big") + bytes([len(channels), len(actions), len(scenes), len(keys)])
    for ia, sn, ch, off, obj, flags, base in channels:
        b += ia.to_bytes(2, "big") + bytes.fromhex(sn) + bytes([ch, off, obj, flags, base])
    for ia, h, v in actions:
        if not 1 <= len(v) <= 4:
            raise ValueError(tr("wert_laenge", wert=v.hex()))
        b += ia.to_bytes(2, "big") + h.to_bytes(2, "big") + bytes([len(v)]) + v
    for first, count in scenes:
        b += bytes([first, count])
    for sn, ga, sc, _ in keys:
        b += bytes.fromhex(sn) + ga.to_bytes(2, "big") + (sc & 0xFF).to_bytes(1, "big")
    b += crc16(b).to_bytes(2, "big")
    return bytes(b), channels, scene_names, keys


def split_parts(blob: bytes) -> list[str]:
    """Split the blob into hex parts for the config command."""
    hx = blob.hex()
    return [hx[i:i + PART_HEX] for i in range(0, len(hx), PART_HEX)]

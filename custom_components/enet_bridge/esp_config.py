"""Server-Konfiguration der Bridge (Format Version 3, src/server.h) aus anlage.json erzeugen.

Ohne Home-Assistant-Abhängigkeiten: wird auch von tools/server_cfg.py genutzt.
Kanäle kommen aus allen Aktor-Kanälen der Anlage, lokale Szenen und Server-Tasten aus anlage["esp"]
(aus den Verknüpfungen des Exports, iex.py, oder von Hand: "szenen" {Name: [{ia, handle, wert}]}, "server_tasten"
[{name, sender, ga, szene}]). Eine Taste ohne "szene" wird nur gemeldet – dann entscheidet HA.
"""
from __future__ import annotations

from typing import Any

try:
    from .texte import tr
except ImportError:  # tools/server_cfg.py lädt das Modul ohne Paket
    from texte import tr

TEIL_HEX = 400  # Hex-Zeichen je config-Befehl (Zeile < 512 Byte)


def crc16(data: bytes) -> int:
    """Wie knxrf::crc16: Polynom 0x3D65, Init 0, invertiert."""
    r = 0
    for b in data:
        r ^= b << 8
        for _ in range(8):
            r = ((r << 1) ^ 0x3D65) & 0xFFFF if r & 0x8000 else (r << 1) & 0xFFFF
    return r ^ 0xFFFF


def ia_int(s: str) -> int:
    a, b, c = (int(x) for x in s.split("."))
    return a << 12 | b << 8 | c


def build(anlage: dict[str, Any], esp: dict[str, Any] | None = None):
    """Liefert (blob, kanäle, szenennamen, tasten). esp überschreibt anlage["esp"]."""
    esp = anlage.get("esp", {}) if esp is None else esp
    srv = anlage["server"]
    channels = []
    for d in anlage["geraete"]:
        for k in d["kanaele"]:
            if k["art"] not in ("schalten", "dimmen"):  # z. B. Jalousie: in der Bridge (noch) nicht umgesetzt
                continue
            channels.append((ia_int(d["ia"]), d["sn"], k["nr"], k["offset"], k.get("obj", 2 + k["nr"]),
                             k["art"] == "dimmen", d.get("ga_basis", 0)))
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
    for ia, sn, ch, off, obj, dim, base in channels:
        b += ia.to_bytes(2, "big") + bytes.fromhex(sn) + bytes([ch, off, obj, 1 if dim else 0, base])
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


def teile(blob: bytes) -> list[str]:
    """Blob in Hex-Teile für den Befehl config zerlegen."""
    hx = blob.hex()
    return [hx[i:i + TEIL_HEX] for i in range(0, len(hx), TEIL_HEX)]

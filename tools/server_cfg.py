#!/usr/bin/env python3
"""
Builds the server configuration for the bridge and uploads it.

  python3 tools/server_cfg.py --anlage anlage.json [--szenen szenen.json] [--port ssh://root@<ha-host>]

--port: local port (/dev/cu.usbserial-0001) or ssh://<host> (bridge on the Raspberry Pi). Without --port display only.
Channels come from anlage.json (all actuator channels), scenes and server keys from the scene file
(without --szenen: from anlage["esp"], like the HA integration, which transfers the configuration itself).
A key without "szene" (or "szene": null) is only reported – the host/HA then decides.
Format: see src/server.h (version 3).
"""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "enet_bridge"))
from esp_config import build, split_parts  # noqa: E402  (shared with the HA integration)


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def main():
    if not arg("--anlage"):
        sys.exit(__doc__)
    with open(arg("--anlage"), encoding="utf-8") as f:
        installation = json.load(f)
    scenes = None
    if arg("--szenen"):
        with open(arg("--szenen"), encoding="utf-8") as f:
            scenes = json.load(f)
    blob, channels, scene_names, keys = build(installation, scenes)
    print(f"{len(channels)} Kanäle, {len(scene_names)} Szenen, {len(keys)} Server-Tasten, {len(blob)} Byte")
    for i, n in enumerate(scene_names):
        print(f"  Szene {i}: {n}")
    for sn, ga, sc, name in keys:
        print(f"  Taste {name or sn} GA {ga}: " + (f"Szene {sc} ({scene_names[sc]})" if sc >= 0 else "nur melden"))
    port = arg("--port")
    if not port:
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from bridge_link import open_link
    link = open_link(port)
    parts = split_parts(blob)
    for n, part in enumerate(parts):
        last = n == len(parts) - 1
        link.send({"cmd": "config", "id": n + 1, "part": n, "data": part, "last": last})
        r = link.wait(lambda m: m.get("re") == n + 1, 2.0)
        if not r or not r.get("ok"):
            sys.exit(f"Bridge lehnt Teil {n} ab: {r}")
    r = link.wait(lambda m: m.get("ev") == "config", 3.0)
    print("Bridge:", json.dumps(r, ensure_ascii=False))
    link.close()


if __name__ == "__main__":
    main()

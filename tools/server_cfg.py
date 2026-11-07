#!/usr/bin/env python3
"""
Erzeugt die Server-Konfiguration für die Bridge und lädt sie hoch.

  python3 tools/server_cfg.py --anlage anlage.json [--szenen szenen.json] [--port ssh://root@<ha-host>]

--port: lokaler Port (/dev/cu.usbserial-0001) oder ssh://<host> (Bridge am Raspi). Ohne --port nur Anzeige.
Kanäle kommen aus anlage.json (alle Aktor-Kanäle), Szenen und Server-Tasten aus der Szenendatei
(ohne --szenen: aus anlage["esp"], wie die HA-Integration, die die Konfiguration selbst überträgt).
Eine Taste ohne "szene" (oder "szene": null) wird nur gemeldet – dann entscheidet der Host/HA.
Format: siehe src/server.h (Version 3).
"""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "enet_bridge"))
from esp_config import build, teile  # noqa: E402  (gemeinsam mit der HA-Integration)


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def main():
    if not arg("--anlage"):
        sys.exit(__doc__)
    with open(arg("--anlage"), encoding="utf-8") as f:
        anlage = json.load(f)
    szenen = None
    if arg("--szenen"):
        with open(arg("--szenen"), encoding="utf-8") as f:
            szenen = json.load(f)
    blob, channels, scene_names, keys = build(anlage, szenen)
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
    parts = teile(blob)
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

#!/usr/bin/env python3
"""
Radio test: measure noise level and reachability of all actuators (status queries only, switches nothing).

  python3 tools/radio_test.py [--port ssh://root@<ha-host>] [--anlage anlage.json] [--runden 4]

Sequence:
  1. With ssh:// the HA integration releases the port (enet_bridge.release), and enet_bridge.reconnect at the end.
  2. Noise level: diag × 3 (RSSI min/max, share of "channel busy").
  3. Each round queries every actuator channel (poll): attempts, reply levels, corrupted telegrams.
  4. Bridge counters (stats), e.g. corrected = telegrams corrected via CRC.
Threshold for comparison: below about −72 dBm bit errors occurred in the test installation.
"""
import json, os, subprocess, sys, time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge_link import open_link  # noqa: E402


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def ha_service(port, service):
    """Pause/resume the integration on the Raspberry Pi (ssh:// only)."""
    if not port.startswith("ssh://"):
        return
    host = port[6:].partition("/")[0]
    subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                    f'curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" '
                    f'http://supervisor/core/api/services/enet_bridge/{service} >/dev/null || true'], check=False)


def main():
    port = arg("--port", "ssh://" + os.environ.get("ENET_HA_HOST", "root@homeassistant.local"))
    rounds = int(arg("--runden", "4"))
    with open(arg("--anlage", "anlage.json"), encoding="utf-8") as f:
        installation = json.load(f)
    channels_ = [(g["ia"], k["nr"], g["name"]) for g in installation["geraete"] if g.get("rolle") == "aktor"
               for k in g.get("kanaele", []) if k["art"] in ("schalten", "dimmen")]

    ha_service(port, "release")
    try:
        link = open_link(port)
        time.sleep(1.5)  # let the bridge finish booting after opening
        print("Störpegel (je ~5 s):")
        for i in range(3):
            link.send({"cmd": "diag", "id": 10 + i})
            m = link.wait(lambda m: m.get("ev") == "diag", 15)
            if m:
                print(f"  RSSI {m['rssi_min']:6.1f} … {m['rssi_max']:6.1f} dBm   Kanal belegt {100 * m['cs_anteil']:4.0f} %")

        link.write_line("v")  # rx_error events
        link.send({"cmd": "monitor", "id": 2, "on": True})
        time.sleep(0.3)
        level_, attempts, noise = defaultdict(list), defaultdict(list), defaultdict(int)
        cid = 100

        def process(m, current):
            """Reply level by sender (the telegram only arrives after the result), errors to the running query."""
            ev = m.get("ev")
            if ev == "telegram" and m.get("dir") == "rx" and len(m["data"]) >= 36:
                d = bytes.fromhex(m["data"])
                if (d[16] & 3) << 8 | d[17] == 0x2C9:
                    level_[f"{d[11] >> 4}.{d[11] & 15}.{d[12]}"].append(m.get("rssi"))
            elif ev == "rx_error" and m.get("kind") in ("manchester", "crc") and current:
                noise[current] += 1

        for _ in range(rounds):
            for ia, ch, _name in channels_:
                cid += 1
                link.send({"cmd": "poll", "id": cid, "ia": ia, "ch": ch})
                while True:
                    m = link.read_json(10)
                    if m is None:
                        attempts[(ia, ch)].append(None)
                        break
                    if m.get("ev") == "result" and m.get("id") == cid:
                        attempts[(ia, ch)].append(m.get("tries") if m.get("ok") else None)
                        break
                    process(m, (ia, ch))
        while (m := link.read_json(0.5)) is not None:
            process(m, None)

        print(f"\nAktoren ({rounds} Abfragen je Kanal):")
        print(f"  {'Kanal':<9} {'Name':<18} {'ok':>5}  {'Versuche':<16} {'Pegel dBm':<15} gestört")
        for ia, ch, name in channels_:
            v = attempts[(ia, ch)]
            ok = sum(1 for x in v if x)
            p = [x for x in level_[ia] if x is not None]  # per device
            ps = f"{min(p):.0f} … {max(p):.0f}" if p else "–"
            vs = " ".join(str(x) if x else "X" for x in v)
            print(f"  {ia + '/' + str(ch):<9} {name[:18]:<18} {ok:>2}/{len(v):<2}  {vs:<16} {ps:<15} {noise[(ia, ch)]}")

        link.send({"cmd": "stats", "id": 99})
        s = link.wait(lambda m: m.get("re") == 99, 2) or {}
        print("\nBridge:", ", ".join(f"{k} {s[k]}" for k in
                                     ("rx_ok", "corrected", "crc_error", "invalid", "overflow", "tx", "tx_busy") if k in s))
        link.write_line("v")
        link.close()
    finally:
        ha_service(port, "reconnect")


if __name__ == "__main__":
    main()

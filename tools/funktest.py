#!/usr/bin/env python3
"""
Funktest: Störpegel und Erreichbarkeit aller Aktoren messen (nur Statusabfragen, schaltet nichts).

  python3 tools/funktest.py [--port ssh://root@<ha-host>] [--anlage anlage.json] [--runden 4]

Ablauf:
  1. Bei ssh:// gibt die HA-Integration den Port frei (enet_bridge.release), am Ende wieder enet_bridge.reconnect.
  2. Störpegel: diag × 3 (RSSI min/max, Anteil „Kanal belegt“).
  3. Je Runde jeden Aktor-Kanal abfragen (poll): Versuche, Pegel der Antworten, gestörte Telegramme.
  4. Zähler der Bridge (stats), u. a. corrected = per CRC korrigierte Telegramme.
Schwelle zum Vergleich: unterhalb von etwa −72 dBm traten in der Testanlage Bitfehler auf.
"""
import json, os, subprocess, sys, time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge_link import open_link  # noqa: E402


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def ha_service(port, service):
    """Integration auf dem Raspi anhalten/fortsetzen (nur bei ssh://)."""
    if not port.startswith("ssh://"):
        return
    host = port[6:].partition("/")[0]
    subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                    f'curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" '
                    f'http://supervisor/core/api/services/enet_bridge/{service} >/dev/null || true'], check=False)


def main():
    port = arg("--port", "ssh://" + os.environ.get("ENET_HA_HOST", "root@homeassistant.local"))
    runden = int(arg("--runden", "4"))
    with open(arg("--anlage", "anlage.json"), encoding="utf-8") as f:
        anlage = json.load(f)
    kanaele = [(g["ia"], k["nr"], g["name"]) for g in anlage["geraete"] if g.get("rolle") == "aktor"
               for k in g.get("kanaele", []) if k["art"] in ("schalten", "dimmen")]

    ha_service(port, "release")
    try:
        link = open_link(port)
        time.sleep(1.5)  # Bridge nach dem Öffnen fertig starten lassen
        print("Störpegel (je ~5 s):")
        for i in range(3):
            link.send({"cmd": "diag", "id": 10 + i})
            m = link.wait(lambda m: m.get("ev") == "diag", 15)
            if m:
                print(f"  RSSI {m['rssi_min']:6.1f} … {m['rssi_max']:6.1f} dBm   Kanal belegt {100 * m['cs_anteil']:4.0f} %")

        link.write_line("v")  # rx_error-Ereignisse
        link.send({"cmd": "monitor", "id": 2, "on": True})
        time.sleep(0.3)
        pegel, versuche, stoer = defaultdict(list), defaultdict(list), defaultdict(int)
        cid = 100

        def verarbeite(m, aktuell):
            """Antwort-Pegel nach Absender (das Telegramm kommt erst nach dem result), Störungen zur laufenden Abfrage."""
            ev = m.get("ev")
            if ev == "telegram" and m.get("dir") == "rx" and len(m["data"]) >= 36:
                d = bytes.fromhex(m["data"])
                if (d[16] & 3) << 8 | d[17] == 0x2C9:
                    pegel[f"{d[11] >> 4}.{d[11] & 15}.{d[12]}"].append(m.get("rssi"))
            elif ev == "rx_error" and m.get("kind") in ("manchester", "crc") and aktuell:
                stoer[aktuell] += 1

        for _ in range(runden):
            for ia, ch, _name in kanaele:
                cid += 1
                link.send({"cmd": "poll", "id": cid, "ia": ia, "ch": ch})
                while True:
                    m = link.read_json(10)
                    if m is None:
                        versuche[(ia, ch)].append(None)
                        break
                    if m.get("ev") == "result" and m.get("id") == cid:
                        versuche[(ia, ch)].append(m.get("tries") if m.get("ok") else None)
                        break
                    verarbeite(m, (ia, ch))
        while (m := link.read_json(0.5)) is not None:
            verarbeite(m, None)

        print(f"\nAktoren ({runden} Abfragen je Kanal):")
        print(f"  {'Kanal':<9} {'Name':<18} {'ok':>5}  {'Versuche':<16} {'Pegel dBm':<15} gestört")
        for ia, ch, name in kanaele:
            v = versuche[(ia, ch)]
            ok = sum(1 for x in v if x)
            p = [x for x in pegel[ia] if x is not None]  # je Gerät
            ps = f"{min(p):.0f} … {max(p):.0f}" if p else "–"
            vs = " ".join(str(x) if x else "X" for x in v)
            print(f"  {ia + '/' + str(ch):<9} {name[:18]:<18} {ok:>2}/{len(v):<2}  {vs:<16} {ps:<15} {stoer[(ia, ch)]}")

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

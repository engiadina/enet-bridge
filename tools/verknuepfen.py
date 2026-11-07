#!/usr/bin/env python3
"""
Sender-Wippe mit einem Aktor-Kanal verknüpfen – ohne eNet Server, nach Mitschnitten
(verknuepfen.jsonl, wandsender.jsonl; docs/NOTES.md „Verknüpfen und Löschen“). --loeschen entfernt sie wieder.

  python3 tools/verknuepfen.py --port ssh://root@<ha-host> --anlage anlage.json \
      --sender 0.0.3 --wippe 4 --aktor 0.0.30 [--kanal 1] [--loeschen]

Regel für Wippe n (bestätigt an HS4 W3/W4 und WS3 W2): Tastenwert-GA 50+3n, Statusabfrage-GA 51+3n,
Parameter-Objekte 2n+1 (= Link-Objekt) und 2n+2 (= ff), Link-Objekt 0x0B+n.
Aktor-Kanal k: Link-Objekt 2+k, GOs kanalweise ab 1 (1 = Tastenwert, 19 = Statusabfrage),
Status-GA = GA-Basis·256 + Kanaloffset + 22 (bestätigt an SA2M K1/K2).
Nur bei ausgestecktem eNet Server verwenden!
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enet  # noqa: E402
from einlernen import Bridge, apci, arg  # noqa: E402

SRV_IA = 0x7FFF


def main():
    port = arg("--port")
    if not port or not arg("--sender") or not arg("--aktor"):
        sys.exit(__doc__)
    with open(arg("--anlage", "anlage.json"), encoding="utf-8") as f:
        anlage = json.load(f)
    dom, srv_sn = anlage["server"]["domain"], anlage["server"]["sn"]
    by_ia = {d["ia"]: d for d in anlage["geraete"]}
    sender, aktor = by_ia[arg("--sender")], by_ia[arg("--aktor")]
    s_ia, a_ia = enet.ia_int(sender["ia"]), enet.ia_int(aktor["ia"])
    s_sn, a_sn = bytes.fromhex(sender["sn"]), bytes.fromhex(aktor["sn"])
    k = int(arg("--kanal", "1"))
    kanal = aktor["kanaele"][k - 1]
    aktor_obj = 2 + k
    status_ga = aktor.get("ga_basis", 0) * 256 + kanal["offset"] + 22
    op = b"\x02\x00" if "--loeschen" in sys.argv else b"\x00\x00"
    n = int(arg("--wippe", "4"))
    WIPPE_GA = 50 + 3 * n             # Tastenwert-GA, Statusabfrage = WIPPE_GA + 1
    SENDER_LINK_OBJ = 0x0B + n        # Link-Objekt des Senders für die Status-LED dieser Wippe
    SENDER_PARAMS = [(2 * n + 1, 0x4F, 1, bytes([SENDER_LINK_OBJ])), (2 * n + 2, 0x4F, 1, b"\xff")]
    b = Bridge(port)

    def to(ia, ap, data):
        return enet.build_frame(dom, True, False, SRV_IA, ia, ap, data)

    def answer_from(ia, ap):
        return lambda d: apci(d) == ap and (d[11] << 8 | d[12]) == ia

    def prop_read(ia, obj, pid):
        r = b.request(to(ia, 0x3D5, bytes([obj, pid, 0x10, 0x01])), answer_from(ia, 0x3D6))
        return r[22:] if r is not None else None

    def prop_write(ia, obj, pid, start, value):
        r = b.request(to(ia, 0x3D7, bytes([obj, pid, 0x10 | start >> 8, start & 255]) + value),
                      answer_from(ia, 0x3D6))
        if r is None or r[22:] != value:
            sys.exit(f"PropWrite {obj:#x}/{pid:#x} an {ia:#06x} fehlgeschlagen")

    def link(ia, obj, sn, ga, go):
        r = b.request(to(ia, 0x2C7, bytes([obj, 0x3F]) + op + sn + ga.to_bytes(2, "big") + go.to_bytes(2, "big")),
                      answer_from(ia, 0x2C9))
        ok = r is not None and len(r) >= 21 and r[20] == 0
        print(f"  Link {'löschen' if op[0] else 'anlegen'} {sn.hex()} GA {ga} -> GO {go}: {'OK' if ok else 'FEHLER'}",
              flush=True)
        if not ok:
            sys.exit("Abbruch")

    def program(ia, ts_obj, steps):
        """Rahmen wie der Server: Ladezustand 02, Vorbereitung, Schritte, Abschluss, Ladezustand 01."""
        prop_write(ia, 0x01, 0x06, 1, b"\x02" + bytes(9))
        prop_write(ia, ts_obj, 0x39, 1, bytes.fromhex("000000000003"))
        steps()
        prop_write(ia, (steps.link_obj), 0xD2, 1, b"\x04")
        t = time.localtime()
        prop_write(ia, ts_obj, 0x39, 1, bytes([t.tm_year - 2000, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec]))
        for _ in range(3):
            b.send(to(ia, 0x3E4, bytes.fromhex("00123c00")))
            time.sleep(0.06)
        prop_write(ia, 0x01, 0x06, 1, b"\x01" + bytes(9))
        for _ in range(5):
            time.sleep(0.5)
            if prop_read(ia, 0x01, 0x06) == b"\x01":
                return
        print("  Ladezustand nicht bestätigt (weiter)", flush=True)

    # 1. Sender wecken
    wake = enet.build_frame(srv_sn, False, True, SRV_IA, 0, 0x3E4,
                            bytes.fromhex("c35148") + s_sn + bytes.fromhex("0200000000"))
    print(f"Handsender wecken – jetzt Taste 7 oder 8 drücken …", flush=True)
    t_end = time.time() + 60
    awake = None
    while time.time() < t_end and awake is None:
        b.send(wake)
        awake = b.wait(lambda d: apci(d) == 0x3DB and d[4:10] == s_sn, 0.1)
    if awake is None:
        sys.exit("Sender nicht wach geworden")
    print("Sender wach, Modus:", (prop_read(s_ia, 0x10, 0x48) or b"").hex(), flush=True)

    # 2. Aktor: Tastenwert (GA n) -> GO 1, Statusabfrage (GA n+1) -> GO 19
    print(f"Aktor {aktor['ia']} Kanal {k} programmieren …", flush=True)

    def aktor_steps():
        link(a_ia, aktor_obj, s_sn, WIPPE_GA, 1)
        link(a_ia, aktor_obj, s_sn, WIPPE_GA + 1, 19)
    aktor_steps.link_obj = aktor_obj
    program(a_ia, 0x04, aktor_steps)

    # 3. Sender: Parameter, Status-LED hört auf die Statusmeldung des Aktors
    print(f"Sender {sender['ia']} programmieren …", flush=True)

    def sender_steps():
        for obj, pid, start, val in SENDER_PARAMS:
            prop_write(s_ia, obj, pid, start, val)
        link(s_ia, SENDER_LINK_OBJ, a_sn, status_ga, 1)
    sender_steps.link_obj = SENDER_LINK_OBJ
    program(s_ia, 0x10, sender_steps)
    prop_write(s_ia, 0x10, 0x48, 1, b"\x03")  # Sender entlassen
    print(f"Fertig – Wippe {n} " + ("gelöst" if op[0] else f"mit {aktor['name']} verknüpft (Status-GA {status_ga})"))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Einlernen eines eNet-Aktors ohne eNet Server (über die Bridge, nach Mitschnitt des Servers).

  python3 tools/einlernen.py --port ssh://root@<ha-host> --anlage anlage.json --ia 0.0.30 [--name "Mini-Dimmer"]

Ablauf (docs/NOTES.md „Einlernen“):
  1. Suche: DoA_Read + DoA_SelRead an GA 0 (unter Server-SN), alle 3 s – Gerät im Lernmodus antwortet DoA_Resp
  2. Auswahl: NetParamWrite Objtyp 50001 PID 71 = SN (Gerät bestätigt)
  3. Domäne: DoA_SN_Write SN + Domäne, Kontrolle DoA_SN_Read
  4. Adresse: IA_SN_Write SN + IA (unter Domäne), Kontrolle IA_SN_Read
  5. Kontrolle: Link-Tabelle GO 1 lesen (GA-Basis), Produktnummer lesen
Nur bei ausgestecktem eNet Server verwenden!
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enet  # noqa: E402

SRV_IA = 0x7FFF


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


class Bridge:
    """Telegramme senden/empfangen über die Bridge (Protokoll 1: send + monitor)."""

    def __init__(self, port):
        from bridge_link import open_link
        self.link = open_link(port)
        self.link.send({"cmd": "monitor", "on": True})

    def send(self, frame):
        self.link.send({"cmd": "send", "frame": frame.hex()})

    def wait(self, match, timeout):
        """Wartet auf ein empfangenes Telegramm, für das match(bytes) True liefert."""
        t = time.time()
        while time.time() - t < timeout:
            m = self.link.read_json(max(0.0, timeout - (time.time() - t)))
            if not m or m.get("ev") != "telegram" or m.get("dir") != "rx":
                continue
            d = bytes.fromhex(m["data"])
            if len(d) >= 18 and match(d):
                return d
        return None

    def request(self, frame, match, timeout=0.5, tries=4):
        for _ in range(tries):
            self.send(frame)
            r = self.wait(match, timeout)
            if r is not None:
                return r
        return None


def apci(d):
    return (d[16] & 3) << 8 | d[17]


def main():
    port, ia_s = arg("--port"), arg("--ia")
    if not port or not ia_s:
        sys.exit(__doc__)
    anlage_path = arg("--anlage", "anlage.json")
    with open(anlage_path, encoding="utf-8") as f:
        anlage = json.load(f)
    srv_sn, dom = anlage["server"]["sn"], anlage["server"]["domain"]
    ia = enet.ia_int(ia_s)
    if any(enet.ia_int(d["ia"]) == ia for d in anlage["geraete"]):
        sys.exit(f"{ia_s} ist in {anlage_path} schon vergeben")
    b = Bridge(port)

    def srv_group(ap, data):  # unter Server-SN an GA 0
        return enet.build_frame(srv_sn, False, True, SRV_IA, 0, ap, data)

    def dom_group(ap, data):  # unter Domäne an GA 0
        return enet.build_frame(dom, True, True, SRV_IA, 0, ap, data)

    # 1. Suche
    print("Suche läuft – Gerät jetzt in den Lernmodus versetzen (Taste am Gerät drücken) …", flush=True)
    sn = None
    t_end = time.time() + 120
    while time.time() < t_end and not sn:
        b.send(srv_group(0x3E1, b""))
        time.sleep(0.5)
        b.send(srv_group(0x3E3, bytes.fromhex("02000000000000ffffffffffff00")))
        r = b.wait(lambda d: apci(d) == 0x3E2 and not d[15] & 1, 2.5)
        if r is not None:
            sn = r[4:10].hex()
            print(f"Gefunden: SN {sn}, Domäne {r[18:24].hex()}", flush=True)
    if not sn:
        sys.exit("Kein Gerät im Lernmodus gefunden")
    snb = bytes.fromhex(sn)

    # Produktnummer (Obj 0 PID 0xD2) per SN lesen – nur zur Information
    r = b.request(srv_group(0x3DA, bytes.fromhex("c35143") + snb + bytes.fromhex("0000d20001")),
                  lambda d: apci(d) == 0x3DB and d[4:10] == snb)
    if r is not None:
        print(f"Produktnummer: {r[21:].hex()}", flush=True)

    # 2. Auswahl
    r = b.request(srv_group(0x3E4, bytes.fromhex("c35147") + snb),
                  lambda d: apci(d) == 0x3DB and d[18:21] == bytes.fromhex("c35147"))
    print("Auswahl (PID 71):", "bestätigt" if r is not None else "keine Antwort (weiter)", flush=True)

    # 3. Domäne setzen und prüfen
    b.send(srv_group(0x3EE, snb + bytes.fromhex(dom)))
    time.sleep(0.15)
    r = b.request(srv_group(0x3EC, snb), lambda d: apci(d) == 0x3ED and d[18:24] == snb)
    if r is None or r[24:30].hex() != dom:
        sys.exit(f"Domäne nicht gesetzt: {r[24:30].hex() if r is not None else 'keine Antwort'}")
    print(f"Domäne {dom} gesetzt", flush=True)

    # 4. Adresse setzen und prüfen (ab jetzt unter Domäne)
    b.send(dom_group(0x3DE, snb + ia.to_bytes(2, "big") + b"\0\0\0\0"))
    time.sleep(0.15)
    r = b.request(dom_group(0x3DC, snb), lambda d: apci(d) == 0x3DD and d[18:24] == snb)
    if r is None or (r[11] << 8 | r[12]) != ia:
        sys.exit("Adresse nicht bestätigt")
    print(f"Adresse {ia_s} gesetzt", flush=True)

    # 5. Kontrolle: Link-Tabelle GO 1 (GA-Basis)
    def fp_read(data):
        return b.request(enet.build_frame(dom, True, False, SRV_IA, ia, 0x2C8, data),
                         lambda d: apci(d) == 0x2C9 and (d[11] << 8 | d[12]) == ia)
    r = fp_read(bytes([3, 0x3F, 0, 0, 1, 0]))
    base = r[28] if r is not None and len(r) >= 32 and r[20] == 0 else None
    print(f"GA-Basis: {base:#04x} (Status-GA Kanal 1 = {base * 256 + 22})" if base is not None
          else "GA-Basis nicht lesbar", flush=True)

    # in anlage.json eintragen (Typ vorerst wie Mini-Dimmer; Kanäle aus iex.DEVICE_TYPES)
    import iex
    typ = arg("--typ", "DVT_DA1M")
    desc, role, chans = iex.DEVICE_TYPES.get(typ, ("unbekannt", "aktor", []))
    dev = {"name": arg("--name", f"Neu {ia_s}"), "ia": ia_s, "sn": sn, "typ": typ, "beschreibung": desc,
           "rolle": role, "kanaele": [{"nr": i + 1, "art": a, "offset": o} for i, (a, o) in enumerate(chans)],
           "ga_basis": base or 0, "eingelernt": "Bridge " + time.strftime("%Y-%m-%d %H:%M")}
    anlage["geraete"].append(dev)
    with open(anlage_path, "w", encoding="utf-8") as f:
        json.dump(anlage, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"In {anlage_path} eingetragen: {dev['name']} ({typ})")


if __name__ == "__main__":
    main()

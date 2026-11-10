#!/usr/bin/env python3
"""
Pairing an eNet actuator without an eNet Server (via the bridge, based on a capture of the server).

  python3 tools/pair.py --port ssh://root@<ha-host> --anlage anlage.json --ia 0.0.30 [--name "Mini-Dimmer"]

Sequence (docs/NOTES.md "Einlernen"):
  1. Search: DoA_Read + DoA_SelRead to GA 0 (under server SN), every 3 s – a device in learn mode answers DoA_Resp
  2. Select: NetParamWrite object type 50001 PID 71 = SN (device confirms)
  3. Domain: DoA_SN_Write SN + domain, check with DoA_SN_Read
  4. Address: IA_SN_Write SN + IA (under domain), check with IA_SN_Read
  5. Check: read link table GO 1 (GA base), read product number
Only use with the eNet Server unplugged!
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enet  # noqa: E402

SRV_IA = 0x7FFF


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


class Bridge:
    """Send/receive telegrams via the bridge (protocol 1: send + monitor)."""

    def __init__(self, port):
        from bridge_link import open_link
        self.link = open_link(port)
        self.link.send({"cmd": "monitor", "on": True})

    def send(self, frame):
        self.link.send({"cmd": "send", "frame": frame.hex()})

    def wait(self, match, timeout):
        """Wait for a received telegram for which match(bytes) returns True."""
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
    installation_path = arg("--anlage", "anlage.json")
    with open(installation_path, encoding="utf-8") as f:
        installation = json.load(f)
    srv_sn, dom = installation["server"]["sn"], installation["server"]["domain"]
    ia = enet.ia_int(ia_s)
    if any(enet.ia_int(d["ia"]) == ia for d in installation["geraete"]):
        sys.exit(f"{ia_s} ist in {installation_path} schon vergeben")
    b = Bridge(port)

    def srv_group(ap, data):  # under server SN to GA 0
        return enet.build_frame(srv_sn, False, True, SRV_IA, 0, ap, data)

    def dom_group(ap, data):  # under domain to GA 0
        return enet.build_frame(dom, True, True, SRV_IA, 0, ap, data)

    # 1. Search
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

    # read product number (obj 0 PID 0xD2) via SN – for information only
    r = b.request(srv_group(0x3DA, bytes.fromhex("c35143") + snb + bytes.fromhex("0000d20001")),
                  lambda d: apci(d) == 0x3DB and d[4:10] == snb)
    if r is not None:
        print(f"Produktnummer: {r[21:].hex()}", flush=True)

    # 2. Select
    r = b.request(srv_group(0x3E4, bytes.fromhex("c35147") + snb),
                  lambda d: apci(d) == 0x3DB and d[18:21] == bytes.fromhex("c35147"))
    print("Auswahl (PID 71):", "bestätigt" if r is not None else "keine Antwort (weiter)", flush=True)

    # 3. Set and verify domain
    b.send(srv_group(0x3EE, snb + bytes.fromhex(dom)))
    time.sleep(0.15)
    r = b.request(srv_group(0x3EC, snb), lambda d: apci(d) == 0x3ED and d[18:24] == snb)
    if r is None or r[24:30].hex() != dom:
        sys.exit(f"Domäne nicht gesetzt: {r[24:30].hex() if r is not None else 'keine Antwort'}")
    print(f"Domäne {dom} gesetzt", flush=True)

    # 4. Set and verify address (from now on under domain)
    b.send(dom_group(0x3DE, snb + ia.to_bytes(2, "big") + b"\0\0\0\0"))
    time.sleep(0.15)
    r = b.request(dom_group(0x3DC, snb), lambda d: apci(d) == 0x3DD and d[18:24] == snb)
    if r is None or (r[11] << 8 | r[12]) != ia:
        sys.exit("Adresse nicht bestätigt")
    print(f"Adresse {ia_s} gesetzt", flush=True)

    # 5. Check: link table GO 1 (GA base)
    def fp_read(data):
        return b.request(enet.build_frame(dom, True, False, SRV_IA, ia, 0x2C8, data),
                         lambda d: apci(d) == 0x2C9 and (d[11] << 8 | d[12]) == ia)
    r = fp_read(bytes([3, 0x3F, 0, 0, 1, 0]))
    base = r[28] if r is not None and len(r) >= 32 and r[20] == 0 else None
    print(f"GA-Basis: {base:#04x} (Status-GA Kanal 1 = {base * 256 + 22})" if base is not None
          else "GA-Basis nicht lesbar", flush=True)

    # add to anlage.json (type defaults to Mini-Dimmer for now; channels from iex.DEVICE_TYPES)
    import iex
    typ = arg("--typ", "DVT_DA1M")
    desc, role, chans = iex.DEVICE_TYPES.get(typ, ("unbekannt", "aktor", []))
    dev = {"name": arg("--name", f"Neu {ia_s}"), "ia": ia_s, "sn": sn, "typ": typ, "beschreibung": desc,
           "rolle": role, "kanaele": [{"nr": i + 1, "art": a, "offset": o} for i, (a, o) in enumerate(chans)],
           "ga_basis": base or 0, "eingelernt": "Bridge " + time.strftime("%Y-%m-%d %H:%M")}
    installation["geraete"].append(dev)
    with open(installation_path, "w", encoding="utf-8") as f:
        json.dump(installation, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"In {installation_path} eingetragen: {dev['name']} ({typ})")


if __name__ == "__main__":
    main()

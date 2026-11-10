#!/usr/bin/env python3
"""
eNet test program: counterpart to the bridge (phase 1, instead of Home Assistant).

  python3 tools/enet.py --port /dev/cu.usbserial-0001 --anlage anlage.json [--log bridge.jsonl]
  python3 tools/enet.py --port /dev/cu.usbserial-0001 --iex "Anlage.iex"

Without --port it reads rtl_433 JSON from stdin (SDR) and only displays.
The installation comes from tools/iex.py (anlage.json, can be extended by hand) or directly from an .iex.

Commands (the 'hilfe' command prints from here on) – Befehle:
  liste                         devices and channels
  ein <device> [channel]        switch on (GO 2 = 1)
  aus <device> [channel]        switch off (GO 2 = 0)
  dimm <device> <percent> [channel] set brightness (GO 4, 0–255)
  status <device> [channel]     query status (GO 20, for dimmers also GO 21)
  roh <bridge command>          send directly to the bridge, e.g. "roh s" or "roh cmd 0.0.4 2 01"
  broadcast an|aus              show server broadcasts (default: aus)
  zustand                       known state of all channels (from status messages and queries)
  szene <name>                  run a scene from the server configuration, e.g. "szene alles an"
  server an|aus                 switch server emulation (answering keys) on/off
  ende
<device> = address (0.0.4) or part of the name (stylos).

Server emulation: --server szenen.json
  Answers SumstatusGet addressed to the server SN (server keys, CC 0x08) with SumstatusInfo and runs the
  assigned scene. Starts OFF and switches itself off as soon as a telegram from the real server is
  received. As long as the real eNet Server is running: do not answer any keys!
"""
import datetime, json, os, queue, sys, threading, time, unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iex  # noqa: E402

GO_SWITCH, GO_VALUE, GO_STATUS, GO_STATUS_VALUE, GO_SUMSTATUS_INFO = 2, 4, 20, 21, 22
STATUS_POLL_DELAY = 8.0  # like the server: query 8 s after a command if no status message arrived

APCI = {
    0x000: "GroupRead", 0x040: "GroupResponse", 0x080: "GroupWrite",
    0x2C7: "FuncPropCmd", 0x2C8: "FuncPropStateRead", 0x2C9: "FuncPropStateResp",
    0x3D5: "PropRead", 0x3D6: "PropResp", 0x3D7: "PropWrite",
    0x3DA: "NetParamRead", 0x3DB: "NetParamResp", 0x3E4: "NetParamWrite",
    0x3DC: "IA_SN_Read", 0x3DD: "IA_SN_Resp", 0x3DE: "IA_SN_Write",
    0x3E1: "DoA_Read", 0x3E2: "DoA_Resp", 0x3E3: "DoA_SelRead",
    0x3EC: "DoA_SN_Read", 0x3ED: "DoA_SN_Resp", 0x3EE: "DoA_SN_Write",
}


def now():
    return f"{datetime.datetime.now():%H:%M:%S.%f}"[:12]


def fold(s):
    s = s.replace("ß", "ss").lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def ia_int(s):
    a, b, c = (int(x) for x in s.split("."))
    return a << 12 | b << 8 | c


def ga_str(a):
    return f"GA {a >> 11}/{(a >> 8) & 7}/{a & 255}"


class Installation:
    def __init__(self, data):
        self.server = data["server"]
        self.devices = data["geraete"]
        self.by_sn = {d["sn"]: d for d in self.devices}
        self.by_ia = {ia_int(d["ia"]): d for d in self.devices}

    def ia_name(self, a):
        if a == ia_int(self.server["ia"]):
            return "Server"
        d = self.by_ia.get(a)
        return d["name"] if d else iex.ia_str(a)

    def sn_name(self, sn):
        if sn == self.server["sn"]:
            return "Server"
        d = self.by_sn.get(sn)
        return d["name"] if d else f"SN {sn}"

    def find(self, key):
        if key.count(".") == 2:
            d = self.by_ia.get(ia_int(key))
            return [d] if d else []
        k = fold(key)
        return [d for d in self.devices if k in fold(d["name"])]


def build_frame(addr_hex, doa, group, src, dst, apci, data, hops=6, lfn=0):
    """Telegram without CRCs (like knxrf::buildTelegram). The bridge sets a new LFN when sending."""
    b = bytearray([0, 0x44, 0xFF, 0x02]) + bytes.fromhex(addr_hex)
    b += bytes([0x00, src >> 8, src & 255, dst >> 8, dst & 255,
                (0x80 if group else 0) | (hops & 7) << 4 | (lfn & 7) << 1 | (1 if doa else 0),
                (apci >> 8) & 3, apci & 255]) + bytes(data)
    b[0] = len(b) - 1
    return bytes(b)


def sumstatus_info(server_sn, ga, status, seq, lfn=0):
    """Server reply to SumstatusGet: under the server SN to the reply GA, seq mirrored."""
    return build_frame(server_sn, False, True, 0x7FFF, ga, 0x080, [status, seq >> 8, seq & 255], lfn=lfn)


def decode(b, installation):
    """Return a dict with fields and a plain-text line, or None."""
    if len(b) < 18 or b[1] != 0x44 or b[2] != 0xFF:
        return None
    t = {
        "addr": b[4:10].hex(), "src": b[11] << 8 | b[12], "dst": b[13] << 8 | b[14],
        "group": bool(b[15] & 0x80), "lfn": (b[15] >> 1) & 7, "doa": bool(b[15] & 1),
        "apci": (b[16] & 3) << 8 | b[17], "data": b[18:],
    }
    base = t["apci"] & 0x3C0
    t["base"] = base if base in (0x000, 0x040, 0x080) else t["apci"]
    t["mine"] = t["addr"] in (installation.server["domain"], installation.server["sn"]) or t["addr"] in installation.by_sn
    d = t["data"]

    if t["doa"]:
        who = installation.ia_name(t["src"])
    else:
        who = installation.sn_name(t["addr"])
    dst = ga_str(t["dst"]) if t["group"] else installation.ia_name(t["dst"])
    detail = d.hex()
    if t["base"] == 0x080 and len(d) == 14:
        cmd = d[9] << 8 | d[10]
        if d[13] == 0x08:  # server key: bit 14 = pressed, bits 11-8 = key code (transmitter obj. PID 0xE2)
            key_obj = f"Taste Code {(cmd >> 8) & 0x0F:X} {'gedrückt' if cmd & 0x4000 else 'losgelassen'}"
        else:  # directly linked: 0x40xx up, 0x80xx down, lower 14 bits hold time in 10 ms
            direction = {0x4000: "links", 0x8000: "rechts"}.get(cmd & 0xC000, f"0x{cmd:04x}")
            hold = (cmd & 0x3FFF) * 10
            key_obj = f"Taste {direction}" + (f" gehalten {hold} ms" if hold else "")
        detail = (key_obj +
                  f"  (SumstatusGet an {installation.sn_name(d[1:7].hex())} {ga_str(d[7] << 8 | d[8])}, "
                  f"seq 0x{d[11]:02x}{d[12]:02x}, CC 0x{d[13]:02x})")
    elif t["base"] == 0x080 and len(d) == 3:
        detail = f"SumstatusInfo {'EIN' if d[0] >> 7 else 'AUS'} {d[0] & 0x7F}%  seq 0x{d[1]:02x}{d[2]:02x}"
    elif t["base"] == 0x080 and len(d) == 2:
        detail = f"Tastenwert 0x{d.hex()}"
    elif t["apci"] in (0x2C7, 0x2C8) and len(d) >= 4:
        detail = f"Handle {d[2] << 8 | d[3]}" + (f" = {d[4:].hex()}" if t["apci"] == 0x2C7 else " ?")
    elif t["apci"] in (0x3D5, 0x3D6, 0x3D7) and len(d) >= 4:
        detail = f"Obj {d[0]} PID {d[1]} Anz {d[2] >> 4} Start {(d[2] & 15) << 8 | d[3]}" + (
            f" = {d[4:].hex()}" if len(d) > 4 else "")
    elif t["apci"] in (0x3DA, 0x3DB, 0x3E4) and len(d) >= 3:
        detail = f"Objtyp {d[0] << 8 | d[1]} PID {d[2]} {d[3:].hex()}"
    elif t["apci"] in (0x3DC, 0x3DD, 0x3DE, 0x3EC, 0x3ED, 0x3EE) and len(d) >= 6:
        detail = f"SN {installation.sn_name(d[:6].hex())} {d[6:].hex()}"
    elif t["apci"] == 0x2C9 and len(d) >= 3:
        detail = ("OK" if d[2] == 0 else f"Fehler 0x{d[2]:02x}") + (f" Wert {d[3:].hex()}" if len(d) > 3 else "")
    name = APCI.get(t["base"], f"APCI 0x{t['apci']:03x}")
    t["line"] = f"{who:>15} -> {dst:<15} {name:<17} {detail}"
    return t


class Tester:
    def __init__(self, installation, ser, log, show_bc, show_all, server_cfg=None):
        self.installation, self.ser, self.log = installation, ser, log
        self.show_bc, self.show_all = show_bc, show_all
        self.pending = {}  # IA -> (description, bridge ms of sending)
        self.last_tx = None
        # server emulation
        self.server_cfg = server_cfg
        self.server_on = False
        self.last_press = {}  # (transmitter SN, GA) -> seq of the last executed key press
        self.acks = {}  # IA -> [threading.Event, reply data], set on FuncPropStateResp
        self.keys = {(k["sender"], k["ga"]): k for k in server_cfg["server_tasten"]} if server_cfg else {}
        # state and status polling
        self.state = {}  # (IA, offset) -> {"ein": bool, "prozent": int, "quelle": str, "zeit": str}
        self.cmd_sent = {}  # IA -> handle of the last command sent (until acknowledged)
        self.poll_timers = {}  # (IA, offset) -> threading.Timer
        self.work_q = queue.Queue()  # scenes and status queries one after another over the radio link
        threading.Thread(target=self.worker, daemon=True).start()

    # --- Receiving ---------------------------------------------------------
    def handle_line(self, raw):
        if self.log:
            self.log.write(raw.rstrip("\n") + "\n")
            self.log.flush()
        try:
            j = json.loads(raw)
        except ValueError:
            return
        ev = j.get("event")
        if ev == "tx":
            t = decode(bytes.fromhex(j["data"]), self.installation)
            res = j.get("result")
            if res != "ok":
                print(f"{now()} TX FEHLGESCHLAGEN ({res}): {t['line'] if t else j['data']}", flush=True)
                if t:
                    self.pending.pop(t["dst"], None)
                return
            if t and t["dst"] in self.pending:
                self.pending[t["dst"]] = (self.pending[t["dst"]][0], j.get("ms"))
            print(f"{now()} TX  {t['line'] if t else j['data']}", flush=True)
            return
        if ev:
            if ev == "error":
                self.pending.clear()  # command was not sent
            print(f"{now()} BRIDGE {json.dumps(j, ensure_ascii=False)}", flush=True)
            return
        if "data" not in j:
            return
        try:
            t = decode(bytes.fromhex(j["data"]), self.installation)
        except ValueError:
            return
        if not t or (not t["mine"] and not self.show_all):
            return
        if t["apci"] == 0x3E4 and not self.show_bc:
            return
        if self.server_cfg:
            self.server_rx(t, j.get("ms"))  # first: answer keys as fast as possible
        extra = f"  [{j['rssi']} dBm]" if "rssi" in j else ""
        ts = str(j["time"])[11:23] if "time" in j else now()  # time from rtl_433, otherwise now
        print(f"{ts} RX  {t['line']}{extra}", flush=True)
        if self.ser:
            self.track(t)
        self.check_answer(t, j.get("ms"))

    # --- State and status polling -----------------------------------------
    def channel_for(self, ia, handle):
        """Actuator channel for a GO handle (offset ≤ handle < offset + 26)."""
        d = self.installation.by_ia.get(ia)
        for ch in (d["kanaele"] if d else []):
            if 0 <= handle - ch["offset"] < 26:
                return d, ch
        return None, None

    def set_state(self, ia, ch, source_, on=None, percent_=None):
        key = (ia, ch["offset"])
        s = self.state.setdefault(key, {})
        old = dict(s)
        if on is not None:
            s["ein"] = on
        if percent_ is not None:
            s["prozent"] = percent_
        s["quelle"], s["zeit"] = source_, now()
        if {k: s.get(k) for k in ("ein", "prozent")} != {k: old.get(k) for k in ("ein", "prozent")}:
            d = self.installation.by_ia[ia]
            value = "EIN" if s.get("ein") else "AUS"
            if ch["art"] == "dimmen" and s.get("prozent") is not None:
                value += f" {s['prozent']} %"
            print(f"    Zustand {d['name']} K{ch['nr']}: {value}  ({source_})", flush=True)

    def cancel_poll(self, ia, offset):
        tm = self.poll_timers.pop((ia, offset), None)
        if tm:
            tm.cancel()

    def track(self, t):
        d = t["data"]
        if t["apci"] == 0x2C9 and len(d) >= 3:
            if t["src"] in self.acks:
                self.acks[t["src"]][1] = d
                self.acks[t["src"]][0].set()
            handle = self.cmd_sent.pop(t["src"], None)
            if handle is not None and d[2] == 0:  # command acknowledged -> schedule status query
                dev, ch = self.channel_for(t["src"], handle)
                if ch:
                    key = (t["src"], ch["offset"])
                    self.cancel_poll(*key)
                    tm = threading.Timer(STATUS_POLL_DELAY, lambda: self.work_q.put(lambda: self.poll(*key)))
                    tm.daemon = True
                    self.poll_timers[key] = tm
                    tm.start()
        elif t["group"] and t["base"] == 0x080 and len(d) == 3 and not t["doa"]:
            # spontaneous SumstatusInfo from an actuator: under its own SN to GA = base * 256 + offset + 22
            dev = self.installation.by_sn.get(t["addr"])
            for ch in (dev["kanaele"] if dev else []):
                if t["dst"] == dev.get("ga_basis", 0) * 256 + ch["offset"] + GO_SUMSTATUS_INFO:
                    ia = ia_int(dev["ia"])
                    self.cancel_poll(ia, ch["offset"])
                    self.set_state(ia, ch, "Statusmeldung", on=bool(d[0] & 0x80), percent_=d[0] & 0x7F)

    def request(self, line, ia, timeout=0.4, tries=3):
        """Send a command to the bridge, wait for the actuator's FuncPropStateResp. Returns the reply data or None."""
        slot = self.acks[ia] = [threading.Event(), None]
        try:
            for _ in range(tries):
                slot[0].clear()
                self.bridge(line)
                if slot[0].wait(timeout):
                    return slot[1]
            return None
        finally:
            self.acks.pop(ia, None)

    def poll(self, ia, offset):
        self.poll_timers.pop((ia, offset), None)
        dev, ch = self.channel_for(ia, offset)
        if not ch:
            return
        r = self.request(f"get {dev['ia']} {offset + GO_STATUS}", ia)
        if r is None or len(r) < 4 or r[2] != 0:
            print(f"    Statusabfrage {dev['name']} K{ch['nr']}: keine Antwort", flush=True)
            return
        percent_ = None
        if ch["art"] == "dimmen":
            r2 = self.request(f"get {dev['ia']} {offset + GO_STATUS_VALUE}", ia)
            if r2 is not None and len(r2) >= 4 and r2[2] == 0:
                percent_ = r2[3] * 100 // 255
        self.set_state(ia, ch, "Abfrage nach 8 s", on=bool(r[3]), percent_=percent_)

    # --- Server emulation --------------------------------------------------
    def server_rx(self, t, ms):
        srv = self.installation.server
        # Telegrams from the real server? The bridge does not receive its own transmissions.
        if t["src"] == ia_int(srv["ia"]) or t["addr"] == srv["sn"]:
            if self.server_on:
                self.server_on = False
                print("!!! Echter eNet Server aktiv – Server-Emulation AUS", flush=True)
            return
        d = t["data"]
        if not (t["group"] and t["base"] == 0x080 and len(d) == 14 and d[1:7].hex() == srv["sn"]):
            return
        seq, cmd = d[11] << 8 | d[12], d[9] << 8 | d[10]
        key = (t["addr"], t["dst"])
        cfg = self.keys.get(key)
        if not self.server_on:
            print(f"    (Server-Taste {cfg['name'] if cfg else key} – Emulation aus, keine Antwort)", flush=True)
            return
        reply_ga = d[7] << 8 | d[8]
        self.bridge("send " + sumstatus_info(srv["sn"], reply_ga, 0xFF, seq).hex())
        press = bool(cmd & 0x4000)  # 0x40xx/0x4Fxx = press, 0x00xx/0x0Fxx = release
        if cfg and press and self.last_press.get(key) != seq:
            self.last_press[key] = seq
            print(f"    => {cfg['name']}: Szene „{cfg['szene']}“", flush=True)
            self.work_q.put(lambda name=cfg["szene"]: self.run_scene(name))
        elif not cfg:
            print(f"    => unbekannte Server-Taste {key} – nur beantwortet", flush=True)

    def worker(self):
        while True:
            self.work_q.get()()

    def run_scene(self, name):
        actions = self.server_cfg["szenen"].get(name)
        if actions is None:
            print(f"Szene „{name}“ unbekannt", flush=True)
            return
        failed = []
        for a in actions:  # like the server: continue only after the actuator has acknowledged
            ia = ia_int(a["ia"])
            self.cmd_sent[ia] = a["handle"]
            if self.request(f"cmd {a['ia']} {a['handle']} {a['wert']}", ia) is None:
                failed.append(a["ia"])
        print(f"    => Szene „{name}“ fertig" + (f", ohne Quittung: {', '.join(failed)}" if failed else ""), flush=True)

    def check_answer(self, t, ms):
        if t["apci"] != 0x2C9 or t["src"] not in self.pending:
            return
        what, tx_ms = self.pending.pop(t["src"])
        d = t["data"]
        dt = f" nach {ms - tx_ms} ms" if ms is not None and tx_ms is not None else ""
        if len(d) < 3 or d[2] != 0:
            print(f"    => {what}: FEHLER rc=0x{d[2]:02x}{dt}" if len(d) >= 3 else f"    => {what}: ?", flush=True)
            return
        val = ""
        if len(d) > 3:
            v = d[3]
            val = f": Wert {v} ({round(v * 100 / 255)} %)" if what.endswith("Wert") else f": {'EIN' if v else 'AUS'}"
        print(f"    => {what}: OK{val}{dt}", flush=True)

    # --- Sending -----------------------------------------------------------
    def bridge(self, cmd):
        if not self.ser:
            print("(kein --port, nur Anzeige)")
            return
        self.ser.write(cmd.encode() + b"\n")

    def send_fp(self, dev, handle, value_hex, what):
        ia = ia_int(dev["ia"])
        self.pending[ia] = (what, None)
        if value_hex is None:
            self.bridge(f"get {dev['ia']} {handle}")
        else:
            self.cmd_sent[ia] = handle
            self.bridge(f"cmd {dev['ia']} {handle} {value_hex}")

    def pick(self, args, need_dimmer=False):
        """args = [device..., channel?] -> (device, channel) or None"""
        if not args:
            print("Gerät fehlt")
            return None
        nr = None
        if len(args) > 1 and args[-1].isdigit():
            nr = int(args.pop())
        found = self.installation.find(" ".join(args))
        if len(found) != 1:
            print("nicht gefunden" if not found else "mehrdeutig: " + ", ".join(d["name"] for d in found))
            return None
        dev = found[0]
        chans = dev["kanaele"]
        if not chans:
            print(f"{dev['name']} hat keine Aktor-Kanäle ({dev['typ']})")
            return None
        if nr is None:
            if len(chans) > 1:
                print(f"{dev['name']} hat {len(chans)} Kanäle – Kanal angeben")
                return None
            nr = chans[0]["nr"]
        ch = next((c for c in chans if c["nr"] == nr), None)
        if not ch:
            print(f"Kanal {nr} gibt es nicht")
            return None
        if need_dimmer and ch["art"] != "dimmen":
            print(f"{dev['name']} K{nr} ist kein Dimmer")
            return None
        return dev, ch

    def command(self, line):
        parts = line.split()
        if not parts:
            return True
        c, args = parts[0].lower(), parts[1:]
        if c in ("ende", "exit", "quit"):
            return False
        if c in ("hilfe", "?", "help"):
            print(__doc__.split("Befehle:")[1])
        elif c in ("liste", "l"):
            s = self.installation.server
            print(f"  Server {s['ia']}  Domäne {s['domain']}  SN {s['sn']}")
            for d in self.installation.devices:
                ch = ", ".join(f"K{k['nr']} {k['art']} (Offset {k['offset']})" for k in d["kanaele"])
                print(f"  {d['ia']:<8} {d['name']:<16} {d['typ']:<10} {d['sn']}  {ch or d['rolle']}")
        elif c in ("ein", "aus"):
            p = self.pick(args)
            if p:
                dev, ch = p
                self.send_fp(dev, ch["offset"] + GO_SWITCH, "01" if c == "ein" else "00",
                             f"{dev['name']} K{ch['nr']} {c}")
        elif c == "dimm":
            # percent = first number after the name, an optional channel follows
            i = next((i for i, a in enumerate(args) if i > 0 and a.rstrip("%").isdigit()), None)
            if i is None:
                print("Aufruf: dimm <Gerät> <Prozent> [Kanal]")
                return True
            pct = min(100, int(args[i].rstrip("%")))
            p = self.pick(args[:i] + args[i + 1:], need_dimmer=True)
            if p:
                dev, ch = p
                v = pct * 255 // 100  # like the server: 50 % -> 0x7F
                self.send_fp(dev, ch["offset"] + GO_VALUE, f"{v:02x}", f"{dev['name']} K{ch['nr']} {pct} %")
        elif c == "status":
            p = self.pick(args)
            if p:
                dev, ch = p
                self.send_fp(dev, ch["offset"] + GO_STATUS, None, f"{dev['name']} K{ch['nr']} Status")
                if ch["art"] == "dimmen":
                    time.sleep(0.3)  # wait for the reply, otherwise pending is overwritten
                    self.send_fp(dev, ch["offset"] + GO_STATUS_VALUE, None, f"{dev['name']} K{ch['nr']} Wert")
        elif c == "roh":
            self.bridge(" ".join(args))
        elif c == "broadcast" and args:
            self.show_bc = args[0] == "an"
        elif c in ("szene", "server") and not self.server_cfg:
            print("keine Server-Konfiguration – Start mit --server <Datei>")
        elif c == "zustand":
            for d in self.installation.devices:
                for ch in d["kanaele"]:
                    s = self.state.get((ia_int(d["ia"]), ch["offset"]))
                    if not s:
                        value = "unbekannt"
                    else:
                        value = ("EIN" if s.get("ein") else "AUS") + (
                            f" {s['prozent']} %" if ch["art"] == "dimmen" and s.get("prozent") is not None else "")
                        value += f"  ({s['quelle']}, {s['zeit']})"
                    print(f"  {d['name']:16} K{ch['nr']}  {value}")
        elif c == "szene":
            name = next((n for n in self.server_cfg["szenen"] if fold(n) == fold(" ".join(args))), None)
            if name:
                self.work_q.put(lambda: self.run_scene(name))
            else:
                print("Szenen: " + ", ".join(self.server_cfg["szenen"]))
        elif c == "server" and args:
            self.server_on = args[0] == "an"
            print("Server-Emulation " + ("AN – Tasten werden beantwortet" if self.server_on else "AUS"))
        else:
            print("unbekannt – 'hilfe' zeigt die Befehle")
        return True


def arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def load_installation():
    if arg("--iex"):
        return Installation(iex.read_iex(arg("--iex")))
    path = arg("--anlage") or "anlage.json"
    if not os.path.exists(path):
        sys.exit(f"{path} fehlt – erst: python3 tools/iex.py <Export.iex> -o anlage.json")
    with open(path, encoding="utf-8") as f:
        return Installation(json.load(f))


def main():
    if "-h" in sys.argv or "--help" in sys.argv:
        sys.exit(__doc__)
    installation = load_installation()
    log = open(arg("--log"), "a") if arg("--log") else None
    port = arg("--port")

    if not port:  # display only, e.g. rtl_433 | enet.py
        t = Tester(installation, None, log, "--broadcast" in sys.argv, "--alle" in sys.argv)
        for raw in sys.stdin:
            t.handle_line(raw)
        return

    try:
        import serial  # pyserial
    except ImportError:
        sys.exit("pyserial fehlt: pip3 install pyserial")
    ser = serial.Serial(port, 115200, timeout=0.2)
    server_cfg = None
    if arg("--server"):
        with open(arg("--server"), encoding="utf-8") as f:
            server_cfg = json.load(f)
    t = Tester(installation, ser, log, "--broadcast" in sys.argv, "--alle" in sys.argv, server_cfg)

    def reader():
        buf = b""
        while True:
            buf += ser.read(ser.in_waiting or 1)  # return immediately once data is available
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                t.handle_line(line.decode("utf-8", "replace"))

    threading.Thread(target=reader, daemon=True).start()
    try:
        import readline  # noqa: F401  (line editor/history)
    except ImportError:
        pass
    print(f"{len(installation.devices)} Geräte geladen, Bridge an {port}. 'hilfe' für Befehle.")
    try:
        while t.command(input("> ")):
            pass
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()

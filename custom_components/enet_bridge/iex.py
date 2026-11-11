"""
Read an eNet Server project export (.iex) and return the installation (same format as anlage.json).

No Home Assistant dependencies: used by the integration's setup and by tools/iex.py.

.iex = ZIP -> project/<UUID> = ZIP with the server's project database.
Each *.rec file is a table: header with field schema (name, type, field size),
followed by fixed-length records. Field value = 1 byte tag (bit 7 = null) + data.

Used tables:
  InstaNetBoxDeviceMetaData  server: address, domain, serial number
  DeviceMetaData             devices: address, type, domain, serial number
  KnxRfDevice                devices: name (installationArea), reference to DeviceMetaData, room (locationUID)
  Location                   building/floor/room (locationType 1/2/3); room name → `raum` (suggested HA area)
  KnxRfDeviceChannel         channels: type, GO offset, object, number → assigned to the device via
  KnxRfDeviceChannelConfigurationGroup (field device)

Channels and transmitter keys come from the export; DEVICE_TYPES is only a fallback and description.

Scenes (KNX scenes in the actuators, NOTES.md "Szenen im Export"):
  Scene                      name, server scene channel, values → VT_SCENE_NUMBER (no.)
  InstaNetBoxDeviceFunction  per scene channel: IBOX_INScS.SC (recall GA), .SSGET (sum status GA)
  KnxRfDeviceFunction        actuator function *.SC: currentLinks → list of server functions (= membership)
Keys: rockers are linked directly to actuators (actuator function *.GBR = GO 1, currentLinks → transmitter
functions), single keys are server keys (transmitter function FT_INScS.SSGI → server input, whose GA = answer GA).
Table IDs (upper 32 bits of the references) differ per export: taken from the .rec header (@0x10).
Lists are stored in .blb slots (`4e` + u32 n + n × int64 reference) or as a table .collection/<ID>.rec.

Links (Conjunction, NOTES.md "Server-Tasten aus dem Export") → section "esp" (esp_config.py):
  Conjunction                trueActions → DeviceFunctionAction (single command) or Scene (scene recall)
  Term / TermOperand         trigger: inputValue → DeviceValue, parent = transmitter function FT_INScS.SC
"""
import io, json, struct, sys, zipfile

try:
    from . import layouts
except ImportError:  # tools/iex.py, tools/enet.py import this module on its own
    import layouts  # type: ignore[no-redef]

try:
    from .texts import tr
except ImportError:  # tools/iex.py and tools/enet.py load the module without a package
    from texts import tr

PKG = "com.insta.instanet.instanetbox."
REC_SERVER = PKG + "transformation.knxrf.knxrfboxdevice.InstaNetBoxDeviceMetaData.rec"
REC_META = PKG + "devicemodel.device.DeviceMetaData.rec"
REC_DEVICE = PKG + "transformation.knxrf.knxrfdevice.KnxRfDevice.rec"
REC_GROUP = PKG + "transformation.knxrf.knxrfdevice.channel.KnxRfDeviceChannelConfigurationGroup.rec"
REC_CHANNEL = PKG + "transformation.knxrf.knxrfdevice.channel.KnxRfDeviceChannel.rec"
REC_SCENE = PKG + "applications.scene.Scene.rec"
REC_LOCATION = PKG + "systemfunctions.locationmanagement.Location.rec"
REC_BOX_FUNC = PKG + "transformation.knxrf.knxrfboxdevice.function.InstaNetBoxDeviceFunction.rec"
REC_FUNC = PKG + "transformation.knxrf.knxrfdevice.function.KnxRfDeviceFunction"
REC_VALUE = PKG + "devicemodel.device.value.DeviceValue"
REC_CONJ = PKG + "applications.conjunction."
REC_ACTION = PKG + "devicemodel.device.action.DeviceFunctionAction.rec"

# Actuator function of a link -> (GO, value from "type:value" → bytes): on/off GO 2, brightness GO 4
ACTIONS = {
    "SOO": (2, lambda v: b"\x01" if v == "true" else b"\x00"),
    "ASC": (4, lambda v: bytes([min(255, int(float(v) * 255 / 100))])),
}

# Channel types -> kind. Actuators: "schalten" (GO 2 on/off, GO 20 status), "dimmen" (+ GO 4 value, GO 21),
# "jalousie" (untested). Transmitters: "taste" (single key, sends to offset + 6), "wippe" (offset + 2/3).
# Variants of the actuator types (other GO numbers, layouts.py): CT_1F05 switching with reduced GOs (DIN-rail
# switch/blind actuator), CT_1F08 Switching Actuator v2 (sensIQ), CT_1F01_DUMMY (System 2000 dimming top unit set to
# switching) – untested.
# Operating top unit (UP1A/UP1AV): the actuator sits in the top unit, the flush-mounted insert only provides the load
# output. The insert decides channel 2: CT_1F0B switching insert, CT_1F0D 2-channel switching insert, CT_1F0C
# dimming insert, CT_1F10 blind insert. Channel 1 is the rocker of the top unit (CT_1F12 light, CT_1F1E blind): it
# operates its own insert by wire and is a transmitter for other actuators – a key of the actuator. Not mapped:
# CT_1F0E/CT_1F0E_SA (extension insert: runs in parallel to the main insert, which switches the load). All untested.
CHANNEL_TYPES = {"CT_1F01": "schalten", "CT_1F02": "dimmen", "CT_1F03": "jalousie",
                 "CT_1F05": "schalten", "CT_1F08": "schalten", "CT_1F01_DUMMY": "schalten",
                 "CT_1F0B": "schalten", "CT_1F0D": "schalten", "CT_1F0C": "dimmen", "CT_1F10": "jalousie",
                 "CT_1F19": "energie", "CT_1F1B": "bewegung", "CT_1F06": "repeater",
                 "CT_1F11": "wippe", "CT_1F12": "wippe", "CT_1F1E": "wippe",
                 "CT_1F13": "taste", "CT_1F13BLED": "taste"}
# rocker channels inside an actuator (operating top unit)
ACTUATOR_ROCKERS = {"CT_1F12", "CT_1F1E"}

# Own device definitions: type -> (description, role, channels [(kind, GO offset)])
# Channel kind "schalten": GO 2 on/off, GO 20 status. "dimmen": additionally GO 4 value, GO 21 value status.
DEVICE_TYPES = {
    "DVT_SA1M": ("Schaltaktor Mini 1fach", "aktor", [("schalten", 0)]),
    "DVT_SA1MP": ("Schaltaktor Mini 1fach (P)", "aktor", [("schalten", 0)]),
    "DVT_SA1Z": ("Schalt-Zwischenstecker", "aktor", [("schalten", 0)]),
    "DVT_SA2M": ("Schaltaktor Mini 2fach", "aktor", [("schalten", 0), ("schalten", 26)]),
    "DVT_SD1M": ("DALI-Dimmaktor Mini", "aktor", [("dimmen", 0)]),
    "DVT_DA1M": ("Dimmaktor Mini", "aktor", [("dimmen", 0)]),
    "DVT_WS3BG": ("Wandsender 3fach", "sender", []),
    "DVT_HS4": ("Handsender 4fach", "sender", []),
}

# Product ident -> type; fallback when the local device definitions (definitionen.json) do not know the type.
# The device reports (obj 0 PID 0xD2) e.g. ffff 521032 22, the catalogue lists 521032 4506 26: the first 3 bytes
# are common to both and unique in the catalogue (mini dimmer measured 2026-10-29).
PRODUCT_TYPES = {
    "521012": "DVT_SA2M",
    "521032": "DVT_DA1M",
    "521062": "DVT_SD1M",
    "521102": "DVT_HS4",
    "521132": "DVT_SA1M",
    "521142": "DVT_SA1MP",
    "521302": "DVT_WS3BG",
    "521362": "DVT_SA1Z",
}


def product_ident(product: str) -> str:
    """First 3 bytes of the product number (device: ffff + 4 bytes, catalogue: 6 bytes)."""
    return product[4:10] if product.startswith("ffff") else product[:6]


def parse_rec(b):
    """Return a list of records (dict field name -> value)."""
    hlen, = struct.unpack(">I", b[8:12])
    nfields, recsize = struct.unpack(">II", b[0x38:0x40])
    o, fields = 0x4C, []
    for _ in range(nfields):
        size, = struct.unpack(">I", b[o:o + 4])
        nlen, = struct.unpack(">H", b[o + 4:o + 6])
        name = b[o + 6:o + 6 + 2 * nlen].decode("utf-16-be")
        ftype, fsize = struct.unpack(">II", b[o + 6 + 2 * nlen:o + 14 + 2 * nlen])
        fields.append((name, ftype, fsize))
        o += size
    recs = []
    for p in range(hlen, len(b) - recsize + 1, recsize):
        r, q, d = b[p:p + recsize], 4, {"_valid": b[p:p + 4] == b"\x00\x00\x00\x01"}
        for name, ftype, fsize in fields:
            v = r[q:q + fsize]
            q += fsize
            if v[0] & 0x80:
                d[name] = None
            elif ftype == 0x0A:  # string: tag, max. length (2), length (2), UTF-16
                n, = struct.unpack(">H", v[3:5])
                d[name] = v[5:5 + 2 * n].decode("utf-16-be", "replace")
            elif fsize == 9:
                d[name] = struct.unpack(">q", v[1:9])[0]
            elif fsize == 5:
                d[name] = struct.unpack(">i", v[1:5])[0]
            else:
                d[name] = v[1:]
        recs.append(d)
    return recs


def sn_hex(v):
    return f"{v:012x}"


def ia_str(a):
    return f"{a >> 12}.{(a >> 8) & 15}.{a & 255}"


def short_str(v):
    """Field of size 85 (type IDs): tag, u32 length, ASCII."""
    return bytes(v[4:4 + v[3]]).decode() if v else None


def blb_slot(b, i):
    """Data of a slot from a .blb (header 0x18, slot size @0x0C, slot = u32 length, i32 next, data)."""
    size, = struct.unpack(">I", b[0x0C:0x10])
    o = 0x18 + i * size
    n, = struct.unpack(">I", b[o:o + 4])
    return b[o + 8:o + 8 + n]


def blb_refs(b, i):
    """Reference list from a .blb slot: `4e` + u32 count + int64 references."""
    d = blb_slot(b, i)
    n, = struct.unpack(">I", d[1:5])
    return [struct.unpack(">q", d[5 + 8 * k:13 + 8 * k])[0] for k in range(n)]


def scene_name(name):
    """Room scenes "~LocSceneLightsOn_Küche" → "Küche: Licht an", own "[libenet:NN]Name" → "Name"."""
    for k, t in (("~LocSceneLightsOn_", "Licht an"), ("~LocSceneLightsOff_", "Licht aus"),
                 ("~LocSceneBlindsOn_", "Jalousien auf"), ("~LocSceneBlindsOff_", "Jalousien ab")):
        if name.startswith(k):
            return f"{name[len(k):]}: {t}", "raum"
    return (name.split("]", 1)[1] if name.startswith("[") else name), "eigen"


def _key_links(devices, funcs, funcs_blb, chans, groups, ia_by_dev, box_funcs, tab_func, tab_box):
    """Complete the keys: rocker → `ziele` (actuator channels, GO 1), server key → `antwort_ga`. Transmitter:
    `zentral` = status targets of central channel 9 (CT_1F15, FT_INAOOS.SSGI → actuator SSGI; only LED links,
    no actuator listens to them)."""
    def channel(f):
        c = chans[f["deviceChannel"] & 0xFFFFFFFF]
        return ia_by_dev.get(groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF), c["number"]

    targets, answer, central = {}, {}, {}  # (transmitter IA, channel) -> …; central: transmitter IA -> targets
    for f in funcs:
        if not f["_valid"] or f["currentLinks"] is None:
            continue
        typ = short_str(f["functionType"])
        refs = blb_refs(funcs_blb, f["currentLinks"])
        if typ.endswith(".GBR") and (typ.startswith("FT_INSA") or typ.startswith("FT_INDA")):
            ia, ch = channel(f)
            for ref in refs:
                if ref >> 32 == tab_func:
                    targets.setdefault(channel(funcs[ref & 0xFFFFFFFF]), []).append({"ia": ia, "ch": ch})
        elif typ.startswith("FT_INAOOS") and typ.endswith(".SSGI"):
            for ref in refs:
                if ref >> 32 == tab_func:
                    ia, ch = channel(funcs[ref & 0xFFFFFFFF])
                    central.setdefault(channel(f)[0], []).append({"ia": ia, "ch": ch})
        elif typ.startswith("FT_INScS") and typ.endswith(".SSGI"):
            for ref in refs:
                if ref >> 32 == tab_box:
                    answer[channel(f)] = box_funcs[ref & 0xFFFFFFFF]["sendGroupAddress"]
    for d in devices:
        if d.get("rolle") == "sender":
            d["zentral"] = sorted(central.get(d["ia"], []), key=lambda x: (int(x["ia"].split(".")[2]), x["ch"]))
        for t in d.get("tasten", []):
            key = (d["ia"], t["nr"])
            if t["art"] == "wippe":
                t["ziele"] = sorted(targets.get(key, []), key=lambda x: (int(x["ia"].split(".")[2]), x["ch"]))
            elif key in answer:
                t["antwort_ga"] = answer[key]
    return devices


def _links(raw, table, collection, funcs, chans, groups, ia_by_dev, scene_ref, devices):
    """Links with server keys as trigger → {"server_tasten": [...], "szenen": {...}} for the bridge.
    Server key: transmitter SN + GA (offset + 6; base 0, the bridge compares only the lower byte).
    Action: single command {ia, handle, wert} or scene recall {ia "0.0.0", handle = scene GA, wert = no.}."""
    try:
        conj, conj_blb = table(REC_CONJ + "Conjunction.rec"), raw(REC_CONJ + "Conjunction.blb")
    except StopIteration:
        return {}
    terms, ops = table(REC_CONJ + "Term.rec"), table(REC_CONJ + "TermOperand.rec")
    values = table(REC_VALUE + ".rec")
    try:
        actions, tab_action = table(REC_ACTION), int.from_bytes(raw(REC_ACTION)[0x10:0x14], "big")
    except StopIteration:
        actions, tab_action = [], -1
    tab_func = int.from_bytes(raw(REC_FUNC + ".rec")[0x10:0x14], "big")
    tab_scene = int.from_bytes(raw(REC_SCENE)[0x10:0x14], "big")
    enet_device = {d["ia"]: d for d in devices}

    def channel(fi):
        c = chans[funcs[fi]["deviceChannel"] & 0xFFFFFFFF]
        return ia_by_dev.get(groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF), c

    def value(ref):
        v = values[ref & 0xFFFFFFFF]
        return blb_slot(raw(REC_VALUE + ".blb"), v["value"])[5:].decode().split(":", 1)[1]

    keys_, scenes, missing_ = [], {}, []
    for c in conj:
        if not c["_valid"] or c["term"] is None or c["trueActions"] is None:
            continue
        name = scene_name(c["name"])[0]
        commands = []
        for ref in blb_refs(conj_blb, c["trueActions"]):
            if ref >> 32 == tab_scene and (ref & 0xFFFFFFFF) in scene_ref:
                nr, ga = scene_ref[ref & 0xFFFFFFFF]
                commands.append({"ia": "0.0.0", "handle": ga, "wert": f"{nr:02x}"})
            elif ref >> 32 == tab_action:
                a = actions[ref & 0xFFFFFFFF]
                ia, ch = channel(a["function"] & 0xFFFFFFFF)
                ft = short_str(funcs[a["function"] & 0xFFFFFFFF]["functionType"])
                vals = collection(a["values"])
                if ia and vals and ft.rsplit(".", 1)[1] in ACTIONS:
                    go, enc = ACTIONS[ft.rsplit(".", 1)[1]]
                    commands.append({"ia": ia, "handle": ch["groupObjectNumberOffset"] + go,
                                    "wert": enc(value(vals[0])).hex()})
                else:
                    missing_.append(f"{name}: {ia} {ft}")
            else:
                missing_.append(f"{name}: Aktion {ref:#x}")
        trigger = []
        for ref in collection(terms[c["term"] & 0xFFFFFFFF]["termOperands"]):
            v = values[ops[ref & 0xFFFFFFFF]["inputValue"] & 0xFFFFFFFF]
            if v["parent"] is None or v["parent"] >> 32 != tab_func:
                continue
            ia, ch = channel(v["parent"] & 0xFFFFFFFF)
            if ia in enet_device and short_str(funcs[v["parent"] & 0xFFFFFFFF]["functionType"]).startswith("FT_INScS"):
                trigger.append((enet_device[ia], ch["number"], ch["groupObjectNumberOffset"] + 6))
        if not trigger or not commands:
            continue  # e.g. only recalled from the app
        while name in scenes:
            name += "'"
        scenes[name] = commands
        for g, nr, ga in sorted(trigger, key=lambda x: (int(x[0]["ia"].split(".")[2]), x[1])):
            keys_.append({"name": tr("taste_name", name=g["name"], k=nr), "sender": g["sn"], "ga": ga, "szene": name})
    esp = {"server_tasten": keys_, "szenen": scenes}
    if missing_:
        esp["nicht_uebernommen"] = missing_
    return esp


def read_iex(path):
    with zipfile.ZipFile(path) as outer:
        inner_name = next(n for n in outer.namelist() if n.startswith("project/") and not n.endswith("/"))
        inner = zipfile.ZipFile(io.BytesIO(outer.read(inner_name)))

    def raw(suffix):
        return inner.read(next(n for n in inner.namelist() if n.endswith("/" + suffix)))

    def table(suffix):
        return parse_rec(raw(suffix))

    def collection(ref):
        """List stored as its own table .collection/<ID>.rec (reference = ID << 32 | 0x7fffffff)."""
        name = next((n for n in inner.namelist() if n.endswith(f"/.collection/{ref >> 32}.rec")), None)
        return [r[".ref"] for r in parse_rec(inner.read(name)) if r["_valid"]] if name else []

    srv = table(REC_SERVER)[0]
    meta = table(REC_META)
    groups = table(REC_GROUP)
    chans_by_dev = {}  # index in KnxRfDevice -> channels
    for c in table(REC_CHANNEL):
        if not c["_valid"] or c["parent"] is None:
            continue
        dev_idx = groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF
        art = CHANNEL_TYPES.get(short_str(c["channelType"]))
        if art:
            chans_by_dev.setdefault(dev_idx, []).append(
                {"nr": c["number"], "art": art, "offset": c["groupObjectNumberOffset"], "obj": c["interfaceObjectIndex"],
                 "kanaltyp": short_str(c["channelType"])})
    rooms = {r["uID"]: r["name"] for r in table(REC_LOCATION) if r["_valid"] and r["locationType"] == 3 and r["name"]}
    devices = []
    for idx, dev in enumerate(table(REC_DEVICE)):
        m = meta[dev["metaData"] & 0xFFFFFFFF]  # reference: table ID << 32 | index
        if not dev["_valid"] or m["address"] is None:
            continue  # placeholder
        typ = m["deviceTypeID"]
        desc, role, channels = DEVICE_TYPES.get(typ, (tr("unbekannt"), "unbekannt", []))
        exp = sorted(chans_by_dev.get(idx, []), key=lambda k: k["nr"])
        actuator_channels = [k for k in exp if k["art"] in ("schalten", "dimmen", "jalousie")]
        keys_ = [k for k in exp if k["art"] in ("taste", "wippe")]
        meas_channels = [k for k in exp if k["art"] in ("energie", "bewegung", "repeater")]
        if actuator_channels or any(k["kanaltyp"] not in ACTUATOR_ROCKERS for k in keys_):
            role = "aktor" if actuator_channels else "sender"
            if actuator_channels:  # actuator with a measuring channel (sensIQ: motion + switching)
                actuator_channels = sorted(actuator_channels + meas_channels, key=lambda k: k["nr"])
        elif meas_channels:
            # energy sensor, motion detector, repeater (measurements.py)
            role, actuator_channels = "sensor", meas_channels
        devices.append({
            "name": dev["installationArea"] or typ,
            "ia": ia_str(m["address"]),
            "sn": sn_hex(m["serialNumber"]),
            "typ": typ,
            "beschreibung": desc,
            "rolle": role,
            "kanaele": actuator_channels or [{"nr": i + 1, "art": art, "offset": off} for i, (art, off) in enumerate(channels)],
            # actuator: set further below from the sum status GA, HA checks it against the link table;
            # transmitter: checked on key press
            "ga_basis": 0,
        })
        if keys_:
            devices[-1]["tasten"] = keys_
        if room := rooms.get(dev["locationUID"]):
            devices[-1]["raum"] = room
    devices.sort(key=lambda d: int(d["ia"].split(".")[2]))

    # Scenes: number from the values, GAs from the server functions, members from the actuator links
    chans = table(REC_CHANNEL)
    groups = table(REC_GROUP)
    dev_tab = table(REC_DEVICE)
    values, values_blb = table(REC_VALUE + ".rec"), raw(REC_VALUE + ".blb")
    box_funcs = table(REC_BOX_FUNC)
    funcs, funcs_blb = table(REC_FUNC + ".rec"), raw(REC_FUNC + ".blb")
    tab_box = int.from_bytes(raw(REC_BOX_FUNC)[0x10:0x14], "big")
    tab_func = int.from_bytes(raw(REC_FUNC + ".rec")[0x10:0x14], "big")
    ia_by_dev = {i: ia_str(meta[d["metaData"] & 0xFFFFFFFF]["address"]) for i, d in enumerate(dev_tab)
                 if d["_valid"] and meta[d["metaData"] & 0xFFFFFFFF]["address"] is not None}
    # GA base of the actuators from the sum status GA (function .SSGI = base·256 + offset + SSGI GO, 22 for CT_1F01;
    # the GO depends on the channel type). Not always 0:
    # it increases after a factory reset (one test installation: base 1 for half of the actuators). Wrong → the
    # bridge does not recognise the status messages, and in listen-only mode the actuator then has no state.
    base_by_ia = {}
    for f in funcs:
        if not f["_valid"] or f["deviceChannel"] is None or f["sendGroupAddress"] is None \
                or not short_str(f["functionType"]).endswith(".SSGI"):
            continue
        c = chans[f["deviceChannel"] & 0xFFFFFFFF]
        ia = ia_by_dev.get(groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF)
        rel = f["sendGroupAddress"] - c["groupObjectNumberOffset"] \
            - (layouts.go_of_type(short_str(c["channelType"]), "SSGI") or 22)
        if ia and rel >= 0 and rel % 256 == 0:
            base_by_ia.setdefault(ia, rel >> 8)
    for d in devices:
        if d["rolle"] == "aktor" and d["ia"] in base_by_ia:
            d["ga_basis"] = base_by_ia[d["ia"]]

    members = {}  # index of server function -> [{"ia", "ch"}]
    for f in funcs:
        if not f["_valid"] or f["currentLinks"] is None or not short_str(f["functionType"]).endswith(".SC"):
            continue
        c = chans[f["deviceChannel"] & 0xFFFFFFFF]
        ia = ia_by_dev.get(groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF)
        for ref in blb_refs(funcs_blb, f["currentLinks"]):
            if ref >> 32 == tab_box and ia:
                members.setdefault(ref & 0xFFFFFFFF, []).append({"ia": ia, "ch": c["number"]})
    scenes, scene_ref = [], {}  # scene_ref: index in Scene -> (no., GA) for links
    for idx, sc in enumerate(table(REC_SCENE)):
        if not sc["_valid"] or sc["values"] is None:
            continue
        nr = None
        for ref in collection(sc["values"]):
            v = values[ref & 0xFFFFFFFF]
            if short_str(v["valueType"]) == "VT_SCENE_NUMBER":
                nr = int(blb_slot(values_blb, v["value"])[5:].decode().split(":")[1])
        channel = sc["boxDeviceSceneChannel"]
        fn = {short_str(f["functionType"]).rsplit(".", 1)[1]: (i, f) for i, f in enumerate(box_funcs)
              if f["_valid"] and f["deviceChannel"] == channel}
        if nr is None or "SC" not in fn:
            continue
        scene_ref[idx] = (nr, fn["SC"][1]["sendGroupAddress"])
        name, art = scene_name(sc["name"])
        m = sorted(members.get(fn["SC"][0], []), key=lambda x: (int(x["ia"].split(".")[2]), x["ch"]))
        if not m:
            continue  # e.g. blind room scenes without blinds
        scenes.append({"name": name, "art": art, "nr": nr, "ga": fn["SC"][1]["sendGroupAddress"],
                       "ga_status": fn["SSGET"][1]["sendGroupAddress"] if "SSGET" in fn else None,
                       "mitglieder": m})
    devices = _key_links(devices, funcs, funcs_blb, chans, groups, ia_by_dev, box_funcs, tab_func, tab_box)
    return {
        "server": {"ia": ia_str(srv["address"]), "domain": sn_hex(srv["domain"]), "sn": sn_hex(srv["serialNumber"]),
                   "verschluesselt": srv["networkKeyEncrypted"] is not None},
        "geraete": devices,
        "szenen": scenes,
        "esp": _links(raw, table, collection, funcs, chans, groups, ia_by_dev, scene_ref, devices),
    }


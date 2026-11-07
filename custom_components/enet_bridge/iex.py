"""
Liest einen eNet-Server-Projektexport (.iex) und liefert die Anlage (Format wie anlage.json).

Ohne Home-Assistant-Abhängigkeiten: genutzt von der Einrichtung der Integration und von tools/iex.py.

.iex = ZIP -> project/<UUID> = ZIP mit der Projektdatenbank des Servers.
Jede *.rec-Datei ist eine Tabelle: Kopf mit Feldschema (Name, Typ, Feldgröße),
danach Datensätze fester Länge. Feldwert = 1 Byte Tag (Bit 7 = null) + Daten.

Verwendet werden:
  InstaNetBoxDeviceMetaData  Server: Adresse, Domäne, Seriennummer
  DeviceMetaData             Geräte: Adresse, Typ, Domäne, Seriennummer
  KnxRfDevice                Geräte: Name (installationArea), Verweis auf DeviceMetaData, Raum (locationUID)
  Location                   Gebäude/Stockwerk/Raum (locationType 1/2/3); Raumname → „raum“ (Vorschlag HA-Bereich)
  KnxRfDeviceChannel         Kanäle: Typ, GO-Offset, Objekt, Nummer → über
  KnxRfDeviceChannelConfigurationGroup (Feld device) dem Gerät zugeordnet

Kanäle und Sender-Tasten kommen aus dem Export; DEVICE_TYPES ist nur Rückfall und Beschreibung.

Szenen (KNX-Szenen in den Aktoren, NOTES.md „Szenen im Export“):
  Scene                      Name, Szenenkanal des Servers, values → VT_SCENE_NUMBER (Nr.)
  InstaNetBoxDeviceFunction  je Szenenkanal: IBOX_INScS.SC (GA Aufruf), .SSGET (GA Sammelstatus)
  KnxRfDeviceFunction        Aktor-Funktion *.SC: currentLinks → Liste der Server-Funktionen (= Mitgliedschaft)
Tasten: Wippen sind direkt mit Aktoren verknüpft (Aktor-Funktion *.GBR = GO 1, currentLinks → Sender-Funktionen),
Einzeltasten sind Server-Tasten (Sender-Funktion FT_INScS.SSGI → Server-Eingang, dessen GA = Antwort-GA).
Tabellen-IDs (obere 32 Bit der Verweise) sind pro Export verschieden: aus dem Kopf der .rec (@0x10).
Listen stehen in .blb-Slots (`4e` + u32 n + n × int64-Verweis) bzw. als Tabelle .collection/<ID>.rec.

Verknüpfungen (Conjunction, NOTES.md „Server-Tasten aus dem Export“) → Abschnitt "esp" (esp_config.py):
  Conjunction                trueActions → DeviceFunctionAction (Einzelbefehl) oder Scene (Szenenaufruf)
  Term / TermOperand         Auslöser: inputValue → DeviceValue, parent = Sender-Funktion FT_INScS.SC
"""
import io, json, struct, sys, zipfile

try:
    from .texte import tr
except ImportError:  # tools/iex.py und tools/enet.py laden das Modul ohne Paket
    from texte import tr

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

# Aktor-Funktion einer Verknüpfung -> (GO, Wert aus „Typ:Wert“ → Bytes): Ein/Aus GO 2, Helligkeit GO 4
AKTIONEN = {
    "SOO": (2, lambda v: b"\x01" if v == "true" else b"\x00"),
    "ASC": (4, lambda v: bytes([min(255, int(float(v) * 255 / 100))])),
}

# Kanaltypen -> Art. Aktoren: "schalten" (GO 2 Ein/Aus, GO 20 Status), "dimmen" (+ GO 4 Wert, GO 21),
# "jalousie" (ungetestet). Sender: "taste" (Einzeltaste, sendet an Offset + 6), "wippe" (Offset + 2/3).
CHANNEL_TYPES = {"CT_1F01": "schalten", "CT_1F02": "dimmen", "CT_1F03": "jalousie",
                 "CT_1F11": "wippe", "CT_1F13": "taste", "CT_1F13BLED": "taste"}

# Eigene Gerätedefinitionen: Typ -> (Beschreibung, Rolle, Kanäle [(Art, GO-Offset)])
# Kanalart "schalten": GO 2 Ein/Aus, GO 20 Status. "dimmen": zusätzlich GO 4 Wert, GO 21 Status Wert.
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

# Produktkennung -> Typ; Rückfall, wenn die lokalen Gerätedefinitionen (definitionen.json) den Typ nicht kennen.
# Das Gerät meldet (Obj 0 PID 0xD2) z. B. ffff 521032 22, der Katalog führt 521032 4506 26: gemeinsam und im
# Katalog eindeutig sind die ersten 3 Byte (Mini-Dimmer gemessen 2026-10-29).
PRODUKT_TYPEN = {
    "521012": "DVT_SA2M",
    "521032": "DVT_DA1M",
    "521062": "DVT_SD1M",
    "521102": "DVT_HS4",
    "521132": "DVT_SA1M",
    "521142": "DVT_SA1MP",
    "521302": "DVT_WS3BG",
    "521362": "DVT_SA1Z",
}


def produkt_kennung(produkt: str) -> str:
    """Erste 3 Byte der Produktnummer (Gerät: ffff + 4 Byte, Katalog: 6 Byte)."""
    return produkt[4:10] if produkt.startswith("ffff") else produkt[:6]


def parse_rec(b):
    """Liefert eine Liste von Datensätzen (dict Feldname -> Wert)."""
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
            elif ftype == 0x0A:  # String: Tag, max. Länge (2), Länge (2), UTF-16
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
    """Feld mit Größe 85 (Typ-IDs): Tag, u32 Länge, ASCII."""
    return bytes(v[4:4 + v[3]]).decode() if v else None


def blb_slot(b, i):
    """Daten eines Slots aus einer .blb (Kopf 0x18, Slotgröße @0x0C, Slot = u32 Länge, i32 next, Daten)."""
    size, = struct.unpack(">I", b[0x0C:0x10])
    o = 0x18 + i * size
    n, = struct.unpack(">I", b[o:o + 4])
    return b[o + 8:o + 8 + n]


def blb_refs(b, i):
    """Verweisliste aus einem .blb-Slot: `4e` + u32 Anzahl + int64-Verweise."""
    d = blb_slot(b, i)
    n, = struct.unpack(">I", d[1:5])
    return [struct.unpack(">q", d[5 + 8 * k:13 + 8 * k])[0] for k in range(n)]


def szenen_name(name):
    """Raumszenen „~LocSceneLightsOn_Küche“ → „Küche: Licht an“, eigene „[libenet:NN]Name“ → „Name“."""
    for k, t in (("~LocSceneLightsOn_", "Licht an"), ("~LocSceneLightsOff_", "Licht aus"),
                 ("~LocSceneBlindsOn_", "Jalousien auf"), ("~LocSceneBlindsOff_", "Jalousien ab")):
        if name.startswith(k):
            return f"{name[len(k):]}: {t}", "raum"
    return (name.split("]", 1)[1] if name.startswith("[") else name), "eigen"


def _tasten_links(devices, funcs, funcs_blb, chans, groups, ia_by_dev, box_funcs, tab_func, tab_box):
    """Tasten ergänzen: Wippe → „ziele“ (Aktor-Kanäle, GO 1), Server-Taste → „antwort_ga“. Sender: „zentral“ =
    Status-Ziele des Zentralkanals 9 (CT_1F15, FT_INAOOS.SSGI → Aktor-SSGI; nur LED-Links, kein Aktor hört darauf)."""
    def kanal(f):
        c = chans[f["deviceChannel"] & 0xFFFFFFFF]
        return ia_by_dev.get(groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF), c["number"]

    ziele, antwort, zentral = {}, {}, {}  # (Sender-IA, Kanal) -> …; zentral: Sender-IA -> Ziele
    for f in funcs:
        if not f["_valid"] or f["currentLinks"] is None:
            continue
        typ = short_str(f["functionType"])
        refs = blb_refs(funcs_blb, f["currentLinks"])
        if typ.endswith(".GBR") and (typ.startswith("FT_INSA") or typ.startswith("FT_INDA")):
            ia, ch = kanal(f)
            for ref in refs:
                if ref >> 32 == tab_func:
                    ziele.setdefault(kanal(funcs[ref & 0xFFFFFFFF]), []).append({"ia": ia, "ch": ch})
        elif typ.startswith("FT_INAOOS") and typ.endswith(".SSGI"):
            for ref in refs:
                if ref >> 32 == tab_func:
                    ia, ch = kanal(funcs[ref & 0xFFFFFFFF])
                    zentral.setdefault(kanal(f)[0], []).append({"ia": ia, "ch": ch})
        elif typ.startswith("FT_INScS") and typ.endswith(".SSGI"):
            for ref in refs:
                if ref >> 32 == tab_box:
                    antwort[kanal(f)] = box_funcs[ref & 0xFFFFFFFF]["sendGroupAddress"]
    for d in devices:
        if d.get("rolle") == "sender":
            d["zentral"] = sorted(zentral.get(d["ia"], []), key=lambda x: (int(x["ia"].split(".")[2]), x["ch"]))
        for t in d.get("tasten", []):
            key = (d["ia"], t["nr"])
            if t["art"] == "wippe":
                t["ziele"] = sorted(ziele.get(key, []), key=lambda x: (int(x["ia"].split(".")[2]), x["ch"]))
            elif key in antwort:
                t["antwort_ga"] = antwort[key]
    return devices


def _verknuepfungen(raw, table, collection, funcs, chans, groups, ia_by_dev, szenen_ref, devices):
    """Verknüpfungen mit Server-Tasten als Auslöser → {"server_tasten": [...], "szenen": {...}} für die Bridge.
    Server-Taste: Sender-SN + GA (Offset + 6; Basis 0, die Bridge vergleicht nur das untere Byte).
    Aktion: Einzelbefehl {ia, handle, wert} oder Szenenaufruf {ia "0.0.0", handle = Szenen-GA, wert = Nr.}."""
    try:
        conj, conj_blb = table(REC_CONJ + "Conjunction.rec"), raw(REC_CONJ + "Conjunction.blb")
    except StopIteration:
        return {}
    terms, ops = table(REC_CONJ + "Term.rec"), table(REC_CONJ + "TermOperand.rec")
    values = table(REC_VALUE + ".rec")
    try:
        aktionen, tab_aktion = table(REC_ACTION), int.from_bytes(raw(REC_ACTION)[0x10:0x14], "big")
    except StopIteration:
        aktionen, tab_aktion = [], -1
    tab_func = int.from_bytes(raw(REC_FUNC + ".rec")[0x10:0x14], "big")
    tab_szene = int.from_bytes(raw(REC_SCENE)[0x10:0x14], "big")
    geraet = {d["ia"]: d for d in devices}

    def kanal(fi):
        c = chans[funcs[fi]["deviceChannel"] & 0xFFFFFFFF]
        return ia_by_dev.get(groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF), c

    def wert(ref):
        v = values[ref & 0xFFFFFFFF]
        return blb_slot(raw(REC_VALUE + ".blb"), v["value"])[5:].decode().split(":", 1)[1]

    tasten, szenen, fehlt = [], {}, []
    for c in conj:
        if not c["_valid"] or c["term"] is None or c["trueActions"] is None:
            continue
        name = szenen_name(c["name"])[0]
        befehle = []
        for ref in blb_refs(conj_blb, c["trueActions"]):
            if ref >> 32 == tab_szene and (ref & 0xFFFFFFFF) in szenen_ref:
                nr, ga = szenen_ref[ref & 0xFFFFFFFF]
                befehle.append({"ia": "0.0.0", "handle": ga, "wert": f"{nr:02x}"})
            elif ref >> 32 == tab_aktion:
                a = aktionen[ref & 0xFFFFFFFF]
                ia, ch = kanal(a["function"] & 0xFFFFFFFF)
                ft = short_str(funcs[a["function"] & 0xFFFFFFFF]["functionType"])
                vals = collection(a["values"])
                if ia and vals and ft.rsplit(".", 1)[1] in AKTIONEN:
                    go, kod = AKTIONEN[ft.rsplit(".", 1)[1]]
                    befehle.append({"ia": ia, "handle": ch["groupObjectNumberOffset"] + go,
                                    "wert": kod(wert(vals[0])).hex()})
                else:
                    fehlt.append(f"{name}: {ia} {ft}")
            else:
                fehlt.append(f"{name}: Aktion {ref:#x}")
        ausloeser = []
        for ref in collection(terms[c["term"] & 0xFFFFFFFF]["termOperands"]):
            v = values[ops[ref & 0xFFFFFFFF]["inputValue"] & 0xFFFFFFFF]
            if v["parent"] is None or v["parent"] >> 32 != tab_func:
                continue
            ia, ch = kanal(v["parent"] & 0xFFFFFFFF)
            if ia in geraet and short_str(funcs[v["parent"] & 0xFFFFFFFF]["functionType"]).startswith("FT_INScS"):
                ausloeser.append((geraet[ia], ch["number"], ch["groupObjectNumberOffset"] + 6))
        if not ausloeser or not befehle:
            continue  # z. B. nur aus der App aufgerufen
        while name in szenen:
            name += "'"
        szenen[name] = befehle
        for g, nr, ga in sorted(ausloeser, key=lambda x: (int(x[0]["ia"].split(".")[2]), x[1])):
            tasten.append({"name": tr("taste_name", name=g["name"], k=nr), "sender": g["sn"], "ga": ga, "szene": name})
    esp = {"server_tasten": tasten, "szenen": szenen}
    if fehlt:
        esp["nicht_uebernommen"] = fehlt
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
        """Liste als eigene Tabelle .collection/<ID>.rec (Verweis = ID << 32 | 0x7fffffff)."""
        name = next((n for n in inner.namelist() if n.endswith(f"/.collection/{ref >> 32}.rec")), None)
        return [r[".ref"] for r in parse_rec(inner.read(name)) if r["_valid"]] if name else []

    srv = table(REC_SERVER)[0]
    meta = table(REC_META)
    groups = table(REC_GROUP)
    chans_by_dev = {}  # Index in KnxRfDevice -> Kanäle
    for c in table(REC_CHANNEL):
        if not c["_valid"] or c["parent"] is None:
            continue
        dev_idx = groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF
        art = CHANNEL_TYPES.get(short_str(c["channelType"]))
        if art:
            chans_by_dev.setdefault(dev_idx, []).append(
                {"nr": c["number"], "art": art, "offset": c["groupObjectNumberOffset"], "obj": c["interfaceObjectIndex"]})
    raeume = {r["uID"]: r["name"] for r in table(REC_LOCATION) if r["_valid"] and r["locationType"] == 3 and r["name"]}
    devices = []
    for idx, dev in enumerate(table(REC_DEVICE)):
        m = meta[dev["metaData"] & 0xFFFFFFFF]  # Verweis: Tabellen-ID << 32 | Index
        if not dev["_valid"] or m["address"] is None:
            continue  # Platzhalter
        typ = m["deviceTypeID"]
        desc, role, channels = DEVICE_TYPES.get(typ, (tr("unbekannt"), "unbekannt", []))
        exp = sorted(chans_by_dev.get(idx, []), key=lambda k: k["nr"])
        aktor_k = [k for k in exp if k["art"] in ("schalten", "dimmen", "jalousie")]
        tasten = [k for k in exp if k["art"] in ("taste", "wippe")]
        if aktor_k or tasten:
            role = "aktor" if aktor_k else "sender"
        devices.append({
            "name": dev["installationArea"] or typ,
            "ia": ia_str(m["address"]),
            "sn": sn_hex(m["serialNumber"]),
            "typ": typ,
            "beschreibung": desc,
            "rolle": role,
            "kanaele": aktor_k or [{"nr": i + 1, "art": art, "offset": off} for i, (art, off) in enumerate(channels)],
            "ga_basis": 0,  # Aktor: unten aus der Sammelstatus-GA; HA prüft sie an der Link-Tabelle, Sender: am Tastendruck
        })
        if tasten:
            devices[-1]["tasten"] = tasten
        if raum := raeume.get(dev["locationUID"]):
            devices[-1]["raum"] = raum
    devices.sort(key=lambda d: int(d["ia"].split(".")[2]))

    # Szenen: Nummer aus den Werten, GAs aus den Server-Funktionen, Mitglieder aus den Aktor-Links
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
    # GA-Basis der Aktoren aus der Sammelstatus-GA (Funktion .SSGI = Basis·256 + Offset + 22). Nicht überall 0:
    # Nach einem Werksreset steigt sie (eine Testanlage: Basis 1 bei der Hälfte der Aktoren). Falsch → die Bridge erkennt
    # die Statusmeldungen nicht, im Mithören bleibt der Aktor dann ohne Zustand.
    basis_by_ia = {}
    for f in funcs:
        if not f["_valid"] or f["deviceChannel"] is None or f["sendGroupAddress"] is None \
                or not short_str(f["functionType"]).endswith(".SSGI"):
            continue
        c = chans[f["deviceChannel"] & 0xFFFFFFFF]
        ia = ia_by_dev.get(groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF)
        rel = f["sendGroupAddress"] - c["groupObjectNumberOffset"] - 22
        if ia and rel >= 0 and rel % 256 == 0:
            basis_by_ia.setdefault(ia, rel >> 8)
    for d in devices:
        if d["rolle"] == "aktor" and d["ia"] in basis_by_ia:
            d["ga_basis"] = basis_by_ia[d["ia"]]

    mitglieder = {}  # Index Server-Funktion -> [{"ia", "ch"}]
    for f in funcs:
        if not f["_valid"] or f["currentLinks"] is None or not short_str(f["functionType"]).endswith(".SC"):
            continue
        c = chans[f["deviceChannel"] & 0xFFFFFFFF]
        ia = ia_by_dev.get(groups[c["parent"] & 0xFFFFFFFF]["device"] & 0xFFFFFFFF)
        for ref in blb_refs(funcs_blb, f["currentLinks"]):
            if ref >> 32 == tab_box and ia:
                mitglieder.setdefault(ref & 0xFFFFFFFF, []).append({"ia": ia, "ch": c["number"]})
    szenen, szenen_ref = [], {}  # szenen_ref: Index in Scene -> (Nr., GA) für Verknüpfungen
    for idx, sc in enumerate(table(REC_SCENE)):
        if not sc["_valid"] or sc["values"] is None:
            continue
        nr = None
        for ref in collection(sc["values"]):
            v = values[ref & 0xFFFFFFFF]
            if short_str(v["valueType"]) == "VT_SCENE_NUMBER":
                nr = int(blb_slot(values_blb, v["value"])[5:].decode().split(":")[1])
        kanal = sc["boxDeviceSceneChannel"]
        fn = {short_str(f["functionType"]).rsplit(".", 1)[1]: (i, f) for i, f in enumerate(box_funcs)
              if f["_valid"] and f["deviceChannel"] == kanal}
        if nr is None or "SC" not in fn:
            continue
        szenen_ref[idx] = (nr, fn["SC"][1]["sendGroupAddress"])
        name, art = szenen_name(sc["name"])
        m = sorted(mitglieder.get(fn["SC"][0], []), key=lambda x: (int(x["ia"].split(".")[2]), x["ch"]))
        if not m:
            continue  # z. B. Jalousie-Raumszenen ohne Jalousien
        szenen.append({"name": name, "art": art, "nr": nr, "ga": fn["SC"][1]["sendGroupAddress"],
                       "ga_status": fn["SSGET"][1]["sendGroupAddress"] if "SSGET" in fn else None,
                       "mitglieder": m})
    devices = _tasten_links(devices, funcs, funcs_blb, chans, groups, ia_by_dev, box_funcs, tab_func, tab_box)
    return {
        "server": {"ia": ia_str(srv["address"]), "domain": sn_hex(srv["domain"]), "sn": sn_hex(srv["serialNumber"]),
                   "verschluesselt": srv["networkKeyEncrypted"] is not None},
        "geraete": devices,
        "szenen": szenen,
        "esp": _verknuepfungen(raw, table, collection, funcs, chans, groups, ia_by_dev, szenen_ref, devices),
    }


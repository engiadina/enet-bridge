#!/usr/bin/env python3
"""
Generate device definitions from the Insta catalogue of the eNet Server firmware.

  python3 tools/katalog.py --alle                      # all types → custom_components/enet_bridge/definitionen.json
  python3 tools/katalog.py [--katalog katalog/fw2.21] [--aus …] [--typen DVT_SD1M,…]

The catalogue (devices_2.x.0.tgz from the server update) is kept locally (katalog/, not in the repo). The definitions
of all types generated from it have been part of the integration since 2026-10-29; a local
/config/enet/definitionen.json extends or overrides them.

Chain in the catalogue (NOTES.md "Gerätekatalog"):
  manufacturer catalogue DVT_x → GDVT_x (GenericDeviceCatalogue) → channels (channel type CT_…, object, GO offset)
  parameters (ChannelTypes + device-specific) → KnxRfPropertyData (PID, index, elementCount)
  → ValueTypeContainerType → ValueType (default, DataType) + KnxRfDatapointType (bit length, converter)
  → DataType (range/unit, choice list, bool).
Parameter memory (PID 79 = 0x4F): radio start element = index + 1, elements = bytes.
"""
import glob, json, os, re, sys
import xml.etree.ElementTree as ET


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def load(path):
    return strip_ns(ET.parse(path).getroot())


class Catalogue:
    def __init__(self, d):
        self.d = d
        self.channel_types = {e.get("id"): e for e in load(f"{d}/ChannelTypes.xml").iter("KnxRfChannelType")}
        self.generic = {e.get("id"): e for e in load(f"{d}/GenericDeviceCatalogue.xml").iter("KnxRfGenericDeviceType")}
        self.vtct = {e.get("id"): e for e in load(f"{d}/ValueTypeContainerTypes.xml").iter("KnxRfValueTypeContainerType")}
        self.vt = {e.get("id"): e for e in load(f"{d}/ValueTypes.xml").iter("ValueType") if e.get("id")}
        self.dt = {e.get("id"): e for e in load(f"{d}/DataTypes.xml").iter("DataType")}
        self.dpt = {e.get("id"): e for e in load(f"{d}/KnxRfDatapointTypes.xml").iter("KnxRfDatapointType")}
        self.devtypes = {}
        for f in glob.glob(f"{d}/*/DeviceCatalogue.xml"):
            manufacturer_ = os.path.basename(os.path.dirname(f))
            for e in load(f).iter("KnxRfDeviceType"):
                md = e.find("MetaData")
                self.devtypes[e.get("id")] = (e.get("genericDeviceTypeID"), manufacturer_, e.get("productID"),
                                              md.get("articleNo") if md is not None else None,
                                              md.get("name") if md is not None else None)

    # --- Value types -------------------------------------------------------
    def value_type(self, vtct_id):
        """Value description for a ValueTypeContainerType (only simple types with one data field)."""
        c = self.vtct.get(vtct_id)
        if c is None:
            return {"art": "unbekannt", "vtct": vtct_id}
        vts = [v.get("valueTypeID") for v in c.iter("ValueType")]
        dpt_ref = c.find("KnxRfDatapointType")
        dpt = self.dpt.get(dpt_ref.get("datapointTypeID")) if dpt_ref is not None else None
        out = {"vtct": vtct_id}
        if dpt is not None:
            out["dpt"] = dpt.get("id")
            out["bits"] = int(dpt.get("bitLength") or 0)
            if dpt.get("converterTypeID"):
                out["konverter"] = dpt.get("converterTypeID")
        if len(vts) != 1 or vts[0] not in self.vt:
            out["art"] = "zusammengesetzt"
            return out
        vt = self.vt[vts[0]]
        default = vt.findtext("DefaultValue")
        dt = self.dt.get(vt.get("dataTypeID"))
        if dt is None:
            out["art"] = "unbekannt"
            return out
        typ = dt.get("type")
        if typ == "BOOL":
            out["art"] = "bool"
            out["standard"] = default == "true"
        elif typ == "MAPENTRYKEY":
            out["art"] = "auswahl"
            out["werte"] = {e.get("key"): int(e.get("value")) for e in dt.iter("Entry")}
            out["standard"] = default
        elif typ in ("INTEGER", "DOUBLE", "FLOAT", "LONG"):
            num = next((x for x in dt if x.tag.startswith("DataType")), None)
            out["art"] = "zahl"
            if num is not None:
                for k_xml, k in (("minValue", "min"), ("maxValue", "max"), ("increment", "schritt")):
                    if num.get(k_xml) is not None:
                        out[k] = float(num.get(k_xml))
                if num.get("unit") == "x 10":  # switching counter: raw value × 10
                    out["faktor"] = 10
                elif num.get("unit"):
                    out["einheit"] = num.get("unit")
            try:
                out["standard"] = float(default)
            except (TypeError, ValueError):
                out["standard"] = default
        else:
            out["art"] = typ.lower() if typ else "unbekannt"
            out["standard"] = default
        out["kodierung"] = encoding(out)
        return out

    # --- Parameter ---------------------------------------------------------
    def parameter(self, el, channel_obj):
        """Parameter entry from KnxRfDeviceParameterType (None if it has no radio address)."""
        pd = el.find("KnxRfPropertyData")
        if pd is None or pd.get("propertyID") is None:
            return None
        pid = int(pd.get("propertyID"))
        idx = int(pd.get("index") or 0)
        pt_id = el.get("id")
        short = re.sub(r"^PT_[A-Z0-9]+[._]", "", pt_id)
        p = {
            "id": short,
            "katalog_id": pt_id,
            "name": el.get("name"),
            "obj": int(pd.get("interfaceObjectIndex")) if pd.get("interfaceObjectIndex") else channel_obj,
            "pid": pid,
            "start": idx + 1 if pid == 79 else idx,
            "anzahl": int(pd.get("elementCount") or 1),
            "lesen": int(el.get("accessLevelRead") or 0),
            "schreiben": int(el.get("accessLevelWrite") or 0),
            "aktiv": el.get("active", "true") != "false",
            "typ": self.value_type(el.get("valueTypeContainerTypeID")),
        }
        if el.get("writeProtectedAtDevice") == "true":
            p["schreibgeschuetzt"] = True
        return p

    def enet_device(self, dvt):
        gdvt_id, manufacturer_, product, article, product_name = self.devtypes.get(
            dvt, (dvt.replace("DVT_", "GDVT_"), None, None, None, None))
        g = self.generic.get(gdvt_id)
        if g is None:
            return None
        out = {"name": g.get("name"), "generisch": gdvt_id, "hersteller": manufacturer_, "produkt": product,
               "artikel": article, "produktname": product_name,
               "geraet_obj": int(g.get("interfaceObjectIndexUserObjectSystem") or 0), "kanaele": [], "geraet": []}
        # parameters a channel type can depend on (e.g. the insert type of an operating top unit), by ID
        selectors = {}
        for el in g.iter("KnxRfDeviceParameterType"):
            if el.get("id") and (p := self.parameter(el, 0)) and p["typ"].get("art") == "auswahl":
                selectors[el.get("id")] = p
        for grp in g.iter("KnxRfDeviceChannelConfigurationGroupType"):
            for ch in grp.iter("KnxRfDeviceChannelType"):
                ct_id, nr = ch.get("channelTypeID"), int(ch.get("number"))
                obj = int(ch.get("interfaceObjectIndex") or 0)
                params = []
                ct = self.channel_types.get(ct_id)
                if ct is not None:
                    for el in ct.iter("KnxRfDeviceParameterType"):
                        p = self.parameter(el, obj)
                        if p:
                            params.append(p)
                spec = ch.find("KnxRfDeviceParameterTypes")
                if spec is not None:
                    for el in spec.findall("KnxRfDeviceParameterType"):
                        p = self.parameter(el, obj)
                        if p:
                            params.append(p)
                entry_ = {"nr": nr, "kanaltyp": ct_id, "obj": obj,
                           "go_offset": int(ch.get("groupObjectNumberOffset") or 0), "parameter": params}
                if ct_id == "CT_DEVICE":
                    out["geraet"] = params
                else:
                    if ct is not None:
                        entry_["alias"] = ct.get("aliasID")
                    if variants := self.channel_variants(ch, selectors):
                        entry_["varianten"] = variants
                    out["kanaele"].append(entry_)
        return out

    @staticmethod
    def channel_variants(ch, selectors):
        """Channel type chosen by a device parameter (KnxRfDependentValueChannelSetting), e.g. operating top unit
        UP1A: the insert type (obj 9 PID 62) decides whether channel 2 switches, dims or drives a blind. Returns
        [{"parameter": {id, obj, pid, start, kodierung}, "werte": {"<raw value>": {kanaltyp, obj, go_offset}}}] in
        catalog order; only parameters with a radio address."""
        out = []
        for dep in ch.iter("KnxRfDependentDeviceParameterValueType"):
            p = selectors.get(dep.get("dependOnParameterTypeID"))
            if p is None:
                continue
            values = {}
            for s in dep.iter("KnxRfDependentValueChannelSetting"):
                raw = p["typ"]["werte"].get(s.get("dependOnValue"))
                if s.get("channelTypeID") and raw is not None:
                    values[str(raw)] = {"kanaltyp": s.get("channelTypeID"),
                                        "obj": int(s.get("interfaceObjectIndex") or 0),
                                        "go_offset": int(s.get("groupObjectNumberOffset") or 0)}
            if values:
                out.append({"parameter": {k: p[k] for k in ("katalog_id", "obj", "pid", "start")}
                            | {"kodierung": p["typ"]["kodierung"]}, "werte": values})
        return out

    def channel_type_layouts(self):
        """Group object number per device function of every channel type: {CT: {"alias", "go": {"SOO": 2, …}}}.
        Installations switch channels to variants of the default type (e.g. CT_1F05 instead of CT_1F01) whose group
        objects are numbered differently – so the GO of a function is taken from the channel type."""
        out = {}
        for ct_id, ct in sorted(self.channel_types.items()):
            go = {f.get("id").rsplit(".", 1)[-1]: int(f.get("groupObjectNumber"))
                  for f in ct.iter("KnxRfDeviceFunctionType") if f.get("groupObjectNumber")}
            if go:
                out[ct_id] = {"alias": ct.get("aliasID"), "go": go}
        return out


def encoding(t):
    """How the value is stored in parameter memory (HA converts with this)."""
    dpt, bits, conv = t.get("dpt", ""), t.get("bits", 0), t.get("konverter", "")
    if conv == "CVT_2_OCTET_FLOAT_VALUE_TO_DOUBLE":
        return "dpt9"
    if dpt.startswith("DPT_5.001") or dpt.startswith("DPT_5.004") and t.get("einheit") == "%":
        return "prozent255" if dpt.startswith("DPT_5.001") else "u8"
    if bits == 8 or t.get("art") in ("bool", "auswahl") and bits <= 8:
        return "u8"
    if bits == 16:
        return "u16_x100" if "RESOLUTION_100" in conv else "u16"
    if bits == 32:
        return "u32"
    return "unbekannt"


def main():
    kat = Catalogue(arg("--katalog", "katalog/fw2.21"))
    all_ = "--alle" in sys.argv
    off = arg("--aus", "custom_components/enet_bridge/definitionen.json" if all_ else "katalog/definitionen.json")
    types_ = arg("--typen")
    if all_:
        types_ = sorted(kat.devtypes)
    elif types_:
        types_ = types_.split(",")
    else:  # all types of the own installations (anlage*.json), otherwise all known ones
        types_ = sorted({g["typ"] for f in glob.glob("anlage*.json")
                        for g in json.load(open(f, encoding="utf-8"))["geraete"]}) or sorted(kat.devtypes)
    defs = {}
    for t in types_:
        d = kat.enet_device(t)
        if d is None:
            print(f"{t}: nicht im Katalog", file=sys.stderr)
            continue
        defs[t] = d
        n = sum(len(k["parameter"]) for k in d["kanaele"]) + len(d["geraet"])
        unknown_ = sum(1 for k in d["kanaele"] for p in k["parameter"] if p["typ"].get("kodierung") in (None, "unbekannt"))
        print(f"{t:<12} {d['name'][:40]:<40} {len(d['kanaele'])} Kanäle, {n} Parameter ({unknown_} ohne Kodierung)")
    os.makedirs(os.path.dirname(off) or ".", exist_ok=True)
    with open(off, "w", encoding="utf-8") as f:
        json.dump({"quelle": os.path.basename(os.path.abspath(kat.d)), "typen": defs,
                   "kanaltypen": kat.channel_type_layouts()}, f, ensure_ascii=False,
                  separators=(",", ":"), sort_keys=True)
        f.write("\n")
    print(f"→ {off}")


if __name__ == "__main__":
    main()

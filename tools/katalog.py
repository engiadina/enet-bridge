#!/usr/bin/env python3
"""
Gerätedefinitionen aus dem Insta-Katalog der eNet-Server-Firmware erzeugen.

  python3 tools/katalog.py --alle                      # alle Typen → custom_components/enet_bridge/definitionen.json
  python3 tools/katalog.py [--katalog katalog/fw2.21] [--aus …] [--typen DVT_SD1M,…]

Der Katalog (devices_2.x.0.tgz aus dem Server-Update) liegt lokal (katalog/, nicht im Repo). Die daraus erzeugten
Definitionen aller Typen liegen seit 2026-10-29 in der Integration (Erlaubnis klärt der Nutzer); eine lokale
/config/enet/definitionen.json ergänzt bzw. überschreibt sie.

Kette im Katalog (NOTES.md „Gerätekatalog“):
  Herstellerkatalog DVT_x → GDVT_x (GenericDeviceCatalogue) → Kanäle (Kanaltyp CT_…, Objekt, GO-Offset)
  Parameter (ChannelTypes + gerätespezifische) → KnxRfPropertyData (PID, index, elementCount)
  → ValueTypeContainerType → ValueType (Standard, DataType) + KnxRfDatapointType (Bitlänge, Konverter)
  → DataType (Bereich/Einheit, Auswahlliste, Bool).
Parameterspeicher (PID 79 = 0x4F): Funk-Startelement = index + 1, Elemente = Bytes.
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


class Katalog:
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
            hersteller = os.path.basename(os.path.dirname(f))
            for e in load(f).iter("KnxRfDeviceType"):
                md = e.find("MetaData")
                self.devtypes[e.get("id")] = (e.get("genericDeviceTypeID"), hersteller, e.get("productID"),
                                              md.get("articleNo") if md is not None else None,
                                              md.get("name") if md is not None else None)

    # --- Wertetypen --------------------------------------------------------
    def werttyp(self, vtct_id):
        """Wertebeschreibung für einen ValueTypeContainerType (nur einfache Typen mit einem Datenfeld)."""
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
                if num.get("unit") == "x 10":  # Schaltzähler: Rohwert × 10
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
        out["kodierung"] = kodierung(out)
        return out

    # --- Parameter ---------------------------------------------------------
    def parameter(self, el, kanal_obj):
        """Parameter-Eintrag aus KnxRfDeviceParameterType (None, wenn ohne Funk-Adresse)."""
        pd = el.find("KnxRfPropertyData")
        if pd is None or pd.get("propertyID") is None:
            return None
        pid = int(pd.get("propertyID"))
        idx = int(pd.get("index") or 0)
        pt_id = el.get("id")
        kurz = re.sub(r"^PT_[A-Z0-9]+[._]", "", pt_id)
        p = {
            "id": kurz,
            "katalog_id": pt_id,
            "name": el.get("name"),
            "obj": int(pd.get("interfaceObjectIndex")) if pd.get("interfaceObjectIndex") else kanal_obj,
            "pid": pid,
            "start": idx + 1 if pid == 79 else idx,
            "anzahl": int(pd.get("elementCount") or 1),
            "lesen": int(el.get("accessLevelRead") or 0),
            "schreiben": int(el.get("accessLevelWrite") or 0),
            "aktiv": el.get("active", "true") != "false",
            "typ": self.werttyp(el.get("valueTypeContainerTypeID")),
        }
        if el.get("writeProtectedAtDevice") == "true":
            p["schreibgeschuetzt"] = True
        return p

    def geraet(self, dvt):
        gdvt_id, hersteller, produkt, artikel, produktname = self.devtypes.get(
            dvt, (dvt.replace("DVT_", "GDVT_"), None, None, None, None))
        g = self.generic.get(gdvt_id)
        if g is None:
            return None
        out = {"name": g.get("name"), "generisch": gdvt_id, "hersteller": hersteller, "produkt": produkt,
               "artikel": artikel, "produktname": produktname,
               "geraet_obj": int(g.get("interfaceObjectIndexUserObjectSystem") or 0), "kanaele": [], "geraet": []}
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
                eintrag = {"nr": nr, "kanaltyp": ct_id, "obj": obj,
                           "go_offset": int(ch.get("groupObjectNumberOffset") or 0), "parameter": params}
                if ct_id == "CT_DEVICE":
                    out["geraet"] = params
                else:
                    if ct is not None:
                        eintrag["alias"] = ct.get("aliasID")
                    out["kanaele"].append(eintrag)
        return out


def kodierung(t):
    """Wie der Wert im Parameterspeicher steht (HA rechnet damit um)."""
    dpt, bits, konv = t.get("dpt", ""), t.get("bits", 0), t.get("konverter", "")
    if konv == "CVT_2_OCTET_FLOAT_VALUE_TO_DOUBLE":
        return "dpt9"
    if dpt.startswith("DPT_5.001") or dpt.startswith("DPT_5.004") and t.get("einheit") == "%":
        return "prozent255" if dpt.startswith("DPT_5.001") else "u8"
    if bits == 8 or t.get("art") in ("bool", "auswahl") and bits <= 8:
        return "u8"
    if bits == 16:
        return "u16_x100" if "RESOLUTION_100" in konv else "u16"
    if bits == 32:
        return "u32"
    return "unbekannt"


def main():
    kat = Katalog(arg("--katalog", "katalog/fw2.21"))
    alle = "--alle" in sys.argv
    aus = arg("--aus", "custom_components/enet_bridge/definitionen.json" if alle else "katalog/definitionen.json")
    typen = arg("--typen")
    if alle:
        typen = sorted(kat.devtypes)
    elif typen:
        typen = typen.split(",")
    else:  # alle Typen der eigenen Anlagen (anlage*.json), sonst alle bekannten
        typen = sorted({g["typ"] for f in glob.glob("anlage*.json")
                        for g in json.load(open(f, encoding="utf-8"))["geraete"]}) or sorted(kat.devtypes)
    defs = {}
    for t in typen:
        d = kat.geraet(t)
        if d is None:
            print(f"{t}: nicht im Katalog", file=sys.stderr)
            continue
        defs[t] = d
        n = sum(len(k["parameter"]) for k in d["kanaele"]) + len(d["geraet"])
        unbek = sum(1 for k in d["kanaele"] for p in k["parameter"] if p["typ"].get("kodierung") in (None, "unbekannt"))
        print(f"{t:<12} {d['name'][:40]:<40} {len(d['kanaele'])} Kanäle, {n} Parameter ({unbek} ohne Kodierung)")
    os.makedirs(os.path.dirname(aus) or ".", exist_ok=True)
    with open(aus, "w", encoding="utf-8") as f:
        json.dump({"quelle": os.path.basename(os.path.abspath(kat.d)), "typen": defs}, f, ensure_ascii=False,
                  separators=(",", ":"), sort_keys=True)
        f.write("\n")
    print(f"→ {aus}")


if __name__ == "__main__":
    main()

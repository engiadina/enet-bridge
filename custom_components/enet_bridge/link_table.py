"""Read and evaluate the actuators' link tables (check links).

Per channel object PID 63, per group object (GO) one entry after another: fp_read `00 00 GO idx` →
`type(1) SN(6) GA(2) GO(2)`; type 01 = factory entry (own SN, one per GO), 00 = created link;
nack rc 255 = end of this GO's list. A GO without a single entry does not exist (factory entry), so a
nack already at entry 0 means: no more GOs.

Evaluation (evaluate() is pure logic without Home Assistant):
- werk     factory entry
- szene    server SN, scene GA → scene GO (SC, 6 for CT_1F01) or status GA → sum status GO (SSGET, 19) of a
           scene this channel is a member of
- wippe    transmitter of the installation, rocker n is a rocker (not single keys) and switches this channel:
           GA 50+3n → GO 1 (GBR), 51+3n → SSGET. GO numbers depend on the channel type (layouts.py). Only the lower GA byte is compared – a transmitter's GA base
           can differ per rocker (2026-11-07: wall transmitter with base 1 and 2, all rockers work).
- geraet   other device of the installation (not a transmitter): not evaluated
- alt      server or transmitter of the installation, but matches no scene / rocker of this channel: leftover
           (2026-11-07: five stale links of a wall transmitter made a dimmer drop to a different value when a
           server key was released)
- fremd    transmitter SN belongs to no device of the installation
Suspicious (repairs) are `alt` and `fremd`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from . import layouts

GO_SCENE, GO_SUMSTATUS_GET, GO_VALUE_IN = 6, 19, 1  # CT_1F01/CT_1F02; other channel types: channel_gos()
MAX_GO = 40      # safety limit; mini actuators have 22
MAX_ENTRIES = 32
SUSPICIOUS = ("alt", "fremd")


@dataclass(frozen=True)
class Link:
    typ: int
    sn: str
    ga: int
    go: int

    @classmethod
    def off_value(cls, v: bytes) -> Link | None:
        if len(v) < 11:
            return None
        return cls(v[0], v[1:7].hex(), int.from_bytes(v[7:9], "big"), int.from_bytes(v[9:11], "big"))

    @property
    def id(self) -> str:
        return f"{self.sn}_{self.ga}_{self.go}"

    def command_(self, op: int) -> bytes:
        """fp_cmd data: op 0 = create, 2 = delete."""
        return bytes([op, 0]) + bytes.fromhex(self.sn) + self.ga.to_bytes(2, "big") + self.go.to_bytes(2, "big")


def channel_gos(installation: dict[str, Any], key: str) -> tuple[int, int, int]:
    """(scene GO, sum status GO, rocker value GO) of channel key ("ia/ch") from its channel type."""
    ia, _, nr = key.partition("/")
    g = next((g for g in installation.get("geraete", []) if g.get("ia") == ia), {})
    ch = next((k for k in g.get("kanaele", []) if str(k.get("nr")) == nr), {"art": "schalten"})
    return (layouts.go(ch, "SC") or GO_SCENE, layouts.go(ch, "SSGET") or GO_SUMSTATUS_GET,
            layouts.go(ch, "GBR") or GO_VALUE_IN)


def _rocker(ga: int, go: int, gos: tuple[int, int, int] = (GO_SCENE, GO_SUMSTATUS_GET, GO_VALUE_IN)) -> int | None:
    """Rocker n for (GA, GO) on the actuator side of a rocker, otherwise None."""
    lo = ga & 0xFF
    if go == gos[2] and lo >= 53 and (lo - 50) % 3 == 0:
        return (lo - 50) // 3
    if go == gos[1] and lo >= 54 and (lo - 51) % 3 == 0:
        return (lo - 51) // 3
    return None


def _rockers(enet_device: dict[str, Any]) -> dict[int, list[str]] | None:
    """Programmed rockers of the transmitter: n → channel keys (single keys: n missing). None = assignment unknown."""
    out: dict[int, list[str]] = {}
    for t in enet_device.get("tasten", []):
        if t.get("art") != "wippe":
            continue
        if "ziele" not in t:
            return None
        out[(t["nr"] + 1) // 2] = [f"{z['ia']}/{z['ch']}" for z in t["ziele"]]
    return out


def evaluate(installation: dict[str, Any], scenes: Iterable[Any], key: str, link: Link) -> tuple[str, dict[str, Any]]:
    """(Kind, details: grund, szene, sender, wippe) of a link-table entry of channel key ("ia/ch").
    scenes: objects or dicts with name, ga, ga_status, mitglieder."""
    if link.typ == 1:
        return "werk", {}
    gos = channel_gos(installation, key)
    if link.sn == installation["server"]["sn"]:
        hits = []
        for s in scenes:
            s = s if isinstance(s, dict) else vars(s)
            if (link.go == gos[0] and link.ga == s["ga"]) or \
                    (link.go == gos[1] and link.ga == s.get("ga_status")):
                if key in s["mitglieder"]:
                    return "szene", {"szene": s["name"]}
                hits.append(s["name"])
        return "alt", ({"grund": "szene_ohne_kanal", "szene": ", ".join(hits)} if hits
                      else {"grund": "keine_szene"})
    g = next((g for g in installation["geraete"] if g["sn"] == link.sn), None)
    if g is None:
        return "fremd", {"grund": "fremd"}
    if g.get("rolle") != "sender":
        return "geraet", {"sender": g["name"]}
    rockers = _rockers(g)
    n = _rocker(link.ga, link.go, gos)
    if rockers is None:
        return "geraet", {"sender": g["name"]}  # assignment unknown: do not evaluate
    if n is not None and key in rockers.get(n, []):
        return "wippe", {"sender": g["name"], "wippe": n}
    if n is not None and n in rockers:
        return "alt", {"grund": "wippe_ohne_kanal", "sender": g["name"], "wippe": n}
    if n is not None:
        return "alt", {"grund": "keine_wippe", "sender": g["name"], "wippe": n}
    return "alt", {"grund": "kein_wippen_link", "sender": g["name"]}


def evaluate_table(installation: dict[str, Any], scenes: Iterable[Any], key: str,
                     links: list[Link]) -> list[dict[str, Any]]:
    scenes = list(scenes)
    out = []
    for link in links:
        art, info = evaluate(installation, scenes, key, link)
        out.append({"sn": link.sn, "ga": link.ga, "go": link.go, "typ": link.typ, "art": art, **info})
    return out

"""Link-Tabellen der Aktoren lesen und bewerten (Verknüpfungen prüfen).

Je Kanal-Objekt PID 63, je Gruppenobjekt (GO) ein Eintrag nach dem anderen: fp_read `00 00 GO idx` →
`Typ(1) SN(6) GA(2) GO(2)`; Typ 01 = Werkseintrag (eigene SN, je GO einer), 00 = angelegte Verknüpfung;
nack rc 255 = Ende der Liste dieses GO. Ein GO ohne einen einzigen Eintrag gibt es nicht (Werkseintrag), ein
nack schon bei Eintrag 0 heißt deshalb: kein GO mehr.

Bewertung (bewerten() ist reine Logik ohne Home Assistant):
- werk     Werkseintrag
- szene    Server-SN, Szenen-GA → GO 6 bzw. Status-GA → GO 19 einer Szene, in der dieser Kanal Mitglied ist
- wippe    Sender der Anlage, Wippe n ist Wippe (keine Einzeltasten) und schaltet diesen Kanal:
           GA 50+3n → GO 1, 51+3n → GO 19. Verglichen wird nur das untere GA-Byte – die GA-Basis eines Senders
           kann je Wippe verschieden sein (2026-11-07: Wandsender mit Basis 1 und 2, alle Wippen gehen).
- geraet   anderes Gerät der Anlage (kein Sender): nicht bewertet
- alt      Server oder Sender der Anlage, passt aber zu keiner Szene / Wippe dieses Kanals: Überbleibsel
           (2026-11-07: fünf Alt-Links eines Wandsenders ließen einen Dimmer beim Loslassen einer Server-Taste
           auf einen anderen Wert fallen)
- fremd    Sender-SN gehört zu keinem Gerät der Anlage
Auffällig (Reparaturen) sind „alt“ und „fremd“.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

GO_SZENE, GO_SAMMELSTATUS, GO_WERT = 6, 19, 1
MAX_GO = 40      # Sicherheitsgrenze; Mini-Aktoren haben 22
MAX_EINTRAEGE = 32
AUFFAELLIG = ("alt", "fremd")


@dataclass(frozen=True)
class Link:
    typ: int
    sn: str
    ga: int
    go: int

    @classmethod
    def aus_wert(cls, v: bytes) -> Link | None:
        if len(v) < 11:
            return None
        return cls(v[0], v[1:7].hex(), int.from_bytes(v[7:9], "big"), int.from_bytes(v[9:11], "big"))

    @property
    def id(self) -> str:
        return f"{self.sn}_{self.ga}_{self.go}"

    def befehl(self, op: int) -> bytes:
        """fp_cmd-Daten: op 0 = anlegen, 2 = löschen."""
        return bytes([op, 0]) + bytes.fromhex(self.sn) + self.ga.to_bytes(2, "big") + self.go.to_bytes(2, "big")


def _wippe(ga: int, go: int) -> int | None:
    """Wippe n zu (GA, GO) auf der Aktor-Seite einer Wippe, sonst None."""
    lo = ga & 0xFF
    if go == GO_WERT and lo >= 53 and (lo - 50) % 3 == 0:
        return (lo - 50) // 3
    if go == GO_SAMMELSTATUS and lo >= 54 and (lo - 51) % 3 == 0:
        return (lo - 51) // 3
    return None


def _wippen(geraet: dict[str, Any]) -> dict[int, list[str]] | None:
    """Programmierte Wippen des Senders: n → Kanal-Keys (Einzeltasten: n fehlt). None = Belegung unbekannt."""
    out: dict[int, list[str]] = {}
    for t in geraet.get("tasten", []):
        if t.get("art") != "wippe":
            continue
        if "ziele" not in t:
            return None
        out[(t["nr"] + 1) // 2] = [f"{z['ia']}/{z['ch']}" for z in t["ziele"]]
    return out


def bewerten(anlage: dict[str, Any], szenen: Iterable[Any], key: str, link: Link) -> tuple[str, dict[str, Any]]:
    """(Art, Einzelheiten: grund, szene, sender, wippe) eines Eintrags der Link-Tabelle von Kanal key („ia/ch“). szenen: Objekte oder dicts
    mit name, ga, ga_status, mitglieder."""
    if link.typ == 1:
        return "werk", {}
    if link.sn == anlage["server"]["sn"]:
        treffer = []
        for s in szenen:
            s = s if isinstance(s, dict) else vars(s)
            if (link.go == GO_SZENE and link.ga == s["ga"]) or \
                    (link.go == GO_SAMMELSTATUS and link.ga == s.get("ga_status")):
                if key in s["mitglieder"]:
                    return "szene", {"szene": s["name"]}
                treffer.append(s["name"])
        return "alt", ({"grund": "szene_ohne_kanal", "szene": ", ".join(treffer)} if treffer
                      else {"grund": "keine_szene"})
    g = next((g for g in anlage["geraete"] if g["sn"] == link.sn), None)
    if g is None:
        return "fremd", {"grund": "fremd"}
    if g.get("rolle") != "sender":
        return "geraet", {"sender": g["name"]}
    wippen = _wippen(g)
    n = _wippe(link.ga, link.go)
    if wippen is None:
        return "geraet", {"sender": g["name"]}  # Belegung nicht bekannt: nicht bewerten
    if n is not None and key in wippen.get(n, []):
        return "wippe", {"sender": g["name"], "wippe": n}
    if n is not None and n in wippen:
        return "alt", {"grund": "wippe_ohne_kanal", "sender": g["name"], "wippe": n}
    if n is not None:
        return "alt", {"grund": "keine_wippe", "sender": g["name"], "wippe": n}
    return "alt", {"grund": "kein_wippen_link", "sender": g["name"]}


def tabelle_bewerten(anlage: dict[str, Any], szenen: Iterable[Any], key: str,
                     links: list[Link]) -> list[dict[str, Any]]:
    szenen = list(szenen)
    out = []
    for link in links:
        art, info = bewerten(anlage, szenen, key, link)
        out.append({"sn": link.sn, "ga": link.ga, "go": link.go, "typ": link.typ, "art": art, **info})
    return out

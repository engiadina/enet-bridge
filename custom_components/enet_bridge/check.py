"""Installation check: differences between the imported installation and the actual installation as repair
issues (Settings → Repairs). Only reads; repairs happen only after confirmation (repairs.py).

- Actuator does not respond (after the sync no channel with a known state) – disappears as soon as it reports.
- eNet scenes not (yet) converted into HA scenes (values not readable, or "Listen only" is on:
  then nothing is read at all, the conversion follows after switching it off).
- Scene links (GA → GO 6 recall, GA+1 → GO 19 sum status) are missing in members of an HA scene.
- Key presses of a transmitter that is not in the installation.
- Suspicious entries in the actuators' link tables (link_table.py): stale links from the server or transmitters and
  links of foreign devices. Read once per actuator (afterwards reported from the installation data) and on request
  (service `verknuepfungen_lesen`, repair dialog); removed or kept only after confirmation.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from . import link_table as lt
from .bridge import BridgeError
from .const import DOMAIN
from .scenes import GO_SUMSTATUS_GET, GO_SCENE, PID_LINKS
from .texts import tr

if TYPE_CHECKING:
    from .hub import Hub, Channel

_LOGGER = logging.getLogger(__name__)

MAX_LINKS = 24  # maximum entries read per GO (end of table = nack rc 255)


class Check:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.hass = hub.hass
        self.missing_links: dict[str, list[tuple[int, int]]] = {}  # channel → [(GA, GO)]
        self._unknown: set[str] = set()
        # up to 0.12.11 the issue ID contained the serial number in plain text – and HA attaches the IDs to every
        # diagnostics download
        reg = ir.async_get(self.hass)
        for domain, issue_id in list(reg.issues):
            if domain == DOMAIN and re.search(r"_[0-9a-f]{12}$", issue_id):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @staticmethod
    def _id(prefix: str, sn: str) -> str:
        """Issue ID without the serial number in plain text (the IDs appear in "Download diagnostics")."""
        return f"{prefix}_{hashlib.sha256(sn.encode()).hexdigest()[:8]}"

    def _report_state(self, issue_id: str, key: str, placeholder: dict[str, str], fixable: bool = False,
                data_: dict | None = None) -> None:
        ir.async_create_issue(self.hass, DOMAIN, issue_id, is_fixable=fixable, is_persistent=False,
                              severity=ir.IssueSeverity.WARNING, translation_key=key,
                              translation_placeholders=placeholder,
                              data={"entry_id": self.hub.entry_id, **(data_ or {})})

    def _done(self, issue_id: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # --- Devices ----------------------------------------------------------------
    @callback
    def check_devices(self) -> None:
        """After syncing with the bridge: actuators without a single known channel."""
        for g in self.hub.installation["geraete"]:
            channels_ = [k for k in self.hub.channels_ if k.enet_device is g]
            if not channels_:
                continue  # transmitters sleep and cannot be queried
            if any(self.hub.state_[k.key].known for k in channels_):
                self._done(self._id("antwortet_nicht", g["sn"]))
            else:
                self._report_state(self._id("antwortet_nicht", g["sn"]), "antwortet_nicht",
                             {"name": g["name"], "ia": g["ia"], "sn": g["sn"]}, fixable=True, data_={"sn": g["sn"]})

    @callback
    def answers(self, ia: str) -> None:
        g = next((g for g in self.hub.installation["geraete"] if g["ia"] == ia), None)
        if g is not None:
            self._done(self._id("antwortet_nicht", g["sn"]))

    @callback
    def unknown_sender(self, sn: str) -> None:
        if not sn or sn in self._unknown or any(g["sn"] == sn for g in self.hub.installation["geraete"]):
            return
        self._unknown.add(sn)
        self._report_state(self._id("unbekannter_sender", sn), "unbekannter_sender", {"sn": sn})

    # --- Scenes ----------------------------------------------------------------
    @callback
    def check_scenes(self) -> None:
        open_items = [s.name for s in self.hub.scenes.scenes if s.art != "ha"]
        key = "szenen_mithoeren" if self.hub.listen_only else "szenen_nicht_umgewandelt"
        for k in ("szenen_mithoeren", "szenen_nicht_umgewandelt"):
            if k != key or not open_items:
                self._done(k)
        if open_items:
            self._report_state(key, key, {"szenen": ", ".join(open_items)})

    async def _await_sender(self) -> None:
        """The checks only read and are not in a hurry: pause while a transmitter is being programmed. The bridge
        processes requests one after another – otherwise slow actuators (up to 3 s per link entry) delayed every
        command to the transmitter (programming right after HA start took minutes)."""
        while self.hub.senders_active:
            await asyncio.sleep(1)

    async def _links(self, k: Channel, go: int) -> set[int] | None:
        """GAs under the server SN that are entered in GO go of this channel (None: not readable)."""
        sn = self.hub.installation["server"]["sn"]
        gas: set[int] = set()
        for idx in range(MAX_LINKS):
            for attempt in range(3):
                await self._await_sender()
                try:
                    r = await self.hub.bridge.request("fp_read", ia=k.ia, obj=k.obj, pid=PID_LINKS,
                                                      data=f"0000{go:02x}{idx:02x}")
                    break
                except BridgeError as err:
                    if err.rc == 255:  # end of table
                        return gas
                    if attempt == 2:
                        return None
            v = bytes.fromhex(r.get("value", ""))
            if len(v) >= 9 and v[1:7].hex() == sn:
                gas.add(int.from_bytes(v[7:9], "big"))
        return gas

    async def check_links(self) -> None:
        """Read the scene links of all members of HA scenes and report missing ones."""
        missing = await self._missing_links()
        self._report_missing(missing)

    async def _missing_links(self, ia: str | None = None) -> dict[str, list[tuple[int, int]]]:
        """Missing scene links per channel (ia: only this device)."""
        channels_ = {k.key: k for k in self.hub.channels_}
        needed: dict[str, set[tuple[int, int]]] = {}
        for s in self.hub.scenes.scenes:
            if s.art != "ha":
                continue
            for key in s.members:
                if ia is not None and not key.startswith(f"{ia}/"):
                    continue
                if key in channels_ and key not in s.open_items:
                    needed.setdefault(key, set()).add((s.ga, GO_SCENE))
                    if s.ga_status is not None:
                        needed[key].add((s.ga_status, GO_SUMSTATUS_GET))
        missing: dict[str, list[tuple[int, int]]] = {}
        for key, wanted in needed.items():
            k = channels_[key]
            for go in (GO_SCENE, GO_SUMSTATUS_GET):
                present = await self._links(k, go)
                if present is None:
                    continue  # not reachable: reported as "does not respond"
                missing_ = [(ga, g) for ga, g in wanted if g == go and ga not in present]
                if missing_:
                    missing.setdefault(key, []).extend(missing_)
        return missing

    def _report_missing(self, missing: dict[str, list[tuple[int, int]]]) -> None:
        channels_ = {k.key: k for k in self.hub.channels_}
        self.missing_links = missing
        if missing:
            names_ = sorted({f"{channels_[key].enet_device['name']} K{channels_[key].nr}" for key in missing})
            scenes = sorted({s.name for s in self.hub.scenes.scenes for key, links in missing.items()
                             if key in s.members and any(ga in (s.ga, s.ga_status) for ga, _ in links)})
            self._report_state("szenen_links_fehlen", "szenen_links_fehlen",
                         {"kanaele": ", ".join(names_), "szenen": ", ".join(scenes)}, fixable=True)
        else:
            self._done("szenen_links_fehlen")

    async def repair_links(self) -> list[str]:
        """Create missing scene links (programming frame; states are restored afterwards)."""
        channels_ = {k.key: k for k in self.hub.channels_}
        sn = self.hub.installation["server"]["sn"]
        errors = []
        for key, links in self.missing_links.items():
            k = channels_.get(key)
            if k is None:
                continue

            async def steps(k=k, links=links) -> None:
                for ga, go in links:
                    data = bytes([0, 0]) + bytes.fromhex(sn) + ga.to_bytes(2, "big") + go.to_bytes(2, "big")
                    await self.hub.bridge.request("fp_cmd", ia=k.ia, obj=k.obj, pid=PID_LINKS, data=data.hex())

            try:
                await self.hub.params.program(k.ia, self.hub.device_obj(k.enet_device), k.obj, steps,
                                                    purpose=tr("zweck_szenen_links"))
            except BridgeError as err:
                errors.append(f"{k.enet_device['name']} K{k.nr}: {err}")
        # if links were missing, the device was usually reset – then its scene values are missing too
        for ia in sorted({channels_[key].ia for key in self.missing_links if key in channels_}):
            errors += await self.hub.scenes.restore_values(ia)
        await self.check_links()
        return errors

    async def restore_actuator(self, ia: str) -> list[str]:
        """Write scenes back into a re-paired actuator (factory reset): missing links, then values."""
        missing = await self._missing_links(ia)
        channels_ = {k.key: k for k in self.hub.channels_}
        errors = []
        for key, links in missing.items():
            k = channels_[key]

            async def steps(k=k, links=links) -> None:
                sn = self.hub.installation["server"]["sn"]
                for ga, go in links:
                    data = bytes([0, 0]) + bytes.fromhex(sn) + ga.to_bytes(2, "big") + go.to_bytes(2, "big")
                    await self.hub.bridge.request("fp_cmd", ia=k.ia, obj=k.obj, pid=PID_LINKS, data=data.hex())

            try:
                await self.hub.params.program(k.ia, self.hub.device_obj(k.enet_device), k.obj, steps,
                                                    purpose=tr("zweck_szenen_links"))
            except BridgeError as err:
                errors.append(f"{k.enet_device['name']} K{k.nr}: {err}")
        errors += await self.hub.scenes.restore_values(ia)
        return errors

    # --- Link tables (links) -------------------------------------------
    async def read_table(self, k: Channel) -> list[lt.Link]:
        """Entire link table of a channel; BridgeError if it cannot be read completely."""
        links: list[lt.Link] = []
        for go in range(1, lt.MAX_GO + 1):
            for idx in range(lt.MAX_ENTRIES):
                for attempt in range(3):
                    await self._await_sender()
                    try:
                        r = await self.hub.bridge.request("fp_read", ia=k.ia, obj=k.obj, pid=PID_LINKS,
                                                          data=f"0000{go:02x}{idx:02x}")
                        break
                    except BridgeError as err:
                        if err.err == "nack":  # rc 255 = end of list; at entry 0: no more GOs
                            if idx == 0 and go == 1:  # GO 1 always has a factory entry
                                raise BridgeError(err.err, tr("linktabelle_unlesbar", key=k.key, err=err)) from err
                            if idx == 0:
                                return links
                            r = None
                            break
                        if attempt == 2:
                            raise BridgeError(err.err, tr("linktabelle_unlesbar", key=k.key, err=err)) from err
                if r is None:
                    break
                if (link := lt.Link.off_value(bytes.fromhex(r.get("value", "")))) is not None:
                    links.append(link)
        return links

    async def read_device_links(self, g: dict) -> dict[int, list[dict]]:
        """Read and evaluate all channels of an actuator, store the result (suspicious entries) in the installation
        and report it as a repair. Channel → evaluated entries."""
        outcome: dict[int, list[dict]] = {}
        for k in [k for k in self.hub.channels_ if k.enet_device is g]:
            outcome[k.nr] = lt.evaluate_table(self.hub.installation, self.hub.scenes.scenes, k.key,
                                                 await self.read_table(k))
        keep = set(g.get("links_behalten", []))
        g["links"] = {"geprueft": dt_util.now().isoformat(timespec="seconds"),
                      "auffaellig": [{"ch": ch, **e} for ch, ee in outcome.items() for e in ee
                                     if e["art"] in lt.SUSPICIOUS and _link_id(ch, e) not in keep]}
        await self.hub.save_installation()
        self._report_links(g)
        return outcome

    @callback
    def _report_links(self, g: dict) -> None:
        auff = g.get("links", {}).get("auffaellig", [])
        if not auff:
            self._done(self._id("links_auffaellig", g["sn"]))
            return
        rows = [tr("link_zeile", ch=e["ch"], go=e["go"], ga=e["ga"], grund=tr(f"link_{e['grund']}", **e))
                  for e in auff]
        self._report_state(self._id("links_auffaellig", g["sn"]), "links_auffaellig",
                     {"name": g["name"], "ia": g["ia"], "eintraege": "\n".join(f"- {z}" for z in rows)},
                     fixable=True, data_={"sn": g["sn"]})

    async def check_links_tables(self) -> None:
        """After the sync: read actuators never read before, report the stored result for the others."""
        for g in self.hub.installation["geraete"]:
            if not any(k.enet_device is g for k in self.hub.channels_):
                continue
            if g.get("links", {}).get("geprueft"):
                self._report_links(g)
                continue
            try:
                await self.read_device_links(g)
            except BridgeError as err:
                _LOGGER.info("%s: link table not checked (%s), next start again", g["name"], err)

    async def remove_links(self, sn: str) -> list[str]:
        """Freshly read and delete an actuator's suspicious entries (programming frame; states are restored
        afterwards), then read again to verify. Returns error texts."""
        g = next(g for g in self.hub.installation["geraete"] if g["sn"] == sn)
        await self.read_device_links(g)
        errors = []
        for k in [k for k in self.hub.channels_ if k.enet_device is g]:
            gone = [lt.Link(e["typ"], e["sn"], e["ga"], e["go"]) for e in g["links"]["auffaellig"] if e["ch"] == k.nr]
            if not gone:
                continue

            async def steps(k=k, gone=gone) -> None:
                for link in gone:
                    await self.hub.bridge.request("fp_cmd", ia=k.ia, obj=k.obj, pid=PID_LINKS,
                                                  data=link.command_(2).hex())

            _LOGGER.info("%s: deleting links %s", k.key, [(x.sn, x.ga, x.go) for x in gone])
            try:
                await self.hub.params.program(k.ia, self.hub.device_obj(g), k.obj, steps,
                                                    purpose=tr("zweck_alt_links"))
            except BridgeError as err:
                errors.append(f"{g['name']} K{k.nr}: {err}")
        await self.read_device_links(g)
        if not errors and g["links"]["auffaellig"]:
            errors.append(tr("links_noch_da", name=g["name"], n=len(g["links"]["auffaellig"])))
        return errors

    async def keep_links(self, sn: str) -> None:
        """The reported entries of this actuator count as intended and are no longer reported."""
        g = next(g for g in self.hub.installation["geraete"] if g["sn"] == sn)
        auff = g.get("links", {}).get("auffaellig", [])
        g["links_behalten"] = sorted(set(g.get("links_behalten", [])) | {_link_id(e["ch"], e) for e in auff})
        g.setdefault("links", {})["auffaellig"] = []
        await self.hub.save_installation()
        self._report_links(g)


def _link_id(ch: int, e: dict) -> str:
    return f"{ch}_{e['sn']}_{e['ga']}_{e['go']}"

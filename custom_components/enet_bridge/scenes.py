"""eNet scenes = KNX scenes stored in the actuators: manage, call, learn, create, delete.

A scene is registered in every member (actuator channel) via two links under the server SN:
scene GA → GO 6 (call, 1 byte nr., + 0x80 = learn current state) and status GA → GO 19
(sum status). The scene values live in the actuator (SCV1[nr]); they are set with a learn command.
Room scenes "lights on/off" share one GA (different numbers) – only delete links when no other
scene of the member still uses them. Confirmed 2026-10-28 (NOTES.md, INTERFACE.md).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from homeassistant.helpers.storage import Store

from .bridge import BridgeError
from .const import DOMAIN
from .texts import tr

if TYPE_CHECKING:
    from .hub import Hub, Channel

_LOGGER = logging.getLogger(__name__)

PID_LINKS = 63
GO_SCENE, GO_SUMSTATUS_GET = 6, 19
# SCV1[0…37]; 16/17 = central key "all on/off" (FT_INAOOS), 18/19 inactive ex works (SNA = 0, meaning unknown)
NR_FREE = [n for n in range(38) if n not in (16, 17, 18, 19)]
SCV_START = 4            # SCV1[nr] in the channel object, PID 79 (switch 00/FF, dimmer 0–255 = 0–100 %)
VALUE_TOLERANCE = 4        # learned value vs. scene value (byte; 1 % ≈ 2.55)
LOCK_S = 4.0           # for this long after a scene call: do not send matching single commands from HA
GA_FREE_FROM = 100         # own scene GAs (pairs GA, GA+1), above those assigned by the server
STATUS_AFTER_S = 5.0      # poll the members' status after a scene call (wait for the dimming ramp)


def value_equal(a: int | None, b: int | None) -> bool:
    """Scene values equal except for rounding (percent ↔ byte, learned values)."""
    return a is not None and b is not None and abs(a - b) <= VALUE_TOLERANCE


@dataclass
class SceneDef:
    name: str
    nr: int
    ga: int
    ga_status: int | None
    members: list[str] = field(default_factory=list)  # channel keys "IA/channel"
    art: str = "eigen"  # eigen | raum (room scene of the eNet Server) | ha (eNet part of an HA scene)
    ha_id: str | None = None  # HA scene (scenes.yaml "id") whose eNet part this scene is
    values_: dict[str, int] = field(default_factory=dict)  # values written to SCV1[nr] per member
    open_items: list[str] = field(default_factory=list)  # members whose reconciliation failed

    @property
    def id(self) -> str:
        return f"{self.ga}_{self.nr}"


# Stored keys stay German (existing installations): field name -> key in the store
STORE_KEYS = {"members": "mitglieder", "values_": "werte", "open_items": "offen"}


def scene_to_store(s: SceneDef) -> dict:
    return {STORE_KEYS.get(k, k): v for k, v in asdict(s).items()}


def scene_from_store(d: dict) -> SceneDef:
    back = {v: k for k, v in STORE_KEYS.items()}
    return SceneDef(**{back.get(k, k): v for k, v in d.items()})


class SceneManager:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._store: Store = Store(hub.hass, 1, f"{DOMAIN}_szenen_{hub.entry_id}")
        self.scenes: list[SceneDef] = []
        self.lock: dict[str, tuple[int, float]] = {}  # channel → (scene value, until) after a scene call
        self.last_call: dict[str, float] = {}  # channel → time of the last scene call involving this channel

    async def laden(self) -> None:
        data_ = await self._store.async_load()
        if data_ is None:  # first start: scenes from the export (anlage.json, tools/iex.py)
            data_ = [{"name": s["name"], "nr": s["nr"], "ga": s["ga"], "ga_status": s.get("ga_status"),
                      "mitglieder": [f"{m['ia']}/{m['ch']}" for m in s.get("mitglieder", [])],
                      "art": s.get("art", "eigen")} for s in self.hub.installation.get("szenen", [])]
            self.scenes = [scene_from_store(d) for d in data_]
            await self._save()
        else:
            self.scenes = [scene_from_store(d) for d in data_]

    async def _save(self) -> None:
        await self._store.async_save([scene_to_store(s) for s in self.scenes])

    def get(self, scene_id: str) -> SceneDef | None:
        return next((s for s in self.scenes if s.id == scene_id), None)

    # --- Execute ------------------------------------------------------------
    async def call_scene(self, s: SceneDef) -> None:
        await self.hub.bridge.request("scene_call", ga=s.ga, nr=s.nr)
        self.poll_status(s)

    def suppress(self, s: SceneDef) -> None:
        """Do not send the single commands from HA that follow right after (an HA scene switches device by device)."""
        bis = time.monotonic() + LOCK_S
        for key in s.members:
            if key in s.values_ and key not in s.open_items:
                self.lock[key] = (s.values_[key], bis)

    def suppressed(self, key: str, on_: bool, percent_: int | None) -> bool:
        """Has this command already been done by the scene call? (consumes the lock)"""
        value, bis = self.lock.get(key, (None, 0.0))
        if value is None or time.monotonic() > bis:
            return False
        _LOGGER.debug("Lock %s: scene value %s, command on=%s %s %%", key, value, on_, percent_)
        if on_ != (value > 0) or (on_ and percent_ is not None and abs(percent_ - round(value * 100 / 255)) > 1):
            return False
        del self.lock[key]
        _LOGGER.debug("Single command %s (on=%s, %s %%) suppressed – done by the scene", key, on_, percent_)
        return True

    async def learn(self, s: SceneDef) -> None:
        """Store the current state of all members as scene values."""
        await self.hub.bridge.request("scene_call", ga=s.ga, nr=s.nr, learn=True)

    def poll_status(self, s: SceneDef) -> None:
        """After the call all members report their status at the same time – the telegrams collide.
        So, like the eNet Server (functionsToPollStatusAfterCallScene), poll after the ramp – but only members that
        have not reported on their own since the call: every poll occupies the radio, and while the bridge is
        sending it hears nothing (2026-11-09: a key press shortly after a scene was lost twice)."""
        call = time.monotonic()
        for key in s.members:
            self.last_call[key] = call

        async def ask() -> None:
            await asyncio.sleep(STATUS_AFTER_S)
            for k in self.hub.channels_:
                if k.key in s.members and self.hub.state_[k.key].reported <= call:
                    try:
                        await self.hub.bridge.request("poll", ia=k.ia, ch=k.nr)
                    except BridgeError as err:
                        _LOGGER.debug("Status after scene %s, %s: %s", s.name, k.key, err)
            if s.art == "ha":
                await self._correct_later(s, call)

        self.hub.hass.async_create_background_task(ask(), f"enet status after scene {s.name}")

    async def _correct_later(self, s: SceneDef, call: float) -> None:
        """The scene telegram is unacknowledged: switch lamps that did not get it individually (the lock had suppressed
        their single command). It is only lost if the actuator has not reported on its own since the call – otherwise
        it executed the scene or was deliberately switched differently afterwards (editor, key, dashboard;
        2026-10-30: the correction had reverted such changes). Also not if the next scene with this channel has
        already come (2026-11-02: the correction of "room off" switched the just-called "room on" off again – the
        status poll does not count as a report of its own)."""
        for k in self.hub.channels_:
            if k.key not in s.values_ or k.key in s.open_items or self.hub.state_[k.key].reported > call \
                    or self.last_call.get(k.key, 0.0) > call:
                continue
            z, wanted = self.hub.state_[k.key], s.values_[k.key]
            percent_ = round(wanted * 100 / 255)
            _LOGGER.debug("After scene %s: %s is %s/%s %%, expected %s", s.name, k.key, z.on, z.percent, wanted)
            if self._stands_on(k, wanted):
                continue
            _LOGGER.info("Scene %s: %s not at scene value – switched individually", s.name, k.key)
            try:
                if wanted and k.art == "dimmen":
                    await self.hub.bridge.request("dim", ia=k.ia, ch=k.nr, percent=max(1, percent_))
                else:
                    await self.hub.bridge.request("switch", ia=k.ia, ch=k.nr, on=wanted > 0)
            except BridgeError as err:
                _LOGGER.warning("Scene %s, %s: %s", s.name, k.key, err)

    # --- eNet part of an HA scene -----------------------------------------
    def _nr_free(self, keys: set[str], except_: SceneDef | None = None) -> int:
        """Lowest scene number not used by another scene in any of these channels."""
        used = {x.nr for x in self.scenes if x is not except_ and keys & set(x.members)}
        nr = next((n for n in NR_FREE if n not in used), None)
        if nr is None:
            raise BridgeError("full", tr("keine_szenennummer"))
        return nr

    def _stands_on(self, k: Channel, value: int) -> bool:
        """Is the channel (according to its last report) at this scene value?"""
        z = self.hub.state_[k.key]
        if not z.known or z.on != (value > 0):
            return False
        return not z.on or k.art != "dimmen" or abs(z.percent - round(value * 100 / 255)) <= 1

    def ha_scene(self, ha_id: str) -> SceneDef | None:
        return next((s for s in self.scenes if s.ha_id == ha_id), None)

    async def reconcile_ha(self, ha_id: str, name: str, wanted: dict[str, int]) -> list[str]:
        """Bring the eNet part of an HA scene into the actuators: links, scene number, values. Returns error texts."""
        s = self.ha_scene(ha_id)
        if s is None:
            nr = self._nr_free(set(wanted))
            ga = self._ga_free()
            s = SceneDef(name, nr, ga, ga + 1, [], art="ha", ha_id=ha_id)
            self.scenes.append(s)
        s.name = name
        if s.nr in {x.nr for x in self.scenes if x is not s and set(wanted) & set(x.members)}:
            # number already used in a (new) member – or shared as with the eNet Server's room scenes
            # (all rooms "lights on" = nr. 16): own number, otherwise the value would also change the other scenes
            s.nr, s.values_ = self._nr_free(set(wanted), except_=s), {}
        errors, open_items = [], []
        channels_ = {k.key: k for k in self.hub.channels_}
        for key in [m for m in s.members if m not in wanted]:
            try:
                if key in channels_:
                    await self._links(channels_[key], s, create=False)
                s.members.remove(key)
                s.values_.pop(key, None)
            except BridgeError as err:
                errors.append(f"{key} entfernen: {err}")
                open_items.append(key)
        for key in wanted:
            k = channels_.get(key)
            if k is None or key in s.members:
                continue
            try:
                await self._links(k, s, create=True)
                s.members.append(key)
                await self._save()
            except BridgeError as err:
                errors.append(f"{k.enet_device['name']} K{k.nr}: {err}")
                open_items.append(key)
        change = {key: w for key, w in wanted.items() if key in s.members and not value_equal(s.values_.get(key), w)}
        if change and all(self._stands_on(channels_[key], w) for key, w in wanted.items() if key in s.members):
            # lamps are already at the scene (the scene editor sets them live): learn instead of a programming frame –
            # one telegram, no actuator restart, the lamps stay on
            try:
                await self.hub.bridge.request("scene_call", ga=s.ga, nr=s.nr, learn=True)
                await asyncio.sleep(1)
                for key, w in list(change.items()):
                    k = channels_[key]
                    r = await self.hub.bridge.request("prop_read", ia=k.ia, obj=k.obj, pid=79,
                                                      start=SCV_START + s.nr, count=1)
                    read_values = bytes.fromhex(r.get("value", "") or "00")[0]
                    if abs(read_values - w) <= VALUE_TOLERANCE:
                        s.values_[key] = w
                        del change[key]
                    else:
                        _LOGGER.debug("Scene %s, %s: learned %s instead of %s – writing", s.name, key, read_values, w)
            except BridgeError as err:
                _LOGGER.debug("Scene %s: learning failed (%s) – writing values", s.name, err)
        for key, w in change.items():
            k = channels_[key]
            try:
                await self.hub.params.write_raw(k.ia, self.hub.device_obj(k.enet_device), k.obj,
                                                    SCV_START + s.nr, bytes([w]), purpose=tr("zweck_szenenwert", name=s.name))
                s.values_[key] = w
            except BridgeError as err:
                errors.append(f"{k.enet_device['name']} K{k.nr}: {err}")
                open_items.append(key)
            await self._save()  # after every channel: state is correct even if aborted
        s.open_items = open_items
        await self._save()
        return errors

    # --- Create / delete -------------------------------------------------
    def _ga_free(self) -> int:
        gas = {g for s in self.scenes for g in (s.ga, s.ga_status) if g is not None}
        gas |= {t["antwort_ga"] for g in self.hub.installation["geraete"] for t in g.get("tasten", []) if t.get("antwort_ga")}
        ga = GA_FREE_FROM
        while ga in gas or ga + 1 in gas:
            ga += 2
        return ga

    def _free(self) -> tuple[int, int]:
        nrs = {s.nr for s in self.scenes}
        gas = {g for s in self.scenes for g in (s.ga, s.ga_status) if g is not None}
        nr = next((n for n in NR_FREE if n not in nrs), None)
        if nr is None:
            raise BridgeError("full", tr("szenenspeicher_voll"))
        ga = GA_FREE_FROM
        while ga in gas or ga + 1 in gas:
            ga += 2
        return nr, ga

    async def create(self, name: str, channels_: list[Channel]) -> SceneDef:
        """New scene: create links in all members. Take over the values afterwards via learn()."""
        if not channels_:
            raise BridgeError("bad_args", tr("keine_mitglieder"))
        nr, ga = self._free()
        s = SceneDef(name, nr, ga, ga + 1, [k.key for k in channels_])
        done: list[Channel] = []
        try:
            for k in channels_:
                await self._links(k, s, create=True)
                done.append(k)
        except BridgeError:
            for k in done:  # remove the half-created scene again
                try:
                    await self._links(k, s, create=False)
                except BridgeError as err:
                    _LOGGER.warning("%s: link not removed: %s", k.key, err)
            raise
        self.scenes.append(s)
        await self._save()
        return s

    async def delete(self, s: SceneDef) -> list[str]:
        """Delete the links in all members and remove the scene. Returns unreachable members."""
        open_items = []
        for key in s.members:
            k = next((k for k in self.hub.channels_ if k.key == key), None)
            if k is None:
                open_items.append(key)  # not in the installation (e.g. removed device)
                continue
            try:
                await self._links(k, s, create=False)
            except BridgeError as err:
                _LOGGER.warning("%s: scene %s not deleted: %s", key, s.name, err)
                open_items.append(key)
        self.scenes.remove(s)
        await self._save()
        return open_items

    async def remove_member(self, ia: str) -> None:
        """Remove a device from all scenes (after removing the device; its links were deleted with it)."""
        for s in self.scenes:
            s.members = [m for m in s.members if not m.startswith(f"{ia}/")]
        await self._save()

    async def restore_values(self, ia: str) -> list[str]:
        """Write an actuator's scene values back from the scene store where the device differs – after a factory reset
        (re-pairing) or after repairing missing scene links: the actuator stores the values itself, the call is only
        a group telegram. Returns error texts."""
        channels_ = {k.key: k for k in self.hub.channels_}
        errors = []
        for s in self.scenes:
            for key in s.members:
                w = s.values_.get(key)
                if not key.startswith(f"{ia}/") or key not in channels_ or w is None:
                    continue
                k = channels_[key]
                try:
                    r = await self.hub.bridge.request("prop_read", ia=k.ia, obj=k.obj, pid=79,
                                                      start=SCV_START + s.nr, count=1)
                    read_values = bytes.fromhex(r.get("value", "") or "00")[0]
                    if value_equal(read_values, w):
                        continue
                    _LOGGER.warning("Scene %s, %s: device has value %s instead of %s – writing it",
                                    s.name, key, read_values, w)
                    await self.hub.params.write_raw(k.ia, self.hub.device_obj(k.enet_device), k.obj,
                                                        SCV_START + s.nr, bytes([w]),
                                                        purpose=tr("zweck_szenenwert", name=s.name))
                except BridgeError as err:
                    errors.append(f"{s.name}, {k.enet_device['name']} K{k.nr}: {err}")
        return errors

    async def _links(self, k: Channel, s: SceneDef, create: bool) -> None:
        """Create or delete both links of the scene in a channel (inside the programming frame)."""
        others = [x for x in self.scenes if x is not s and k.key in x.members]
        links = [(s.ga, GO_SCENE), (s.ga_status, GO_SUMSTATUS_GET)]
        if not create:  # keep GAs that another scene of this channel still uses
            links = [(ga, go) for ga, go in links if not any(ga in (x.ga, x.ga_status) for x in others)]
        links = [(ga, go) for ga, go in links if ga is not None]
        if not links:
            return
        sn = self.hub.installation["server"]["sn"]
        ia = k.ia

        async def steps() -> None:
            for ga, go in links:
                data = bytes([0 if create else 2, 0]) + bytes.fromhex(sn) + ga.to_bytes(2, "big") \
                    + go.to_bytes(2, "big")
                await self.hub.bridge.request("fp_cmd", ia=ia, obj=k.obj, pid=PID_LINKS, data=data.hex())

        await self.hub.params.program(ia, self.hub.device_obj(k.enet_device), k.obj, steps,
                                            purpose=tr("zweck_szenen_links"))

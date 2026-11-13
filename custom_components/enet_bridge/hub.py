"""Installation (device directory) + bridge: state table, key mapping, resync after ESP start.

Commissioning without input: on connect, HA compares the bridge's server configuration (CRC) with
anlage.json and transfers it if they differ. A bridge that is not yet calibrated (or one where no
actuator answers) searches for its frequency correction via status requests and stores it in NVS.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.logbook import async_log_entry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import esp_config, firmware, layouts
from .bridge import Bridge, BridgeError, foreign_radio
from .const import KIND_DIMMER, KIND_BLIND, KIND_SWITCH, KIND_KEY, KIND_ROCKER, DOMAIN, SENSOR_TYPES
from .installation import InstallationStore
from .params import Param, ParamStore, dpt9_decode
from .ha_scenes import HaScenes
from .ha_rocker import HaRocker
from .measurements import UNTESTED, Measurements, is_measuring_device
from .check import Check
from .scenes import SceneManager
from .texts import german, tr

_LOGGER = logging.getLogger(__name__)
BATTERY_WARNING_S = 3600  # interval between "Battery low" log warnings per device

OSV_HOLD_MS = 4000  # both rocker keys held this long: dimmer stores the switch-on brightness
SYNC_DELAY_S = 3.0  # after opening: ESP restarts under Linux, wait for the start event
REPEAT_S = 2.0  # same key telegram (SN, GA, key value) within this time: repetition
REAL_SERVER_S = 300  # "real server active" for this long after its last telegram (bridge reports ≤ 1×/min)
HELLO_ATTEMPTS = 3  # hello attempts before the port counts as "no bridge"

# Frequency correction (FSCTRL0, ~1.59 kHz per step): search order if no actuator answers.
# A spacing of 30 (~48 kHz) lies within the capture range; 60 = boards/modules built so far.
CAL_CANDIDATES = (60, 0, 30, -30, 90, -60, 120, -90, -120)
CAL_SAMPLES = 3       # fine tuning: at least this many averaged telegrams
CAL_TEST_CHANNELS = 2  # coarse search: query at most this many devices per candidate
CAL_PASSIVE_S = 300   # "listen only": wait this long per frequency correction for foreign telegrams
TRACKING_S = 600         # check the mean offset of received telegrams this often
TRACKING_SAMPLES = 20     # adjust only after this many telegrams since the last change
TRACKING_THRESHOLD = 2    # adjust from this many FSCTRL0 steps (~1.59 kHz) of offset
TRACKING_INTERVAL_S = 3600  # store in NVS at most this often

PID_LINKS = 63  # link table in the channel object

# GAs per key kind relative to the offset (docs/NOTES.md "Sender-Programmierung")
GA_SPAN = {KIND_KEY: 6, KIND_ROCKER: 3}
# Blinds: travel info GO 10 is not sent by every actuator (Gira 5427 00 only reports its position via the sum status
# GO 11, about every 2 s while moving, and repeats it unchanged when the relay switches off; firmware 0.8.26 passes on
# unchanged status messages). The motion is derived from position changes and ends with the target position, an
# unchanged status message after position changes, or – as a fallback – when the reports stop.
MOVING_UP, MOVING_DOWN = 2, 3
BLIND_IDLE_S = 8.0     # fallback: no position change for this long while moving: stopped
BLIND_MARGIN_S = 15.0  # without any position report: stopped after travel time + this
BLIND_TRAVEL_S = 120.0  # travel time if the parameter has not been read (catalogue default)


def rocker_as_server_key(msg: dict[str, Any]) -> None:
    """A rocker with HA lamps addresses the server SN: the firmware reports it like a server key (press =
    bit 0x4000). For the rocker, the direction bit is 0x4000/0x8000; the hold time is in the lower 14 bits."""
    cmd = int(msg.get("cmd", "0") or "0", 16)
    msg["richtung"] = "rechts" if cmd & 0xC000 == 0x8000 else "links"
    msg["hold_ms"] = (cmd & 0x3FFF) * 10
    msg["press"] = cmd & 0x3FFF == 0
    msg.pop("code", None)


def sensor_action_key(channel: int, on: bool) -> str:
    """Key in geraet["sensor_aktionen"]: "1_ein" sun protection on … "2_aus" twilight off."""
    return f"{channel}_{'ein' if on else 'aus'}"


@dataclass
class Channel:
    """Actuator channel from anlage.json."""

    enet_device: dict[str, Any]
    nr: int
    art: str

    @property
    def ia(self) -> str:
        return self.enet_device["ia"]

    @property
    def key(self) -> str:
        return f"{self.ia}/{self.nr}"

    @property
    def obj(self) -> int:
        """Channel object (link table, parameters); export field `obj`, otherwise the rule 2 + channel."""
        k = next((k for k in self.enet_device.get("kanaele", []) if k["nr"] == self.nr), {})
        return k.get("obj") or 2 + self.nr

    @property
    def stored(self) -> dict[str, Any]:
        """Stored channel entry (kanaele) – kind, offset, object, channel type."""
        return next((k for k in self.enet_device.get("kanaele", []) if k["nr"] == self.nr), {"art": self.art})

    def go(self, function: str, default: int) -> int:
        """GO number of a device function (catalog name, e.g. "SC") for this channel's type (layouts.py)."""
        return layouts.go(self.stored, function) or default


@dataclass
class Key:
    """Key or rocker of a transmitter from anlage.json."""

    enet_device: dict[str, Any]
    nr: int
    art: str
    offset: int

    @property
    def key(self) -> str:
        return f"{self.enet_device['sn']}/{self.nr}"


@dataclass
class ParamGroup:
    """Parameters of an actuator: channel parameters (channel = no.) or device parameters (channel = None)."""

    enet_device: dict[str, Any]
    channel: int | None
    params: list[Param]
    device_obj: int


@dataclass
class Diagnose:
    rssi: float | None = None
    time_: Any = None  # datetime of the last report
    attempts: int | None = None


@dataclass
class ChannelState:
    on: bool = False
    percent: int = 0  # blind: position as in the actuator (0 % = top)
    slat: int | None = None  # blind only
    motion: int | None = None  # blind only: motion 0 stopped top, 1 stopped bottom, 2 moving up, 3 moving down
    known: bool = False
    reported: float = 0.0  # time.monotonic() of the last report that was not a reply to a status request
    moved_at: float = 0.0  # blind: time.monotonic() of the last command or position change while moving
    tracking: bool = False  # blind: position changes were reported since the command
    target: int | None = None  # blind: target position of a position command (as in the actuator)
    quiet: int = 0  # blind: unchanged status messages since the command (1st = start of the movement)
    watch: Any = None  # blind: cancel function of the motion check


@dataclass
class Hub:
    hass: HomeAssistant
    entry_id: str
    bridge: Bridge
    installation: dict[str, Any]
    channels_: list[Channel] = field(default_factory=list)  # lamps (switching, dimming)
    blinds: list[Channel] = field(default_factory=list)
    keys_: list[Key] = field(default_factory=list)
    state_: dict[str, ChannelState] = field(default_factory=dict)
    definitions: dict[str, Any] = field(default_factory=dict)
    store: InstallationStore | None = None
    real_server_time: Any = None  # datetime of the last "real server" report (display only, switches nothing)
    no_bridge: bool = False  # nothing on the port answers hello (blank ESP32, foreign device)
    flashing: bool = False
    fw_bundle: str | None = None  # version of the bundled firmware (firmware.mitgeliefert)
    _auto_update: set = field(default_factory=set)  # (bridge ID, old, new version): only once per start
    bridge_device_id: str | None = None
    device_ids: dict[str, str] = field(default_factory=dict)
    _sync_task: asyncio.Task | None = None
    _cal_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _tracking_task: asyncio.Task | None = None
    calibrations: dict[str, Any] = field(default_factory=dict)  # blind key → calibration.Calibration (hass.data)
    added: set[str] = field(default_factory=set)  # unique IDs of the entities created at this setup (visibility.py)
    _tracked_at: float = 0.0  # time.monotonic() of the last tracking adjustment
    _unsub: Any = None

    def __post_init__(self) -> None:
        for g in self.installation.get("geraete", []):
            for k in g.get("kanaele", []):
                if k["art"] in (KIND_SWITCH, KIND_DIMMER):
                    self.channels_.append(Channel(g, k["nr"], k["art"]))
                elif k["art"] == KIND_BLIND:
                    self.blinds.append(Channel(g, k["nr"], k["art"]))
            for t in g.get("tasten", []):
                if t["art"] in GA_SPAN:
                    self.keys_.append(Key(g, t["nr"], t["art"], t["offset"]))
        self.state_ = {k.key: ChannelState() for k in self.all_channels}
        self.measurements = Measurements(self)
        self.diag: dict[str, Diagnose] = {}
        self.illuminance: dict[str, float] = {}  # sun sensor SN → brightness in lux (value telegram, DPT 9.004)
        self.sensor_active: dict[str, bool] = {}  # "SN/channel" → sun protection or twilight active (scene telegram)
        self._last_sensor_scene: tuple[Any, float] = (None, 0.0)
        self.battery_low: dict[str, bool] = {}  # transmitter SN → battery low (from the RF info bit, FW 0.8.8+)
        self._battery_warned: dict[str, float] = {}  # transmitter SN → time.monotonic() of the last log warning
        self.unknown_devices: dict[str, dict[str, Any]] = {}  # SN → product number at pairing (diagnostics)
        self.param_groups: list[ParamGroup] = []
        for g in self.installation.get("geraete", []):
            d = self.definitions.get(g.get("typ", ""))
            # no lamp channels, but parameters
            sensor = g.get("typ") in SENSOR_TYPES or (g.get("rolle") == "sensor" and is_measuring_device(g))
            if not d or (g.get("rolle") != "aktor" and not sensor):
                continue
            gp = [Param(p) for p in d.get("geraet", [])]
            if gp := [p for p in gp if p.usable]:
                self.param_groups.append(ParamGroup(g, None, gp, d["geraet_obj"]))
            stored_types = {k["nr"]: k.get("kanaltyp") for k in g.get("kanaele", [])}
            for k in d.get("kanaele", []):
                if stored_types.get(k["nr"]) not in (None, k["kanaltyp"]):
                    continue  # channel switched to another type: the catalog parameters belong to the default type
                kind = next((c["art"] for c in g.get("kanaele", []) if c["nr"] == k["nr"]), None)
                kp = [p for p in (Param(d, kind=kind) for d in k["parameter"]) if p.usable]
                if kp and (sensor or any(c.enet_device is g and c.nr == k["nr"] for c in self.all_channels)):
                    self.param_groups.append(ParamGroup(g, k["nr"], kp, d["geraet_obj"]))
        self.params = ParamStore(self.hass, self.entry_id, self.bridge, self.signal("params"))
        self.params.remember_states = self._remember_states
        self.params.restore_states = self._restore_states
        self.params.frame_report = self._frame_report
        self.programmed: set[str] = set()  # IAs whose actuator is currently in a programming frame
        self.senders_active: set[str] = set()  # SNs of transmitters currently being programmed (waiting or running)
        self.scene_in_progress: str | None = None  # HA scene whose eNet part is currently being transferred
        self.scenes = SceneManager(self)
        self.ha_scenes = HaScenes(self)
        self.ha_rocker = HaRocker(self)
        self.check = Check(self)
        self._last_key: tuple[Any, float] = (None, 0.0)
        self.bridge.listen_only_mode = self.listen_only

    # signals to the entities
    def signal(self, name: str) -> str:
        return f"{DOMAIN}_{self.entry_id}_{name}"

    @property
    def server_on(self) -> bool:
        return bool(self.bridge.info.get("server", {}).get("on"))

    @property
    def server_wanted(self) -> bool:
        """Desired server switch (HA manages it and enforces it on every bridge)."""
        return bool(self.installation["server"].get("funktionen", True))

    @property
    def listen_only(self) -> bool:
        """"Listen only": the bridge only receives (keys as events, states from actuator reports) and sends
        nothing – for operation alongside the real eNet Server before migration. Server functions are then off."""
        return bool(self.installation["server"].get("mithoeren", False))

    async def set_listen_only(self, on: bool) -> None:
        self.installation["server"]["mithoeren"] = on
        self.bridge.listen_only_mode = on
        await self.save_installation()
        _LOGGER.info("Bridge: listen only %s", "on" if on else "off")
        self.check.check_scenes()  # switch the "scenes after listening" ↔ "not converted" notice immediately
        async_dispatcher_send(self.hass, self.signal("bridge"))
        if self.bridge.connected:
            await self.set_server(self.server_wanted)
        self._schedule_sync(0.5)  # off: catch up on calibration, queries, scene sync; on: calibrate passively

    def reload_after_radio(self) -> None:
        """Reload the entry (rebuild entities), but only once no radio session is running or waiting:
        reloading closes the serial port, and a running programming would abort with "Bad file descriptor".
        The lock serves waiters in order, so the reload comes after all sessions already started."""
        async def wait_and_reload() -> None:
            async with self.params.lock:
                pass
            if not self.senders_active:  # otherwise the last programming run reloads
                self.hass.config_entries.async_schedule_reload(self.entry_id)

        self.hass.async_create_background_task(wait_and_reload(), f"{DOMAIN} neu laden nach Funk")

    async def save_installation(self) -> None:
        if self.store:
            await self.store.save(self.installation)

    async def set_server(self, on: bool) -> None:
        """Set and remember the server switch; effective on the bridge only while "listen only" is not on."""
        effective = on and not self.listen_only
        reply = await self.bridge.command("server", on=effective)
        self.bridge.info.setdefault("server", {})["on"] = bool(reply.get("on", effective))
        if self.server_wanted != on:
            self.installation["server"]["funktionen"] = on
            await self.save_installation()
        async_dispatcher_send(self.hass, self.signal("bridge"))

    @property
    def real_server(self) -> bool:
        z = self.real_server_time
        return z is not None and (dt_util.now() - z).total_seconds() < REAL_SERVER_S

    @property
    def all_channels(self) -> list[Channel]:
        """All actuator channels (lamps and blinds): state, queries, parameters, GA base."""
        return self.channels_ + self.blinds

    @property
    def radio(self) -> dict[str, Any]:
        return self.bridge.info.get("radio", {})

    def channel(self, enet_device: dict[str, Any], nr: int | None) -> Channel | None:
        return next((k for k in self.all_channels if k.enet_device is enet_device and k.nr == nr), None)

    @callback
    def _frame_report(self, ia: str, active: bool, purpose: str, errors: str | None) -> None:
        """Display: "Programming" sensor, icon of this actuator's lamps and an entry in its activity
        (on the "Read parameters" button of the device page)."""
        (self.programmed.add if active else self.programmed.discard)(ia)
        self.report_programming()
        if g := next((g for g in self.installation["geraete"] if g["ia"] == ia), None):
            text = (tr("akt_wird_programmiert", zweck=purpose) if active else tr("akt_programmiert", zweck=purpose)
                    if errors is None else tr("akt_programmierung_fehler", zweck=purpose, fehler=errors))
            entity_id = er.async_get(self.hass).async_get_entity_id("button", DOMAIN, f"{g['sn']}_parameter_lesen")
            async_log_entry(self.hass, g["name"], text, DOMAIN, entity_id)
        for k in self.all_channels:
            if k.ia == ia:
                async_dispatcher_send(self.hass, self.signal(f"state_{k.key}"))

    @callback
    def report_programming(self) -> None:
        """Refresh the "Programming" sensor and – while something is being transferred – show a notice at the top
        of Settings (entry under Repairs, disappears by itself; user request 2026-11-02)."""
        async_dispatcher_send(self.hass, self.signal("programmierung"))
        names_ = sorted({g["name"] for g in self.installation["geraete"] if g["ia"] in self.programmed})
        hints = {"szene_wird_uebertragen": {"szene": self.scene_in_progress} if self.scene_in_progress else None,
                    "aktor_wird_programmiert": {"geraete": ", ".join(names_)}
                    if names_ and not self.scene_in_progress else None}
        for key, placeholder in hints.items():
            issue_id = f"{key}_{self.entry_id}"
            if placeholder is None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            else:
                ir.async_create_issue(self.hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
                                      severity=ir.IssueSeverity.WARNING, translation_key=key,
                                      translation_placeholders=placeholder)

    @callback
    def device_activity(self, enet_device: dict[str, Any], text: str) -> None:
        """Entry in a device's activity (logbook on its "Read parameters" button)."""
        entity_id = er.async_get(self.hass).async_get_entity_id("button", DOMAIN, f"{enet_device['sn']}_parameter_lesen")
        async_log_entry(self.hass, enet_device["name"], text, DOMAIN, entity_id)

    def activity(self, text: str) -> None:
        """Entry in the bridge's activity (on the "Programming" sensor) – for operations that belong to no
        existing device (pairing, removal, scene sync)."""
        entity_id = er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, f"{self.entry_id}_programmierung")
        async_log_entry(self.hass, "eNet-Bridge", text, DOMAIN, entity_id)

    def _remember_states(self, ia: str) -> dict[str, tuple[bool, int]]:
        """Known states of an actuator's channels before a programming frame."""
        return {k.key: (z.on, z.percent) for k in self.channels_ if k.ia == ia and (z := self.state_[k.key]).known}

    async def _restore_states(self, ia: str, before: dict[str, tuple[bool, int]]) -> None:
        """After the frame the outputs are off (application restart): set the channels that were on again."""
        for k in self.channels_:
            if k.key not in before or not before[k.key][0]:
                continue
            on_, percent_ = before[k.key]
            try:
                if k.art == KIND_DIMMER:
                    await self.bridge.request("dim", ia=k.ia, ch=k.nr, percent=max(1, percent_))
                else:
                    await self.bridge.request("switch", ia=k.ia, ch=k.nr, on=True)
            except BridgeError as err:
                _LOGGER.warning("%s: state not restored after programming: %s", k.key, err)
                continue
            z = self.state_[k.key]
            z.on, z.percent, z.known = True, percent_, True
            async_dispatcher_send(self.hass, self.signal(f"state_{k.key}"))

    def device_obj(self, enet_device: dict[str, Any]) -> int:
        """Device object (timestamp when programming): from the device definitions, otherwise 4."""
        return self.definitions.get(enet_device.get("typ", ""), {}).get("geraet_obj") or 4

    def _device_versions(self, info: dict[str, Any]) -> None:
        """Firmware, radio chip and ID of the bridge on the HA device (device page: "Firmware: 0.8.9")."""
        if not getattr(self, "bridge_device_id", None):
            return
        v = self.radio.get("version")
        dr.async_get(self.hass).async_update_device(
            self.bridge_device_id, sw_version=info.get("fw"),
            hw_version=f"CC1101 0x{v:02X}" if isinstance(v, int) and v else None,
            serial_number=info.get("id"))

    def model(self, enet_device: dict[str, Any]) -> str:
        """Device designation for display: German from the installation (catalog name at import/pairing), otherwise
        the English product name from the catalog."""
        model = enet_device.get("beschreibung") or enet_device.get("typ", "")
        if not german():
            model = self.definitions.get(enet_device.get("typ", ""), {}).get("produktname") or model
        return tr("ungetestet_zusatz", modell=model) if enet_device.get("typ") in UNTESTED else model

    def blind_moving(self, k: Channel, motion: int | None, target: int | None = None) -> None:
        """Provisional motion after a command (cover): until position reports confirm or end it."""
        z = self.state_[k.key]
        z.motion, z.moved_at, z.tracking, z.target, z.quiet = motion, time.monotonic(), False, target, 0
        self._watch_blind(k.key)

    def _travel_time(self, key: str) -> float:
        k = next((k for k in self.blinds if k.key == key), None)
        if k is not None:
            for g in self.device_params(k.enet_device):
                if g.channel == k.nr:
                    for p in g.params:
                        if p.id == "MUDT" and isinstance(v := self.params.value(k.enet_device["sn"], p), (int, float)) \
                                and v > 0:
                            return float(v)
        return BLIND_TRAVEL_S

    def _watch_blind(self, key: str) -> None:
        """End the motion when the position no longer changes (or after the travel time without any report)."""
        z = self.state_[key]
        if z.watch is not None:
            z.watch()
            z.watch = None
        if z.motion not in (MOVING_UP, MOVING_DOWN):
            z.tracking, z.target = False, None
            return
        limit = self._travel_time(key) + BLIND_MARGIN_S

        @callback
        def check(_now: Any) -> None:
            z.watch = None
            if z.motion not in (MOVING_UP, MOVING_DOWN):
                return
            idle = time.monotonic() - z.moved_at
            if (z.tracking and idle >= BLIND_IDLE_S) or idle >= limit:
                z.motion = None
                async_dispatcher_send(self.hass, self.signal(f"state_{key}"))
            else:
                z.watch = async_call_later(self.hass, 1.0, check)

        z.watch = async_call_later(self.hass, 1.0, check)

    def measuring_sns(self) -> set[str]:
        """Devices whose travel time is being measured (their page shows only the measuring buttons)."""
        return {c.channel.enet_device["sn"] for c in self.calibrations.values() if c.running}

    def controls_others(self, group: ParamGroup, p: Param) -> bool:
        """Does another parameter of the device depend on this one (then its entities change with the value)?"""
        return any(c["parameter"] == p.id for g in self.device_params(group.enet_device) for q in g.params
                   for c in (q.d.get("abhaengig") or {}).get("bedingungen", []))

    def param_active(self, group: ParamGroup, p: Param) -> bool:
        """Is the parameter active with the current values of the parameters it depends on (catalogue
        DeviceMemberTypeDependency, e.g. slat time only for BLINDS)? Also sets its value type for that case."""
        dep = p.d.get("abhaengig")
        if not dep:
            return p.d.get("aktiv", True)
        sn, results, typ = group.enet_device["sn"], [], None
        for c in dep["bedingungen"]:
            ctrl = self._param_by_id(group, c["parameter"]) if c["parameter"] else None
            v = self.params.value(sn, ctrl) if ctrl is not None else None
            key = str(v).lower() if isinstance(v, bool) else str(v)
            # "" = setting for all values not listed (e.g. "cancel" values only with priority 0)
            s = c["werte"].get(key, c["werte"].get("")) if v is not None else None
            if s is None:
                continue  # unknown (not read yet): does not decide
            results.append(s.get("aktiv", True))
            typ = typ or s.get("typ")
        p.current = typ
        if not results:
            return p.d.get("aktiv", True)
        return any(results) if dep.get("verknuepfung") == "OR" else all(results)

    def _param_by_id(self, group: ParamGroup, ident: str) -> Param | None:
        """Parameter by short ID: in the same channel first, then device parameters."""
        same = [g for g in self.device_params(group.enet_device) if g.channel == group.channel]
        others = [g for g in self.device_params(group.enet_device) if g.channel != group.channel]
        return next((p for g in same + others for p in g.params if p.id == ident), None)

    def device_params(self, enet_device: dict[str, Any]) -> list[ParamGroup]:
        return [g for g in self.param_groups if g.enet_device is enet_device]

    async def read_parameters(self, enet_device: dict[str, Any], only_missing: bool = False) -> int:
        params = [p for g in self.device_params(enet_device) for p in g.params]
        if only_missing:
            params = self.params.missing(enet_device["sn"], params)
        if not params:
            return 0
        return await self.woken(enet_device, lambda: self.params.read(enet_device["ia"], enet_device["sn"], params))

    async def woken(self, enet_device: dict[str, Any], job):
        """Run job(); wake a sleeping device (sun sensor) first like a transmitter: repeat the wake-up call until
        it answers after a press on a test key, keep it awake meanwhile, release it afterwards."""
        if enet_device.get("typ") not in SENSOR_TYPES:
            return await job()
        from .pair import RadioSession
        from .sender import Sender

        def activity(text: str) -> None:  # logbook on the sensor's "Read parameters" button
            eid = er.async_get(self.hass).async_get_entity_id("button", DOMAIN, f"{enet_device['sn']}_parameter_lesen")
            async_log_entry(self.hass, enet_device["name"], text, DOMAIN, eid)

        activity(tr("akt_sensor_wecken"))
        async with RadioSession(self) as radio_session:
            s = Sender(self, enet_device, radio_session)
            await s.wake()
            activity(tr("akt_sensor_wach"))
            hold = self.hass.async_create_background_task(s.keep_awake(), f"{DOMAIN} keep sensor awake")
            try:
                return await job()
            finally:
                hold.cancel()
                with contextlib.suppress(BridgeError):
                    await s.pw(self.device_obj(enet_device), 0x48, b"\x03")  # release

    @callback
    def _check_switch_on_brightness(self, key_obj: Key, msg: dict[str, Any]) -> None:
        """Both keys of a rocker ≥ 4 s: the dimmers store their brightness as switch-on brightness (OSV) without
        reporting it (the eNet Server doesn't re-read either; capture 2026-10-30) – so read it again."""
        if self.listen_only or msg.get("press") or int(msg.get("cmd", "0") or "0", 16) & 0xC000 != 0xC000 \
                or int(msg.get("hold_ms", 0)) < OSV_HOLD_MS:
            return
        entry_ = next((t for t in key_obj.enet_device.get("tasten", []) if t["nr"] == key_obj.nr), {})
        targets = {f"{z['ia']}/{z['ch']}" for z in entry_.get("ziele", [])}
        groups_ = [g for g in self.param_groups if g.channel is not None and f"{g.enet_device['ia']}/{g.channel}" in targets]
        if not groups_:
            return

        async def read() -> None:
            await asyncio.sleep(2)  # dimmer blinks as confirmation
            for g in groups_:
                params = [p for p in g.params if p.id == "OSV"]
                if params:
                    try:
                        await self.params.read(g.enet_device["ia"], g.enet_device["sn"], params)
                    except BridgeError as err:
                        _LOGGER.warning("%s: switch-on brightness not read: %s", g.enet_device["name"], err)

        self.hass.async_create_background_task(read(), f"{DOMAIN} Einschalthelligkeit lesen")

    async def _preload_parameters(self) -> None:
        """Read missing actuator parameters (all on first start; re-read via the "Read parameters" button)."""
        for g in {id(x.enet_device): x.enet_device for x in self.param_groups}.values():
            if g.get("typ") in SENSOR_TYPES:
                continue  # sleeps: only on button press (with wake-up)
            if n := await self.read_parameters(g, only_missing=True):
                _LOGGER.info("%s: %d parameters read", g["name"], n)

    def key_for(self, sn: str, ga: int, base: int | None = None) -> Key | None:
        for t in self.keys_:
            if t.enet_device["sn"] != sn:
                continue
            b = t.enet_device.get("ga_basis", 0) if base is None else base
            rel = ga - b * 256 - t.offset
            if 1 <= rel <= GA_SPAN[t.art]:
                return t
        return None

    def _battery(self, msg: dict) -> None:
        """Transmitter battery state: in the RF info byte of every telegram (as with the eNet Server)."""
        if (b := msg.get("battery")) not in ("ok", "weak") or not (sn := msg.get("sn")):
            return
        weak = b == "weak"
        if self.battery_low.get(sn) != weak:
            # the sun sensor toggles the bit on almost every telegram → at most one warning per hour per device
            if weak and time.monotonic() - self._battery_warned.get(sn, -BATTERY_WARNING_S) >= BATTERY_WARNING_S:
                _LOGGER.warning("Battery low: transmitter %s", sn)
                self._battery_warned[sn] = time.monotonic()
            self.battery_low[sn] = weak
            async_dispatcher_send(self.hass, self.signal(f"batterie_{sn}"))

    def _sensor_value(self, msg: dict) -> bool:
        """Value telegram of a sun sensor: brightness (2-byte DPT 9.004, measured GA base·256 + 4).
        Returns True if the telegram has been handled."""
        sn = msg.get("sn")
        if not sn or not any(g["sn"] == sn and g.get("typ") in SENSOR_TYPES for g in self.installation["geraete"]):
            return False
        if msg.get("kind") == "wert1" and msg.get("cmd"):
            self._sensor_scene(sn, int(msg.get("ga", -1)), int(msg["cmd"], 16))
            return True
        if msg.get("kind") != "wert" or not msg.get("cmd"):
            _LOGGER.info("Sun sensor %s: telegram not evaluated yet: %s", sn, msg)
            return True
        lux = round(dpt9_decode(bytes.fromhex(msg["cmd"])), 2)
        if self.illuminance.get(sn) != lux:
            self.illuminance[sn] = lux
            async_dispatcher_send(self.hass, self.signal(f"helligkeit_{sn}"))
        return True

    def _sensor_scene(self, sn: str, ga: int, value: int) -> None:
        """Scene telegram of the sun sensor (1 byte, scene number in bits 5–0; the first one also carries bit 6,
        the repetitions do not). Channel from the GA: base·256 + 3 = K1 sun protection, + 7 = K2 twilight
        (GO offset 0/4 + 3). Factory setting: K1 scene 48 sun protection on / 49 off, K2 50 twilight on / 51 off
        (the test keys on the sensor trigger the on scene: K1 → 48, K2 → 50, measured 2026-11-06).
        The twilight threshold is far below the sun protection threshold: one channel on sets the other off."""
        channel = {3: 1, 7: 2}.get(ga & 0xFF)
        scene = value & 0x3F
        if channel is None:
            _LOGGER.info("Sun sensor %s: scene %d to unknown GA %d", sn, scene, ga)
            return
        ident, now_ = (sn, channel, scene), time.monotonic()
        if self._last_sensor_scene[0] == ident and now_ - self._last_sensor_scene[1] < REPEAT_S:
            return  # repetitions of the sensor (4× after the first telegram)
        self._last_sensor_scene = (ident, now_)
        # Scene numbers from the device (ASNVE/ASNVU, channel object 3 or 4, PID 79 bytes 6/7; read 2026-11-06:
        # K1 48 above / 49 below, K2 51 above / 50 below). Sun protection on = above, twilight on = below threshold.
        values_ = self.params.values_.get(sn, {})
        obj = channel + 2
        above, below = values_.get(f"{obj}/79/6"), values_.get(f"{obj}/79/7")
        if above is not None and below is not None and scene in (int(above, 16), int(below, 16)):
            on = scene == int(above if channel == 1 else below, 16)
        else:
            on = scene % 2 == 0  # factory setting (48/49, 50/51) as long as the parameters have not been read
        self.sensor_active[f"{sn}/{channel}"] = on
        if on:
            self.sensor_active[f"{sn}/{3 - channel}"] = False
        _LOGGER.debug("Sun sensor %s ch%d: scene %d (%s)", sn, channel, scene, "on" if on else "off")
        async_dispatcher_send(self.hass, self.signal(f"sensor_aktiv_{sn}"))
        async_dispatcher_send(self.hass, self.signal(f"sensor_szene_{sn}_{channel}"), {"ein": on, "szene": scene})
        g = next((g for g in self.installation["geraete"] if g["sn"] == sn), {})
        action = g.get("sensor_aktionen", {}).get(sensor_action_key(channel, on))
        if action and not self.listen_only:
            self.hass.async_create_background_task(self._sensor_action(g, channel, on, action), "enet Sensoraktion")

    async def _sensor_action(self, g: dict, channel: int, on: bool, action: str) -> None:
        """Recall the sun sensor's selected scene (selection on the device page). HA scene via scene.turn_on –
        its eNet part goes out as a scene telegram (HaScenes); eNet scene directly as a scene telegram."""
        art, _, value = action.partition(":")
        name = f"{g.get('name', g['sn'])} K{channel} {'ein' if on else 'aus'}"
        try:
            if art == "ha":
                eid = next((st.entity_id for st in self.hass.states.async_all("scene")
                            if str(st.attributes.get("id")) == value), None)
                if eid is None:
                    _LOGGER.warning("%s: scene %s not found", name, value)
                    return
                _LOGGER.debug("%s → %s", name, eid)
                await self.hass.services.async_call("scene", "turn_on", {"entity_id": eid}, blocking=True)
            elif art == "enet":
                s = next((s for s in self.scenes.scenes if str(s.id) == value), None)
                if s is None:
                    _LOGGER.warning("%s: eNet scene %s not found", name, value)
                    return
                _LOGGER.debug("%s → eNet scene %s", name, s.name)
                await self.scenes.call_scene(s)
        except Exception as err:  # noqa: BLE001 – only report scene errors, sensor evaluation continues
            _LOGGER.warning("%s: scene failed: %s", name, err)

    def _learn_sender_base(self, sn: str, ga: int) -> Key | None:
        """Key press doesn't match the known GA base: transmitters sleep (link table not readable), so derive
        the base from the telegram (GA = base·256 + offset + 1…n) if it is not yet confirmed."""
        t = self.key_for(sn, ga, base=ga >> 8)
        if t is None or t.enet_device.get("ga_basis_geprueft"):
            return None
        t.enet_device["ga_basis"], t.enet_device["ga_basis_geprueft"] = ga >> 8, True
        _LOGGER.info("%s: GA base %d taken from button press", t.enet_device["name"], ga >> 8)
        self.hass.async_create_task(self.save_installation())
        return t

    async def _read_actuator_bases(self) -> None:
        """GA base of the actuators from the factory entry of the link table (GO 1: type 01, own SN, GA = base·256
        + offset + 1). Once per device; if anything changes, the bridge configuration is transferred again."""
        changed = False
        for g in self.installation["geraete"]:
            k = next((k for k in self.all_channels if k.enet_device is g), None)
            if k is None or g.get("ga_basis_geprueft"):
                continue
            for idx in range(8):
                try:
                    r = await self.bridge.request("fp_read", ia=g["ia"], obj=k.obj, pid=PID_LINKS,
                                                  data=f"000001{idx:02x}")
                except BridgeError as err:
                    _LOGGER.debug("%s: link table (entry %d): %s", g["name"], idx, err)
                    break  # end of table (rc 255) or not reachable → retry later
                v = bytes.fromhex(r.get("value", ""))
                if len(v) >= 9 and v[0] == 0x01 and v[1:7].hex() == g["sn"]:
                    base = v[7]
                    if base != g.get("ga_basis", 0):
                        _LOGGER.warning("%s: GA base %d (installation had %d)", g["name"], base, g.get("ga_basis", 0))
                        changed = True
                    g["ga_basis"], g["ga_basis_geprueft"] = base, True
                    await self.save_installation()
                    break
        if changed:
            await self._sync_config(self.bridge.info)

    # --- Lifecycle ----------------------------------------------------------
    async def async_start(self) -> None:
        self.fw_bundle = await self.hass.async_add_executor_job(firmware.bundled)
        self._unsub = self.bridge.add_listener(self._on_message)
        self.bridge.start()
        self.ha_scenes.start()
        self.measurements.start()
        self._tracking_task = self.hass.async_create_background_task(self._track_frequency(),
                                                                        f"{DOMAIN} frequency tracking")

    async def async_stop(self) -> None:
        self.ha_scenes.stop()
        self.ha_rocker.stop()
        self.measurements.stop()
        if self._unsub:
            self._unsub()
        if self._sync_task:
            self._sync_task.cancel()
        if self._tracking_task:
            self._tracking_task.cancel()
        await self.hass.async_add_executor_job(self.bridge.stop)

    def _schedule_sync(self, delay: float) -> None:
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
        self._sync_task = self.hass.async_create_background_task(self._sync(delay), f"{DOMAIN} sync")

    async def _sync(self, delay: float) -> None:
        """Sequence 10.1: hello (clock), then states from the table, query unknown channels."""
        await asyncio.sleep(delay)
        if self.flashing:
            return
        try:
            info = await self._hello()
            if info is None:
                return
            if self._update_needed(info):
                self.hass.async_create_background_task(self.update_firmware(), f"{DOMAIN} firmware")
                return
            _LOGGER.info("eNet bridge connected: firmware %s, protocol %s, configuration %s",
                         info.get("fw"), info.get("proto"), info.get("config"))
            self._device_versions(info)
            async_dispatcher_send(self.hass, self.signal("bridge"))
            if not self.radio.get("ok"):
                _LOGGER.warning("Bridge: radio module (CC1101) not reachable – check the wiring")
                return
            await self._sync_config(info)
            if self.server_on != (self.server_wanted and not self.listen_only):
                _LOGGER.info("Bridge: server functions → %s (as set in HA)", self.server_wanted)
                await self.set_server(self.server_wanted)
            if self.listen_only:  # send nothing: states only from the table, calibration from foreign telegrams
                self.check.check_scenes()  # scenes stay eNet scenes until "listen only" is off: notice
                await self.bridge.command("state")
                if not self.radio.get("calibrated"):
                    await self._calibrate_passive()
                return
            calibrated = not self.radio.get("calibrated")
            if calibrated:
                await self.calibrate()
            elif self.all_channels and not await self._answers(list({k.ia: k for k in self.all_channels}.values())
                                                             [:CAL_TEST_CHANNELS]):
                # Otherwise states come from the bridge's table without any actuator having to answer: after a
                # module swap the old correction would remain and the bridge would be deaf (tracking needs reception).
                _LOGGER.warning("No actuator answers – searching the frequency correction again")
                await self.calibrate()
                calibrated = True  # don't search again right away
            for z in self.state_.values():
                z.known = False
            await self.bridge.command("state")
            await asyncio.sleep(0.5)  # wait for state events from the table
            for attempt in range(3):
                open_items = [k for k in self.all_channels if not self.state_[k.key].known]
                if not open_items:
                    break
                if attempt:
                    await asyncio.sleep(2)
                for k in open_items:
                    try:
                        await self.bridge.request("poll", ia=k.ia, ch=k.nr)
                    except BridgeError as err:
                        _LOGGER.debug("poll %s (Versuch %d): %s", k.key, attempt + 1, err)
            if not calibrated and self.all_channels and not any(self.state_[k.key].known for k in self.all_channels):
                _LOGGER.warning("No actuator answers – searching the frequency correction again")
                if await self.calibrate():
                    for k in self.all_channels:
                        try:
                            await self.bridge.request("poll", ia=k.ia, ch=k.nr)
                        except BridgeError:
                            pass
            for k in self.all_channels:
                if not self.state_[k.key].known:
                    _LOGGER.warning("%s (%s) does not answer the status request", k.enet_device["name"], k.key)
            await self._read_actuator_bases()
            await self._preload_parameters()
            self.check.check_devices()
            for step in (self.ha_scenes.convert,  # eNet scenes from the import → HA scenes (once)
                            self.ha_scenes.reconcile,  # only differences go out over the radio
                            self.check.check_links,  # installation check: read only
                            self.check.check_links_tables):  # link tables (once per actuator), read only
                try:
                    await step()
                except BridgeError:
                    raise
                except Exception:  # noqa: BLE001 – unexpected installation/scenes.yaml: report, bridge keeps running
                    _LOGGER.exception("Scenes: %s failed", step.__name__)
            self.check.check_scenes()
        except BridgeError as err:
            _LOGGER.warning("Synchronisation with the bridge failed: %s", err)

    async def _hello(self) -> dict[str, Any] | None:
        """hello with retries; if no bridge answers, a notice about firmware installation."""
        for attempt in range(HELLO_ATTEMPTS):
            if attempt:
                await asyncio.sleep(2)
            try:
                info = await self.bridge.hello()
            except BridgeError as err:
                if err.err != "timeout":
                    raise
                continue
            if info.get("product", "enet-bridge") == "enet-bridge" and "proto" in info:
                self.no_bridge = False
                return info
            break
        self.bridge.info = {}
        if not self.no_bridge:
            self.no_bridge = True
            _LOGGER.warning("No eNet bridge answers on port %s – install the firmware via the update entity "
                            "\"Bridge firmware\"", self.bridge.port)
        async_dispatcher_send(self.hass, self.signal("bridge"))
        return None

    def _update_needed(self, info: dict[str, Any]) -> bool:
        """Bridge answers as a bridge and the bundled firmware is newer → automatic. Only once per bridge and
        starting version: if it still reports the old version after flashing, no loop."""
        new = self.fw_bundle
        if not new or firmware.version_tuple(info.get("fw")) >= firmware.version_tuple(new):
            return False
        attempt = (info.get("id"), info.get("fw"), new)
        if attempt in self._auto_update:
            return False
        self._auto_update.add(attempt)
        _LOGGER.warning("Bridge firmware %s is outdated – updating to %s automatically", info.get("fw"), new)
        return True

    async def update_firmware(self) -> None:
        """Release the port, flash the bundled firmware, reconnect (followed by a new sync)."""
        if self.flashing or not self.fw_bundle:
            return
        if foreign_radio(self.bridge.port):
            raise BridgeError("fremd", tr("keine_bridge_port", port=self.bridge.port))
        self.flashing = True
        async_dispatcher_send(self.hass, self.signal("bridge"))
        try:
            await self.hass.async_add_executor_job(self.bridge.release)
            # chip from the hello; blank ESP (no bridge) → esptool detects it
            chip = None if self.no_bridge else self.bridge.info.get("chip", firmware.DEFAULT_CHIP)
            await self.hass.async_add_executor_job(firmware.flash, self.bridge.port, chip)
            _LOGGER.warning("Bridge firmware %s installed", self.fw_bundle)
        except Exception as err:  # noqa: BLE001 – esptool raises arbitrary errors
            _LOGGER.error("Flashing the bridge failed: %s", err)
            raise BridgeError("flash", str(err)) from err
        finally:
            self.flashing = False
            self.bridge.reconnect()
            async_dispatcher_send(self.hass, self.signal("bridge"))

    async def _sync_config(self, info: dict[str, Any]) -> None:
        """Transfer the server configuration from anlage.json if the bridge has a different one (or none)."""
        layouts_ok = firmware.version_tuple(info.get("fw")) >= esp_config.FW_GO_LAYOUTS
        blob = esp_config.build(self.installation, version=4 if layouts_ok else 3)[0]
        crc = blob[-2:].hex()
        cfg = info.get("config", {})
        if cfg.get("valid") and cfg.get("crc") == crc:
            return
        _LOGGER.info("Bridge: transferring configuration %s → %s", cfg.get("crc"), crc)
        outcome = await self.bridge.upload_config(esp_config.split_parts(blob))
        self.bridge.info["config"] = {"valid": True, **{k: outcome.get(k) for k in
                                                         ("crc", "channels", "scenes", "keys")}}

    async def _answers(self, channels_: list[Channel]) -> bool:
        for k in channels_:
            try:
                await self.bridge.request("poll", ia=k.ia, ch=k.nr)
                return True
            except BridgeError:
                continue
        return False

    async def calibrate(self) -> int | None:
        """Search for the frequency correction (status requests only), fine-tune and store it on the bridge.

        Coarse: try candidates until an actuator answers. Fine: average the FREQEST of the replies
        (FSCTRL0 += FREQEST). Returns the stored value or None (no actuator reachable)."""
        async with self._cal_lock:
            # one channel per device, so the coarse search tries different actuators
            test = list({k.ia: k for k in self.all_channels}.values())[:CAL_TEST_CHANNELS]
            if not test:
                return None
            start = self.radio.get("freq_offset", CAL_CANDIDATES[0])
            for f in dict.fromkeys((start, *CAL_CANDIDATES)):
                await self._radio(freq_offset=f)
                if await self._answers(test):
                    break
            else:
                await self._radio(freq_offset=start)
                _LOGGER.warning("Calibration: no actuator answers at any frequency correction")
                return None
            for _ in range(2):  # collect telegrams for the average
                for k in self.all_channels:
                    try:
                        await self.bridge.request("poll", ia=k.ia, ch=k.nr)
                    except BridgeError:
                        pass
            r = await self._radio()
            if r.get("freqest_n", 0) >= CAL_SAMPLES:
                f = max(-128, min(127, f + round(r["freqest"])))
            await self._radio(freq_offset=f, save=True)
            _LOGGER.info("Calibration: frequency correction %d saved (previous offset %.1f from %d telegrams)",
                         f, r.get("freqest", 0.0), r.get("freqest_n", 0))
            return f

    async def _track_frequency(self) -> None:
        """Track the frequency correction during operation without sending: the bridge averages the FREQEST of all
        valid telegrams since the last change. If the average is off by at least TRACKING_THRESHOLD steps,
        adjust FSCTRL0 by that amount and store it (at most once per TRACKING_INTERVAL_S).
        Catches drift (temperature, ageing) and a module swap; replaces the former "calibrate radio" button."""
        while True:
            await asyncio.sleep(TRACKING_S)
            if (self.flashing or not self.bridge.connected or self._cal_lock.locked()
                    or not self.radio.get("ok") or not self.radio.get("calibrated")):
                continue
            try:
                async with self._cal_lock:
                    r = await self._radio()
                    n, est, f = r.get("freqest_n", 0), r.get("freqest"), r.get("freq_offset")
                    if (n < TRACKING_SAMPLES or est is None or f is None or abs(est) < TRACKING_THRESHOLD
                            or time.monotonic() - self._tracked_at < TRACKING_INTERVAL_S):
                        continue
                    new = max(-128, min(127, f + round(est)))
                    await self._radio(freq_offset=new, save=True)
                    self._tracked_at = time.monotonic()
                    _LOGGER.info("Frequency correction adjusted: %d → %d (offset %.1f from %d telegrams)",
                                 f, new, est, n)
            except BridgeError as err:
                _LOGGER.debug("Frequency tracking: %s", err)

    async def _calibrate_passive(self) -> int | None:
        """Frequency correction without sending: per candidate, wait until enough foreign telegrams (keys, actuator
        reports, real server) have been received, then fine-tune with their FREQEST and store. Runs until it
        succeeds (or "listen only" is off – then the normal calibration takes over)."""
        start = self.radio.get("freq_offset", CAL_CANDIDATES[0])
        _LOGGER.info("Listen only: determining frequency correction from received telegrams")
        while self.listen_only:
            for f in dict.fromkeys((start, *CAL_CANDIDATES)):
                await self._radio(freq_offset=f)
                end = time.monotonic() + CAL_PASSIVE_S
                while time.monotonic() < end and self.listen_only:
                    await asyncio.sleep(15)
                    r = await self._radio()
                    if r.get("freqest_n", 0) >= CAL_SAMPLES:
                        f = max(-128, min(127, f + round(r["freqest"])))
                        await self._radio(freq_offset=f, save=True)
                        _LOGGER.info("Listen only: frequency correction %d saved (from %d telegrams)",
                                     f, r["freqest_n"])
                        return f
                if not self.listen_only:
                    return None
        return None

    async def _radio(self, **fields_: Any) -> dict[str, Any]:
        reply = await self.bridge.command("radio", **fields_)
        self.bridge.info["radio"] = reply["radio"]
        try:  # receive counters for diagnostics (valid / CRC error / unreadable), shown on the freq. correction sensor
            self.bridge.info["stats"] = await self.bridge.command("stats")
        except BridgeError:
            pass
        async_dispatcher_send(self.hass, self.signal("bridge"))
        return reply["radio"]

    # --- Events -------------------------------------------------------------
    @callback
    def _after_bridge_scene(self, nr: int) -> None:
        """Local bridge scene finished: if it contained a scene recall (IA 0), query the members' status as after
        a recall from HA (the bridge itself only follows up on single commands)."""
        names_ = list(self.installation.get("esp", {}).get("szenen", {}))
        if not 0 <= nr < len(names_):
            return
        for a in self.installation["esp"]["szenen"][names_[nr]]:
            if a["ia"] == "0.0.0":
                s = next((s for s in self.scenes.scenes if s.ga == a["handle"] and s.nr == int(a["wert"], 16)), None)
                if s:
                    self.scenes.poll_status(s)

    @callback
    def _redraw_bridge(self, _now: Any = None) -> None:
        async_dispatcher_send(self.hass, self.signal("bridge"))

    @callback
    def _on_message(self, msg: dict[str, Any]) -> None:
        ev = msg.get("ev")
        if ev == "_connection":
            if msg["connected"]:
                self._schedule_sync(SYNC_DELAY_S)
            async_dispatcher_send(self.hass, self.signal("bridge"))
        elif ev == "radio":
            war_ok = self.radio.get("ok")
            self.bridge.info["radio"] = msg.get("radio", {})
            if self.radio.get("ok"):
                _LOGGER.info("Bridge: radio module reachable again")
                if not war_ok:
                    self._schedule_sync(0.5)
            else:
                _LOGGER.warning("Bridge: radio module (CC1101) stopped answering – check the wiring")
            async_dispatcher_send(self.hass, self.signal("bridge"))
        elif ev == "start":
            _LOGGER.info("Bridge started (%s)", msg.get("reset_reason"))
            self._schedule_sync(0.2)
        if ev in ("state", "result") and msg.get("rssi") is not None and msg.get("ia"):
            d = self.diag.setdefault(msg["ia"], Diagnose())
            d.rssi, d.time_ = msg["rssi"], dt_util.now()
            if ev == "result":
                d.attempts = msg.get("tries")
            async_dispatcher_send(self.hass, self.signal(f"diag_{msg['ia']}"))
        if ev == "state":
            key = f"{msg.get('ia')}/{msg.get('ch')}"
            if (z := self.state_.get(key)) is not None:
                _LOGGER.debug("State %s: %s", key, msg)
                self.check.answers(msg.get("ia"))
                old_known, old_percent = z.known, z.percent
                z.on, z.percent, z.known = bool(msg.get("on")), int(msg.get("percent", 0)), True
                if msg.get("kind") == "blind":
                    z.slat = msg.get("slat")
                    if msg.get("move") is not None:
                        z.motion = int(msg["move"])
                    elif old_known and z.percent != old_percent:
                        z.motion = MOVING_DOWN if z.percent > old_percent else MOVING_UP
                        z.moved_at, z.tracking = time.monotonic(), True
                        if z.target is not None and z.percent == z.target:
                            z.motion = None  # target position reached
                        self._watch_blind(key)
                    elif msg.get("source") == "status" and z.motion in (MOVING_UP, MOVING_DOWN):
                        # unchanged status message: the first one after a command marks the start, one after
                        # position changes (or a second one, already in the end position) the end of the travel
                        if z.tracking or z.quiet >= 1:
                            z.motion = None
                            self._watch_blind(key)
                        else:
                            z.quiet += 1
                if msg.get("source") != "poll":
                    z.reported = time.monotonic()
                async_dispatcher_send(self.hass, self.signal(f"state_{key}"))
        elif ev == "key":
            self._battery(msg)
            if self.measurements.telegram(msg) or self._sensor_value(msg):
                return
            key_obj = self.key_for(msg.get("sn", ""), int(msg.get("ga", -1))) \
                or self._learn_sender_base(msg.get("sn", ""), int(msg.get("ga", -1)))
            # seq (status requests): new press = new seq, repetition (transmitter saw no reply) = same seq
            ident = (msg.get("sn"), msg.get("ga"), msg.get("cmd"), msg.get("seq"))
            now_ = time.monotonic()
            if self._last_key[0] == ident and now_ - self._last_key[1] < REPEAT_S:
                _LOGGER.debug("Repeated button telegram discarded: %s", msg)
                return  # transmitter repeats unanswered telegrams (e.g. release after "both keys")
            self._last_key = (ident, now_)
            if key_obj is None:
                _LOGGER.debug("Button press without assignment: %s", msg)
                self.check.unknown_sender(msg.get("sn", ""))
                return
            if key_obj.art == KIND_ROCKER and msg.get("kind") == "server":
                rocker_as_server_key(msg)
            async_dispatcher_send(self.hass, self.signal(f"key_{key_obj.key}"), msg)
            if key_obj.art == KIND_ROCKER:
                self.ha_rocker.key_press(key_obj, msg)
            self.ha_scenes.key_press(msg)
            self._check_switch_on_brightness(key_obj, msg)
        elif ev == "scene_done":
            self._after_bridge_scene(int(msg.get("nr", -1)))
        elif ev == "real_server":
            if not self.real_server and self.server_on:
                _LOGGER.warning("Real eNet server active – bridge and server both answer the server buttons. "
                                "Switch off the server or the bridge's \"Server functions\"")
            self.real_server_time = dt_util.now()
            async_dispatcher_send(self.hass, self.signal("bridge"))
            async_call_later(self.hass, REAL_SERVER_S + 1, self._redraw_bridge)
        elif ev == "error":
            _LOGGER.warning("Bridge reports an error: %s", msg)


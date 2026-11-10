"""HA scenes as the original: their eNet part is kept invisibly as an eNet scene in the actuators.

When a scene is saved in the scene editor (HA reloads the scenes → "scene_reloaded") and at startup, the
integration reconciles: eNet lamps of the scene get links and their scene value (SCV1[nr]), removed lamps
and deleted scenes lose their links. When HA calls the scene (scene.turn_on), the eNet part goes out as one
scene telegram; the single commands HA then sends to the eNet lamps are suppressed for a short time
(scenes.SceneManager.suppress). The rest of the scene runs through HA as usual.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.logbook import async_log_entry
from homeassistant.const import ATTR_ENTITY_ID, EVENT_CALL_SERVICE, STATE_ON
from homeassistant.core import Event, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.yaml import dump, load_yaml

from .bridge import BridgeError
from .const import KIND_DIMMER, DOMAIN
from .scenes import value_equal
from .texts import tr

if TYPE_CHECKING:
    from .hub import Hub, Channel

_LOGGER = logging.getLogger(__name__)

EVENT_SCENE_RELOADED = "scene_reloaded"  # homeassistant/components/homeassistant/scene.py


def scene_value(channel: Channel, state_: dict[str, Any]) -> int:
    """State of a lamp in the HA scene → SCV1 byte (switch 00/FF, dimmer 0–255 via whole percent)."""
    if state_.get("state") != STATE_ON:
        return 0
    if channel.art != KIND_DIMMER or state_.get("brightness") is None:
        return 255
    percent_ = max(1, round(int(state_["brightness"]) * 100 / 255))
    return round(percent_ * 255 / 100)


def state_from_value(channel: Channel, value: int) -> dict[str, Any]:
    """SCV1 byte → state of a lamp in scenes.yaml (inverse of scene_value)."""
    if not value:
        return {"state": "off"}
    return {"state": "on", "brightness": value} if channel.art == KIND_DIMMER else {"state": "on"}


class HaScenes:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.hass = hub.hass
        self._lock = asyncio.Lock()
        self._unsubs: list = []
        self.cfg: list[dict[str, Any]] = []  # last read scenes.yaml (choice of key actions)
        self.in_progress: set[str] = set()  # HA scenes whose eNet part is currently being transferred
        self.per_taste: set[str] = set()  # HA scenes whose eNet part the bridge already called via a server key

    def names_(self) -> dict[str, str]:
        """All HA scenes (also without an eNet lamp): id → name."""
        return {str(s["id"]): s.get("name") or str(s["id"]) for s in self.cfg}

    def start(self) -> None:
        self._unsubs = [self.hass.bus.async_listen(EVENT_SCENE_RELOADED, self._reloaded),
                        self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._service)]

    def stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []

    # --- Reconcile -----------------------------------------------------------
    @callback
    def _reloaded(self, event: Event) -> None:
        self.schedule_reconcile()

    def schedule_reconcile(self) -> None:
        async def safe() -> None:
            try:
                await self.reconcile()
            except Exception:  # noqa: BLE001 – never take the integration down with it
                _LOGGER.exception("HA scenes: sync failed")

        self.hass.async_create_background_task(safe(), "enet HA-Szenen abgleichen")

    def _lamps(self) -> dict[str, Channel]:
        """entity_id → channel of the eNet lights of this installation."""
        by_uid = {f"{k.enet_device['sn']}_{k.nr}": k for k in self.hub.channels_}
        reg = er.async_get(self.hass)
        return {e.entity_id: by_uid[e.unique_id] for e in er.async_entries_for_config_entry(reg, self.hub.entry_id)
                if e.domain == "light" and e.unique_id in by_uid}

    async def _configuration(self) -> list[dict[str, Any]]:
        path_ = self.hass.config.path("scenes.yaml")

        def read() -> Any:
            try:
                return load_yaml(path_)
            except FileNotFoundError:
                return []

        data_ = await self.hass.async_add_executor_job(read)
        self.cfg = [s for s in data_ or [] if isinstance(s, dict) and s.get("id")
                       and isinstance(s.get("entities"), dict)]
        async_dispatcher_send(self.hass, self.hub.signal("ha_szenen"))  # refresh the choice of key actions
        return self.cfg

    async def reconcile(self) -> None:
        async with self._lock:
            try:
                cfg = await self._configuration()
            except Exception as err:  # noqa: BLE001 – broken scenes.yaml: HA reports that itself
                _LOGGER.warning("scenes.yaml not readable: %s", err)
                return
            if self.hub.listen_only:  # write nothing to the actuators; reconcile when it is switched off (sync)
                return
            lamps = self._lamps()
            manager = self.hub.scenes
            errors: list[str] = []
            seen = set()
            work = []  # (ha_id, name, target): eNet part must be changed over the radio
            for sc_ in cfg:
                wanted = {lamps[e].key: scene_value(lamps[e], z if isinstance(z, dict) else {"state": z})
                        for e, z in sc_["entities"].items() if e in lamps}
                ha_id, name = str(sc_["id"]), sc_.get("name") or str(sc_["id"])
                if not wanted:
                    continue
                seen.add(ha_id)
                s = manager.ha_scene(ha_id)
                if s and not s.open_items and set(s.members) == set(wanted) \
                        and all(value_equal(s.values_.get(k), w) for k, w in wanted.items()):
                    if s.name != name:
                        s.name = name
                        await manager._save()
                    continue
                work.append((ha_id, name, wanted))
            gone = [s for s in manager.scenes if s.ha_id and s.ha_id not in seen]
            # While transferring, these scenes (key as well as call from HA) run individually through HA – the half
            # written eNet part is not used (user 2026-10-30: call right after saving)
            self.in_progress = {ha_id for ha_id, _, _ in work} | {s.ha_id for s in gone}
            if self.in_progress:
                await self._refresh_keys()
            for ha_id, name, wanted in work:
                self.hub.scene_in_progress = name
                self.hub.report_programming()
                self._activity(ha_id, name, tr("akt_szene_uebertragen"))
                try:
                    f = await manager.reconcile_ha(ha_id, name, wanted)
                except BridgeError as err:
                    f = [str(err)]
                errors += [f"{name}: {x}" for x in f]
                self._activity(ha_id, name, tr("akt_szene_uebertragen_ok") if not f
                                 else tr("akt_szene_uebertragen_teil", fehler="; ".join(f)))
            for s in gone:
                open_items = await manager.delete(s)  # HA scene deleted or without an eNet lamp
                errors += [tr("links_in_nicht_geloescht", name=s.name, wo=k) for k in open_items]
                if open_items:
                    self.hub.activity(tr("akt_szene_entfernt_offen", name=s.name, geraete=", ".join(open_items)))
            self.in_progress = set()
            self.hub.scene_in_progress = None
            self.hub.report_programming()
            await self._refresh_keys()
            if errors:  # per scene it is in its activity; retried at the next save or start
                _LOGGER.warning("HA-Szenen: %s", "; ".join(errors))

    @callback
    def _activity(self, ha_id: str, name: str, text: str) -> None:
        """Entry in the HA scene's activity."""
        eid = next((st.entity_id for st in self.hass.states.async_all("scene")
                    if str(st.attributes.get("id")) == ha_id), None)
        async_log_entry(self.hass, name, text, DOMAIN, eid)

    # --- Call ---------------------------------------------------------------
    @callback
    def _service(self, event: Event) -> None:
        """scene.turn_on: send the eNet part right away as a scene telegram, before HA switches the lamps one by one."""
        if event.data.get("domain") != "scene" or event.data.get("service") != "turn_on" or self.hub.listen_only:
            return
        data_ = event.data.get("service_data") or {}
        ids = data_.get(ATTR_ENTITY_ID) or []
        for entity_id in [ids] if isinstance(ids, str) else ids:
            state_ = self.hass.states.get(entity_id)
            ha_id = state_.attributes.get("id") if state_ else None
            s = self.hub.scenes.ha_scene(str(ha_id)) if ha_id is not None else None
            if s is None or not s.values_ or s.ha_id in self.in_progress:
                continue  # without a (finished) eNet part: HA switches individually
            if s.ha_id in self.per_taste:
                continue  # server key: the bridge already sent the scene telegram, lock is set
            _LOGGER.debug("scene.turn_on %s → eNet scene %s (GA %s, no. %s, values %s)", entity_id, s.name, s.ga,
                          s.nr, s.values_)
            self.hub.scenes.suppress(s)
            self.hass.async_create_background_task(self._call(s), f"enet Szene {s.name}")

    async def _call(self, s) -> None:
        try:
            await self.hub.scenes.call_scene(s)
        except BridgeError as err:  # release the lock; the later correction catches lamps already suppressed
            for key in s.members:
                self.hub.scenes.lock.pop(key, None)
            _LOGGER.warning("Scene %s: eNet call failed (%s), switching lights individually", s.name, err)
            self.hub.scenes.poll_status(s)

    # --- Transmitter keys with HA scene ---------------------------------------
    async def _refresh_keys(self) -> None:
        """Re-set server keys with an HA scene: number/GA of the eNet part may have changed, a scene may have gained or
        lost an eNet part."""
        from . import sender  # sender imports hub
        esp = self.hub.installation.get("esp", {})
        before = (repr(esp.get("server_tasten")), repr(esp.get("szenen")))
        for g in self.hub.installation["geraete"]:
            if any(t.get("ha") for t in esp.get("server_tasten", []) if t["sender"] == g["sn"]) \
                    or any(t.get("szene") for t in esp.get("server_tasten", []) if t["sender"] == g["sn"]):
                try:
                    sender.set_server_keys(self.hub, g, sender.sender_actions(self.hub, g))
                except BridgeError:
                    continue  # assignment unknown
        if (repr(esp.get("server_tasten")), repr(esp.get("szenen"))) != before:
            await self.hub.save_installation()
            await self.hub._sync_config(self.hub.bridge.info)

    @callback
    def key_press(self, msg: dict[str, Any]) -> None:
        """Server key with an HA scene: the bridge already called the eNet part, HA switches the rest. On press –
        or on release if the bridge caught up on the scene there (press not received; firmware from 0.8.16 then
        reports scene ≥ 0)."""
        caught_up = not msg.get("press") and (msg.get("scene") if msg.get("scene") is not None else -1) >= 0
        if msg.get("kind") != "server" or not (msg.get("press") or caught_up):
            return
        ga = int(msg.get("ga", -1)) & 0xFF
        t = next((t for t in self.hub.installation.get("esp", {}).get("server_tasten", [])
                  if t["sender"] == msg.get("sn") and t["ga"] & 0xFF == ga and t.get("ha")), None)
        if t is not None:
            self.hass.async_create_background_task(self._switch_rest(str(t["ha"])), "enet Szene per Taste")

    async def _switch_rest(self, ha_id: str) -> None:
        sc_ = next((s for s in self.cfg if str(s["id"]) == ha_id), None)
        if sc_ is None:
            return
        s = self.hub.scenes.ha_scene(ha_id)
        if s is None or not s.values_ or ha_id in self.in_progress:  # without a (finished) eNet part: call normally
            eid = next((st.entity_id for st in self.hass.states.async_all("scene")
                        if str(st.attributes.get("id")) == ha_id), None)
            if eid:
                await self.hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: eid})
            return
        # Call the scene via scene.turn_on, not just the rest via scene.apply: otherwise the scene entity stays
        # "never activated" (HA only sets the timestamp on turn_on). The bridge already sent the scene telegram:
        # _service skips it (per_taste), the lock catches the single commands to the eNet lamps.
        eid = next((st.entity_id for st in self.hass.states.async_all("scene")
                    if str(st.attributes.get("id")) == ha_id), None)
        if eid is None:
            lamps = self._lamps()
            rest = {e: z for e, z in sc_["entities"].items() if not (e in lamps and lamps[e].key in s.values_)}
            if rest:
                await self.hass.services.async_call("scene", "apply", {"entities": rest})
            return
        self.hub.scenes.suppress(s)
        self.per_taste.add(ha_id)
        try:
            await self.hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: eid}, blocking=True)
        finally:
            self.per_taste.discard(ha_id)

    # --- Convert: eNet scenes (import) → HA scenes ----------------------------
    async def convert(self) -> None:
        """Create eNet scenes and bridge scenes from the import as HA scenes.

        eNet scenes: values read from the actuators; the eNet scene stays in the actuators and becomes the eNet part,
        the new HA scene takes over the entity ID of the old eNet scene entity. If a value is missing (radio), the
        scene stays unchanged until the next start.
        Bridge scenes (links of the eNet Server with single commands, e.g. "lights on"): states from the commands;
        the reconciliation then creates the eNet part, and the keys then call "Scene: …"."""
        async with self._lock:
            manager = self.hub.scenes
            channels_ = {k.key: k for k in self.hub.channels_}
            entity = {k.key: e for e, k in self._lamps().items()}
            new = []
            for i, s in enumerate([s for s in manager.scenes if s.art != "ha"]):
                values_: dict[str, int] = {}
                for key in s.members:
                    k = channels_.get(key)
                    if k is None or key not in entity:
                        continue
                    for _ in range(3):
                        try:
                            r = await self.hub.bridge.request("prop_read", ia=k.ia, obj=k.obj, pid=79,
                                                              start=4 + s.nr, count=1)
                            values_[key] = bytes.fromhex(r.get("value", ""))[0]
                            break
                        except (BridgeError, IndexError) as err:
                            _LOGGER.debug("Scene %s, %s: value not read (%s)", s.name, key, err)
                if len(values_) < len([m for m in s.members if m in entity]):
                    _LOGGER.warning("Scene %s: not all scene values readable – converting on next start", s.name)
                    continue
                ha_id = f"{int(time.time() * 1000) + i}"
                entry_ = {"id": ha_id, "name": s.name,
                           "entities": {entity[key]: state_from_value(channels_[key], w) for key, w in values_.items()},
                           "metadata": {}}
                new.append((s, ha_id, entry_, values_))
            bridge = self._bridge_scenes(channels_, entity)
            if not new and not bridge:
                return
            reg = er.async_get(self.hass)
            path_ = self.hass.config.path("scenes.yaml")

            def write() -> None:
                try:
                    data_ = load_yaml(path_) or []
                except FileNotFoundError:
                    data_ = []
                data_ += [e for _, _, e, _ in new] + [e for _, e in bridge]
                write_utf8_file_atomic(path_, dump(data_))

            await self.hass.async_add_executor_job(write)
            for s, ha_id, _, values_ in new:
                old = reg.async_get_entity_id("scene", DOMAIN, f"{self.hub.entry_id}_szene_{s.id}")
                button = reg.async_get_entity_id("button", DOMAIN, f"{self.hub.entry_id}_szene_{s.id}_speichern")
                for eid in (old, button):
                    if eid:
                        reg.async_remove(eid)
                if old:  # reserve the entity ID of the old eNet scene for the HA scene
                    reg.async_get_or_create("scene", "homeassistant", ha_id, suggested_object_id=old.split(".", 1)[1])
                s.art, s.ha_id, s.values_, s.open_items = "ha", ha_id, values_, []
            await manager._save()
            if bridge:  # switch keys to the new HA scene, the bridge scene is dropped
                esp = self.hub.installation["esp"]
                for name, entry_ in bridge:
                    for t in esp.get("server_tasten", []):
                        if t.get("szene") == name:
                            t.pop("szene")
                            t["ha"] = entry_["id"]
                    del esp["szenen"][name]
                await self.hub.save_installation()
                await self.hub._sync_config(self.hub.bridge.info)
            names_ = [s.name for s, *_ in new] + [n for n, _ in bridge]
            _LOGGER.info("Created as HA scenes: %s", ", ".join(names_))
        await self.hass.services.async_call("scene", "reload", blocking=True)  # triggers the reconciliation
        self.hub.activity(tr("akt_szenen_uebernommen", namen=", ".join(names_)))

    def _bridge_scenes(self, channels_: dict[str, Channel], entity: dict[str, str]) -> list[tuple[str, dict]]:
        """Bridge scenes that only contain single commands to known lamps → (name, scenes.yaml entry).
        Handle = channel offset + GO (2 switch 00/01, 4 dim value 0–255; dim value 0 = off)."""
        out = []
        for i, (name, actions) in enumerate(self.hub.installation.get("esp", {}).get("szenen", {}).items()):
            states_: dict[str, dict] = {}
            for a in actions:
                if a["ia"] == "0.0.0":
                    break  # calls an eNet scene: stays a bridge scene
                hits = None
                for k in channels_.values():
                    off = next((x.get("offset", 0) for x in k.enet_device.get("kanaele", []) if x["nr"] == k.nr), 0)
                    if k.ia == a["ia"] and a["handle"] - off in (2, 4):
                        hits = k, a["handle"] - off
                        break
                if hits is None or hits[0].key not in entity:
                    break
                k, go = hits
                value = int(a["wert"], 16)
                states_[entity[k.key]] = state_from_value(k, 255 if go == 2 and value else value)
            else:
                if states_:
                    out.append((name, {"id": f"{int(time.time() * 1000) + 500 + i}", "name": name,
                                       "entities": states_, "metadata": {}}))
        return out

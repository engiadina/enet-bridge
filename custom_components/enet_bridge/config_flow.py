"""Setup: port of the bridge and origin of the installation.

Three ways: upload an eNet Server export (migration), a new installation (without server, own domain and
server identity) or an existing anlage.json (tools/iex.py). Afterwards the installation lives in HA's store.

"+ Add device" (subentry dialog): name/room, search with progress indicator, result as a message.
No subentry is left behind – the installation manages the devices.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    FileSelector,
    FileSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import pair, iex, sender
from .installation import load_file, new_installation
from .bridge import BridgeError, foreign_radio, list_ports
from .const import CONF_INSTALLATION, CONF_INSTALLATION_DATA, CONF_PORT, DEFAULT_INSTALLATION, DOMAIN
from . import texts
from .texts import tr

CONF_EXPORT = "export"
CONF_SERVER_OFF = "server_aus"


class EnetConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {"geraet": AddDevice}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnetOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        texts.set_language(self.hass.config.language)  # initial setup runs before async_setup_entry
        return self.async_show_menu(step_id="user", menu_options=["export", "neu", "datei"])

    async def _port_schema(self) -> dict:
        ports = await self.hass.async_add_executor_job(list_ports)
        return {vol.Required(CONF_PORT, default=ports[0] if ports else ""): SelectSelector(
            SelectSelectorConfig(options=ports, custom_value=True, mode=SelectSelectorMode.DROPDOWN))}

    async def _port_ok(self, port: str, errors: dict[str, str]) -> bool:
        if foreign_radio(port):
            errors[CONF_PORT] = "port_fremd"
        elif not await self.hass.async_add_executor_job(os.path.exists, port):
            errors[CONF_PORT] = "port_missing"
        return not errors

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the bridge port (e.g. after replugging); the installation stays unchanged."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None and await self._port_ok(user_input[CONF_PORT], errors):
            port = user_input[CONF_PORT]
            if any(e.data.get(CONF_PORT) == port for e in self._async_current_entries() if e.entry_id != entry.entry_id):
                return self.async_abort(reason="port_in_use")
            return self.async_update_reload_and_abort(entry, data_updates={CONF_PORT: port})
        schema = await self._port_schema()
        if not foreign_radio(entry.data[CONF_PORT]):
            schema = {vol.Required(CONF_PORT, default=entry.data[CONF_PORT]): schema[next(iter(schema))]}
        return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema(schema), errors=errors)

    async def _create(self, port: str, installation: dict[str, Any], **data_: Any) -> ConfigFlowResult:
        srv = installation["server"]
        if any(e.data.get(CONF_PORT) == port for e in self._async_current_entries()):
            return self.async_abort(reason="port_in_use")
        await self.async_set_unique_id(srv["domain"])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"eNet {srv['domain']}", data={CONF_PORT: port, **data_})

    async def async_step_export(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Migration: take over the eNet Server project export (.iex) (domain, server identity, devices, scenes)."""
        errors: dict[str, str] = {}
        if user_input is not None and await self._port_ok(user_input[CONF_PORT], errors):
            def read() -> dict[str, Any]:
                with process_uploaded_file(self.hass, user_input[CONF_EXPORT]) as path_:
                    return iex.read_iex(path_)

            try:
                installation = await self.hass.async_add_executor_job(read)
            except Exception:  # noqa: BLE001 – arbitrary file format; the cause is shown in the form
                errors[CONF_EXPORT] = "export_invalid"
            else:
                if installation["server"].get("verschluesselt"):
                    errors[CONF_EXPORT] = "export_encrypted"
                else:
                    # If the real server is still running: "listen only" – the bridge sends nothing (otherwise both
                    # answer); switch it off once the server is off, then the server functions are active too
                    installation["server"]["funktionen"] = True
                    installation["server"]["mithoeren"] = not user_input[CONF_SERVER_OFF]
                    return await self._create(user_input[CONF_PORT], installation, **{CONF_INSTALLATION_DATA: installation})
        schema = vol.Schema({
            vol.Required(CONF_EXPORT): FileSelector(FileSelectorConfig(accept=".iex")),
            **await self._port_schema(),
            vol.Required(CONF_SERVER_OFF, default=False): BooleanSelector(),
        })
        return self.async_show_form(step_id="export", data_schema=schema, errors=errors)

    async def async_step_neu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """New installation without eNet Server: domain and server identity are generated, devices are paired later."""
        errors: dict[str, str] = {}
        if user_input is not None and await self._port_ok(user_input[CONF_PORT], errors):
            installation = new_installation()
            return await self._create(user_input[CONF_PORT], installation, **{CONF_INSTALLATION_DATA: installation})
        return self.async_show_form(step_id="neu", data_schema=vol.Schema(await self._port_schema()),
                                    errors=errors)

    async def async_step_datei(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take over an existing anlage.json (tools/iex.py) once."""
        errors: dict[str, str] = {}
        if user_input is not None and await self._port_ok(user_input[CONF_PORT], errors):
            try:
                installation = await self.hass.async_add_executor_job(load_file, user_input[CONF_INSTALLATION])
            except FileNotFoundError:
                errors[CONF_INSTALLATION] = "anlage_missing"
            except (ValueError, KeyError):
                errors[CONF_INSTALLATION] = "anlage_invalid"
            else:
                return await self._create(user_input[CONF_PORT], installation, **{CONF_INSTALLATION_DATA: installation})
        schema = vol.Schema({**await self._port_schema(),
                             vol.Required(CONF_INSTALLATION, default=DEFAULT_INSTALLATION): str})
        return self.async_show_form(step_id="datei", data_schema=schema, errors=errors)


class AddDevice(ConfigSubentryFlow):
    """"+ Add device": put an actuator or transmitter into learning mode, HA searches and pairs it."""

    def __init__(self) -> None:
        self._input: dict[str, Any] = {}
        self._job: asyncio.Task | None = None
        self._device: dict[str, Any] | None = None
        self._errors: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="nicht_geladen")
        if user_input is not None:
            self._input = user_input
            return await self.async_step_suchen()
        schema = vol.Schema({vol.Optional("name"): str, vol.Optional("raum"): AreaSelector()})
        return self.async_show_form(step_id="user", data_schema=schema)

    async def _pair(self) -> None:
        hub = self._get_entry().runtime_data
        room = None
        if area_id := self._input.get("raum"):
            area = ar.async_get(self.hass).async_get_area(area_id)
            room = area.name if area else None
        try:
            self._device = await pair.add(hub, self._input.get("name") or None, room)
        except BridgeError as err:
            self._errors = str(err)

    async def async_step_suchen(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._job is None:
            self._job = self.hass.async_create_task(self._pair(), "enet Gerät hinzufügen")
        if not self._job.done():
            return self.async_show_progress(step_id="suchen", progress_action="suchen", progress_task=self._job)
        return self.async_show_progress_done(next_step_id="fertig")

    async def async_step_fertig(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        self.hass.config_entries.async_schedule_reload(self._get_entry().entry_id)
        if self._device is None:
            return self.async_abort(reason="fehlgeschlagen",
                                    description_placeholders={"fehler": self._errors or tr("unbekannt")})
        g = self._device
        return self.async_abort(reason="eingelernt", description_placeholders={
            "name": g["name"], "ia": g["ia"], "typ": self._get_entry().runtime_data.model(g)})


class EnetOptionsFlow(OptionsFlow):
    """"Configure" on the eNet installation: menu – assign transmitter, remove device (with overview and confirm)."""

    def __init__(self) -> None:
        self._sn: str | None = None
        self._kinds: dict[int, str] = {}
        self._assignment: dict[int, dict[str, Any]] = {}
        self._actions: dict[int, str] = {}
        self._programmed = False
        self._name = ""
        self._job: asyncio.Task | None = None
        self._report: dict[str, Any] | None = None
        self._errors: str | None = None

    def _hub(self):
        return self.config_entry.runtime_data if self.config_entry.state is ConfigEntryState.LOADED else None

    def _device(self) -> dict[str, Any] | None:
        hub = self._hub()
        return next((g for g in hub.installation["geraete"] if g["sn"] == self._sn), None) if hub else None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._hub() is None:
            return self.async_abort(reason="nicht_geladen")
        return self.async_show_menu(step_id="init", menu_options=["sender", "entfernen"])

    # --- Assign transmitter -------------------------------------------------
    async def async_step_sender(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = self._hub()
        if user_input is not None:
            self._sn = user_input["geraet"]
            return await self.async_step_betriebsart()
        options_ = [SelectOptionDict(value=g["sn"], label=f"{g['name']} ({g['ia']}, {hub.model(g)})")
                    for g in sorted(hub.installation["geraete"], key=lambda g: pair.ia_int(g["ia"]))
                    if g.get("rolle") == "sender"]
        if not options_:
            return self.async_abort(reason="keine_sender")
        schema = vol.Schema({vol.Required("geraet"): SelectSelector(SelectSelectorConfig(options=options_))})
        return self.async_show_form(step_id="sender", data_schema=schema)

    async def async_step_betriebsart(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub, g = self._hub(), self._device()
        if g is None:
            return self.async_abort(reason="nicht_geladen")
        try:
            old = sender.assignment(g)
        except BridgeError as err:
            return self.async_abort(reason="fehlgeschlagen", description_placeholders={"fehler": str(err)})
        n_rockers = sender.rocker_count(hub, g)
        if user_input is not None:
            self._kinds = {n: user_input[f"wippe_{n}"] for n in range(1, n_rockers + 1)}
            return await self.async_step_details()
        options_ = ["wippe", "tasten"]  # labels: strings.json `selector.wippen_art`
        fields_ = {}
        for n in range(1, n_rockers + 1):
            a = old.get(n)
            now_ = "tasten" if a is not None and a["art"] == "tasten" else "wippe"  # without lamps = not used
            fields_[vol.Required(f"wippe_{n}", default=now_)] = SelectSelector(SelectSelectorConfig(
                options=options_, translation_key="wippen_art"))
        return self.async_show_form(step_id="betriebsart", data_schema=vol.Schema(fields_),
                                    description_placeholders={"name": g["name"]})

    async def async_step_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub, g = self._hub(), self._device()
        if g is None:
            return self.async_abort(reason="nicht_geladen")
        old = sender.assignment(g)
        if user_input is not None:
            self._assignment, self._actions = {}, {}
            for n, art in self._kinds.items():
                if art == "wippe":
                    targets = [{"ia": k.split("/")[0], "ch": int(k.split("/")[1])} for k in user_input.get(f"lampen_{n}", [])]
                    ha = list(user_input.get(f"ha_lampen_{n}", []))
                    if targets or ha or n in old:  # unassigned rocker without lamps: nothing to write
                        before = old.get(n, {}) if old.get(n, {}).get("art") == "wippe" else {}
                        self._assignment[n] = {"art": "wippe", "ziele": targets, "ha_ziele": ha,
                                        "antwort_ga": before.get("antwort_ga")}
                elif art == "tasten":
                    before = old.get(n, {}).get("tasten", {}) if old.get(n, {}).get("art") == "tasten" else {}
                    self._assignment[n] = {"art": "tasten", "tasten": {k: dict(before.get(k, {})) for k in (2 * n - 1, 2 * n)}}
                    for k in (2 * n - 1, 2 * n):
                        self._actions[k] = user_input.get(f"taste_{k}", sender.REPORT)
            self._programmed = sender.needs_programming(g, self._assignment)
            return await self.async_step_uebernehmen()
        lamps = [SelectOptionDict(value=k.key, label=f"{k.enet_device['name']} K{k.nr} ({k.key})") for k in hub.channels_]
        actions = [SelectOptionDict(value=w, label=t) for w, t in sender.action_options(hub)]
        own = [e.entity_id for e in er.async_entries_for_config_entry(er.async_get(self.hass), self.config_entry.entry_id)]
        fields_ = {}
        for n, art in self._kinds.items():
            if art == "wippe":
                now_ = [f"{z['ia']}/{z['ch']}" for z in old.get(n, {}).get("ziele", [])]
                fields_[vol.Optional(f"lampen_{n}", default=now_)] = SelectSelector(
                    SelectSelectorConfig(options=lamps, multiple=True))
                # lamps of other integrations: HA switches/dims them, the bridge acknowledges the rocker
                fields_[vol.Optional(f"ha_lampen_{n}", default=old.get(n, {}).get("ha_ziele", []))] = EntitySelector(
                    EntitySelectorConfig(domain=["light", "switch"], multiple=True, exclude_entities=own))
            elif art == "tasten":
                for k in (2 * n - 1, 2 * n):
                    fields_[vol.Required(f"taste_{k}", default=sender.key_action(hub, g, k))] = SelectSelector(
                        SelectSelectorConfig(options=actions))
        return self.async_show_form(step_id="details", data_schema=vol.Schema(fields_),
                                    description_placeholders={"name": g["name"]})

    async def _assign(self) -> None:
        try:
            self._programmed = await sender.change_assignment(self._hub(), self._device(), self._assignment, self._actions)
        except BridgeError as err:
            self._errors = str(err)

    async def async_step_uebernehmen(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._job is None:
            self._job = self.hass.async_create_task(self._assign(), "enet Sender belegen")
        if not self._job.done():
            return self.async_show_progress(step_id="uebernehmen", progress_task=self._job,
                                            progress_action="programmieren" if self._programmed else "uebertragen")
        return self.async_show_progress_done(next_step_id="belegt")

    async def async_step_belegt(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)  # recreate key entities
        g = self._device()
        if self._errors:
            return self.async_abort(reason="fehlgeschlagen", description_placeholders={"fehler": self._errors})
        return self.async_abort(reason="belegt_programmiert" if self._programmed else "belegt",
                                description_placeholders={"name": g["name"] if g else ""})

    async def async_step_entfernen(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = self._hub()
        if user_input is not None:
            self._sn = user_input["geraet"]
            return await self.async_step_bestaetigen()
        options_ = [SelectOptionDict(value=g["sn"], label=f"{g['name']} ({g['ia']}, {hub.model(g)})")
                    for g in sorted(hub.installation["geraete"], key=lambda g: pair.ia_int(g["ia"]))]
        schema = vol.Schema({vol.Required("geraet"): SelectSelector(SelectSelectorConfig(options=options_))})
        return self.async_show_form(step_id="entfernen", data_schema=schema)

    async def async_step_bestaetigen(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        g = self._device()
        if g is None:
            return self.async_abort(reason="nicht_geladen")
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("verstanden"):
                return await self.async_step_laeuft()
            errors["verstanden"] = "nicht_bestaetigt"
        a = pair.impact(self._hub(), g)
        rows = []
        if g.get("rolle") == "sender":
            rows.append(tr("folge_sender_wecken"))
            if a["aktoren"]:
                rows.append(tr("folge_links_geloescht", liste=", ".join(a["aktoren"])))
            if a["tasten"]:
                rows.append(tr("folge_tasten_entfallen", liste=", ".join(a["tasten"])))
        else:
            if a["verweise"]:
                rows.append(tr("folge_verweise", liste=", ".join(a["verweise"])))
            if a["esp_szenen"]:
                rows.append(tr("folge_esp_szenen", liste=", ".join(a["esp_szenen"])))
        if a["szenen"]:
            rows.append(tr("folge_szenen", liste=", ".join(a["szenen"])))
        self._name = g["name"]
        schema = vol.Schema({vol.Required("verstanden", default=False): BooleanSelector()})
        return self.async_show_form(step_id="bestaetigen", data_schema=schema, errors=errors,
                                    description_placeholders={
                                        "name": g["name"], "ia": g["ia"], "typ": self._hub().model(g),
                                        "details": "\n".join(rows) or tr("folge_nichts")})

    async def _remove(self) -> None:
        try:
            self._report = await pair.remove(self._hub(), self._device())
        except BridgeError as err:
            self._errors = str(err)

    async def async_step_laeuft(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._job is None:
            self._job = self.hass.async_create_task(self._remove(), "enet Gerät entfernen")
        if not self._job.done():
            return self.async_show_progress(step_id="laeuft", progress_action="laeuft", progress_task=self._job)
        return self.async_show_progress_done(next_step_id="fertig")

    async def async_step_fertig(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._report is None:
            return self.async_abort(reason="fehlgeschlagen", description_placeholders={"fehler": self._errors or "?"})
        reg = dr.async_get(self.hass)
        for dev in dr.async_entries_for_config_entry(reg, self.config_entry.entry_id):
            if any(i[0] == DOMAIN and i[1].split("_")[0] == self._sn for i in dev.identifiers):
                reg.async_remove_device(dev.id)  # the device and, if any, its channel devices
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        open_items = self._report["offen"]
        return self.async_abort(reason="entfernt_offen" if open_items else "entfernt",
                                description_placeholders={"name": self._name, "offen": "; ".join(open_items)})


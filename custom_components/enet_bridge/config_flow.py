"""Einrichtung: Port der Bridge und Herkunft der Anlage.

Drei Wege: Export des eNet Servers hochladen (Umzug), neue Anlage (ohne Server, eigene Domäne und
Server-Kennung) oder eine bestehende anlage.json (tools/iex.py). Danach liegt die Anlage im Speicher von HA.

„+ Gerät hinzufügen“ (Untereintrag-Dialog): Name/Raum, Suche mit Fortschrittsanzeige, Ergebnis als Meldung.
Es bleibt kein Untereintrag stehen – die Geräte verwaltet die Anlage.
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
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    FileSelector,
    FileSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import einlernen, iex, sender
from .anlage import lade_datei, neue_anlage
from .bridge import BridgeError, fremdes_funkmodul, list_ports
from .const import CONF_ANLAGE, CONF_ANLAGE_DATEN, CONF_PORT, DEFAULT_ANLAGE, DOMAIN
from . import texte
from .texte import tr

CONF_EXPORT = "export"
CONF_SERVER_AUS = "server_aus"


class EnetConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {"geraet": GeraetHinzufuegen}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnetOptionen()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        texte.setzen(self.hass.config.language)  # Erst-Einrichtung läuft vor async_setup_entry
        return self.async_show_menu(step_id="user", menu_options=["export", "neu", "datei"])

    async def _port_schema(self) -> dict:
        ports = await self.hass.async_add_executor_job(list_ports)
        return {vol.Required(CONF_PORT, default=ports[0] if ports else ""): SelectSelector(
            SelectSelectorConfig(options=ports, custom_value=True, mode=SelectSelectorMode.DROPDOWN))}

    async def _port_ok(self, port: str, errors: dict[str, str]) -> bool:
        if fremdes_funkmodul(port):
            errors[CONF_PORT] = "port_fremd"
        elif not await self.hass.async_add_executor_job(os.path.exists, port):
            errors[CONF_PORT] = "port_missing"
        return not errors

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Port der Bridge ändern (z. B. nach Umstecken); die Anlage bleibt unverändert."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None and await self._port_ok(user_input[CONF_PORT], errors):
            port = user_input[CONF_PORT]
            if any(e.data.get(CONF_PORT) == port for e in self._async_current_entries() if e.entry_id != entry.entry_id):
                return self.async_abort(reason="port_in_use")
            return self.async_update_reload_and_abort(entry, data_updates={CONF_PORT: port})
        schema = await self._port_schema()
        if not fremdes_funkmodul(entry.data[CONF_PORT]):
            schema = {vol.Required(CONF_PORT, default=entry.data[CONF_PORT]): schema[next(iter(schema))]}
        return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema(schema), errors=errors)

    async def _anlegen(self, port: str, anlage: dict[str, Any], **daten: Any) -> ConfigFlowResult:
        srv = anlage["server"]
        if any(e.data.get(CONF_PORT) == port for e in self._async_current_entries()):
            return self.async_abort(reason="port_in_use")
        await self.async_set_unique_id(srv["domain"])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"eNet {srv['domain']}", data={CONF_PORT: port, **daten})

    async def async_step_export(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Umzug: Projektexport (.iex) des eNet Servers übernehmen (Domäne, Server-Kennung, Geräte, Szenen)."""
        errors: dict[str, str] = {}
        if user_input is not None and await self._port_ok(user_input[CONF_PORT], errors):
            def lesen() -> dict[str, Any]:
                with process_uploaded_file(self.hass, user_input[CONF_EXPORT]) as pfad:
                    return iex.read_iex(pfad)

            try:
                anlage = await self.hass.async_add_executor_job(lesen)
            except Exception:  # noqa: BLE001 – beliebiges Dateiformat; Ursache steht im Formular
                errors[CONF_EXPORT] = "export_invalid"
            else:
                if anlage["server"].get("verschluesselt"):
                    errors[CONF_EXPORT] = "export_encrypted"
                else:
                    # Läuft der echte Server noch: „Nur mithören“ – die Bridge sendet nichts (sonst antworten beide);
                    # ausschalten, sobald der Server aus ist, dann sind auch die Server-Funktionen aktiv
                    anlage["server"]["funktionen"] = True
                    anlage["server"]["mithoeren"] = not user_input[CONF_SERVER_AUS]
                    return await self._anlegen(user_input[CONF_PORT], anlage, **{CONF_ANLAGE_DATEN: anlage})
        schema = vol.Schema({
            vol.Required(CONF_EXPORT): FileSelector(FileSelectorConfig(accept=".iex")),
            **await self._port_schema(),
            vol.Required(CONF_SERVER_AUS, default=False): BooleanSelector(),
        })
        return self.async_show_form(step_id="export", data_schema=schema, errors=errors)

    async def async_step_neu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Neue Anlage ohne eNet Server: Domäne und Server-Kennung werden erzeugt, Geräte später einlernen."""
        errors: dict[str, str] = {}
        if user_input is not None and await self._port_ok(user_input[CONF_PORT], errors):
            anlage = neue_anlage()
            return await self._anlegen(user_input[CONF_PORT], anlage, **{CONF_ANLAGE_DATEN: anlage})
        return self.async_show_form(step_id="neu", data_schema=vol.Schema(await self._port_schema()),
                                    errors=errors)

    async def async_step_datei(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Bestehende anlage.json (tools/iex.py) einmalig übernehmen."""
        errors: dict[str, str] = {}
        if user_input is not None and await self._port_ok(user_input[CONF_PORT], errors):
            try:
                anlage = await self.hass.async_add_executor_job(lade_datei, user_input[CONF_ANLAGE])
            except FileNotFoundError:
                errors[CONF_ANLAGE] = "anlage_missing"
            except (ValueError, KeyError):
                errors[CONF_ANLAGE] = "anlage_invalid"
            else:
                return await self._anlegen(user_input[CONF_PORT], anlage, **{CONF_ANLAGE_DATEN: anlage})
        schema = vol.Schema({**await self._port_schema(),
                             vol.Required(CONF_ANLAGE, default=DEFAULT_ANLAGE): str})
        return self.async_show_form(step_id="datei", data_schema=schema, errors=errors)


class GeraetHinzufuegen(ConfigSubentryFlow):
    """„+ Gerät hinzufügen“: Aktor oder Sender in den Lernmodus versetzen, HA sucht und lernt ein."""

    def __init__(self) -> None:
        self._eingabe: dict[str, Any] = {}
        self._aufgabe: asyncio.Task | None = None
        self._geraet: dict[str, Any] | None = None
        self._fehler: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="nicht_geladen")
        if user_input is not None:
            self._eingabe = user_input
            return await self.async_step_suchen()
        schema = vol.Schema({vol.Optional("name"): str, vol.Optional("raum"): AreaSelector()})
        return self.async_show_form(step_id="user", data_schema=schema)

    async def _einlernen(self) -> None:
        hub = self._get_entry().runtime_data
        raum = None
        if area_id := self._eingabe.get("raum"):
            area = ar.async_get(self.hass).async_get_area(area_id)
            raum = area.name if area else None
        try:
            self._geraet = await einlernen.aufnehmen(hub, self._eingabe.get("name") or None, raum)
        except BridgeError as err:
            self._fehler = str(err)

    async def async_step_suchen(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._aufgabe is None:
            self._aufgabe = self.hass.async_create_task(self._einlernen(), "enet Gerät hinzufügen")
        if not self._aufgabe.done():
            return self.async_show_progress(step_id="suchen", progress_action="suchen", progress_task=self._aufgabe)
        return self.async_show_progress_done(next_step_id="fertig")

    async def async_step_fertig(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        self.hass.config_entries.async_schedule_reload(self._get_entry().entry_id)
        if self._geraet is None:
            return self.async_abort(reason="fehlgeschlagen",
                                    description_placeholders={"fehler": self._fehler or tr("unbekannt")})
        g = self._geraet
        return self.async_abort(reason="eingelernt", description_placeholders={
            "name": g["name"], "ia": g["ia"], "typ": self._get_entry().runtime_data.modell(g)})


class EnetOptionen(OptionsFlow):
    """„Konfigurieren“ an der eNet-Anlage: Menü – Sender belegen, Gerät entfernen (mit Übersicht und Bestätigung)."""

    def __init__(self) -> None:
        self._sn: str | None = None
        self._arten: dict[int, str] = {}
        self._bel: dict[int, dict[str, Any]] = {}
        self._aktionen: dict[int, str] = {}
        self._programmiert = False
        self._name = ""
        self._aufgabe: asyncio.Task | None = None
        self._bericht: dict[str, Any] | None = None
        self._fehler: str | None = None

    def _hub(self):
        return self.config_entry.runtime_data if self.config_entry.state is ConfigEntryState.LOADED else None

    def _geraet(self) -> dict[str, Any] | None:
        hub = self._hub()
        return next((g for g in hub.anlage["geraete"] if g["sn"] == self._sn), None) if hub else None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._hub() is None:
            return self.async_abort(reason="nicht_geladen")
        return self.async_show_menu(step_id="init", menu_options=["sender", "entfernen"])

    # --- Sender belegen -----------------------------------------------------
    async def async_step_sender(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = self._hub()
        if user_input is not None:
            self._sn = user_input["geraet"]
            return await self.async_step_betriebsart()
        optionen = [SelectOptionDict(value=g["sn"], label=f"{g['name']} ({g['ia']}, {hub.modell(g)})")
                    for g in sorted(hub.anlage["geraete"], key=lambda g: einlernen.ia_int(g["ia"]))
                    if g.get("rolle") == "sender"]
        if not optionen:
            return self.async_abort(reason="keine_sender")
        schema = vol.Schema({vol.Required("geraet"): SelectSelector(SelectSelectorConfig(options=optionen))})
        return self.async_show_form(step_id="sender", data_schema=schema)

    async def async_step_betriebsart(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub, g = self._hub(), self._geraet()
        if g is None:
            return self.async_abort(reason="nicht_geladen")
        try:
            alt = sender.belegung(g)
        except BridgeError as err:
            return self.async_abort(reason="fehlgeschlagen", description_placeholders={"fehler": str(err)})
        n_wippen = sender.wippen_anzahl(hub, g)
        if user_input is not None:
            self._arten = {n: user_input[f"wippe_{n}"] for n in range(1, n_wippen + 1)}
            return await self.async_step_details()
        optionen = ["wippe", "tasten"]  # Beschriftung: strings.json „selector.wippen_art“
        felder = {}
        for n in range(1, n_wippen + 1):
            a = alt.get(n)
            jetzt = "tasten" if a is not None and a["art"] == "tasten" else "wippe"  # ohne Lampen = nicht benutzt
            felder[vol.Required(f"wippe_{n}", default=jetzt)] = SelectSelector(SelectSelectorConfig(
                options=optionen, translation_key="wippen_art"))
        return self.async_show_form(step_id="betriebsart", data_schema=vol.Schema(felder),
                                    description_placeholders={"name": g["name"]})

    async def async_step_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub, g = self._hub(), self._geraet()
        if g is None:
            return self.async_abort(reason="nicht_geladen")
        alt = sender.belegung(g)
        if user_input is not None:
            self._bel, self._aktionen = {}, {}
            for n, art in self._arten.items():
                if art == "wippe":
                    ziele = [{"ia": k.split("/")[0], "ch": int(k.split("/")[1])} for k in user_input.get(f"lampen_{n}", [])]
                    if ziele or n in alt:  # unbelegte Wippe ohne Lampen: nichts zu schreiben
                        self._bel[n] = {"art": "wippe", "ziele": ziele}
                elif art == "tasten":
                    vorher = alt.get(n, {}).get("tasten", {}) if alt.get(n, {}).get("art") == "tasten" else {}
                    self._bel[n] = {"art": "tasten", "tasten": {k: dict(vorher.get(k, {})) for k in (2 * n - 1, 2 * n)}}
                    for k in (2 * n - 1, 2 * n):
                        self._aktionen[k] = user_input.get(f"taste_{k}", sender.MELDEN)
            self._programmiert = sender.muss_programmieren(g, self._bel)
            return await self.async_step_uebernehmen()
        lampen = [SelectOptionDict(value=k.key, label=f"{k.geraet['name']} K{k.nr} ({k.key})") for k in hub.kanaele]
        aktionen = [SelectOptionDict(value=w, label=t) for w, t in sender.aktion_optionen(hub)]
        felder = {}
        for n, art in self._arten.items():
            if art == "wippe":
                jetzt = [f"{z['ia']}/{z['ch']}" for z in alt.get(n, {}).get("ziele", [])]
                felder[vol.Optional(f"lampen_{n}", default=jetzt)] = SelectSelector(
                    SelectSelectorConfig(options=lampen, multiple=True))
            elif art == "tasten":
                for k in (2 * n - 1, 2 * n):
                    felder[vol.Required(f"taste_{k}", default=sender.aktion_der_taste(hub, g, k))] = SelectSelector(
                        SelectSelectorConfig(options=aktionen))
        return self.async_show_form(step_id="details", data_schema=vol.Schema(felder),
                                    description_placeholders={"name": g["name"]})

    async def _belegen(self) -> None:
        try:
            self._programmiert = await sender.belegung_aendern(self._hub(), self._geraet(), self._bel, self._aktionen)
        except BridgeError as err:
            self._fehler = str(err)

    async def async_step_uebernehmen(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._aufgabe is None:
            self._aufgabe = self.hass.async_create_task(self._belegen(), "enet Sender belegen")
        if not self._aufgabe.done():
            return self.async_show_progress(step_id="uebernehmen", progress_task=self._aufgabe,
                                            progress_action="programmieren" if self._programmiert else "uebertragen")
        return self.async_show_progress_done(next_step_id="belegt")

    async def async_step_belegt(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)  # Tasten-Entitäten neu
        g = self._geraet()
        if self._fehler:
            return self.async_abort(reason="fehlgeschlagen", description_placeholders={"fehler": self._fehler})
        return self.async_abort(reason="belegt_programmiert" if self._programmiert else "belegt",
                                description_placeholders={"name": g["name"] if g else ""})

    async def async_step_entfernen(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = self._hub()
        if user_input is not None:
            self._sn = user_input["geraet"]
            return await self.async_step_bestaetigen()
        optionen = [SelectOptionDict(value=g["sn"], label=f"{g['name']} ({g['ia']}, {hub.modell(g)})")
                    for g in sorted(hub.anlage["geraete"], key=lambda g: einlernen.ia_int(g["ia"]))]
        schema = vol.Schema({vol.Required("geraet"): SelectSelector(SelectSelectorConfig(options=optionen))})
        return self.async_show_form(step_id="entfernen", data_schema=schema)

    async def async_step_bestaetigen(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        g = self._geraet()
        if g is None:
            return self.async_abort(reason="nicht_geladen")
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("verstanden"):
                return await self.async_step_laeuft()
            errors["verstanden"] = "nicht_bestaetigt"
        a = einlernen.auswirkungen(self._hub(), g)
        zeilen = []
        if g.get("rolle") == "sender":
            zeilen.append(tr("folge_sender_wecken"))
            if a["aktoren"]:
                zeilen.append(tr("folge_links_geloescht", liste=", ".join(a["aktoren"])))
            if a["tasten"]:
                zeilen.append(tr("folge_tasten_entfallen", liste=", ".join(a["tasten"])))
        else:
            if a["verweise"]:
                zeilen.append(tr("folge_verweise", liste=", ".join(a["verweise"])))
            if a["esp_szenen"]:
                zeilen.append(tr("folge_esp_szenen", liste=", ".join(a["esp_szenen"])))
        if a["szenen"]:
            zeilen.append(tr("folge_szenen", liste=", ".join(a["szenen"])))
        self._name = g["name"]
        schema = vol.Schema({vol.Required("verstanden", default=False): BooleanSelector()})
        return self.async_show_form(step_id="bestaetigen", data_schema=schema, errors=errors,
                                    description_placeholders={
                                        "name": g["name"], "ia": g["ia"], "typ": self._hub().modell(g),
                                        "details": "\n".join(zeilen) or tr("folge_nichts")})

    async def _entfernen(self) -> None:
        try:
            self._bericht = await einlernen.entfernen(self._hub(), self._geraet())
        except BridgeError as err:
            self._fehler = str(err)

    async def async_step_laeuft(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._aufgabe is None:
            self._aufgabe = self.hass.async_create_task(self._entfernen(), "enet Gerät entfernen")
        if not self._aufgabe.done():
            return self.async_show_progress(step_id="laeuft", progress_action="laeuft", progress_task=self._aufgabe)
        return self.async_show_progress_done(next_step_id="fertig")

    async def async_step_fertig(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._bericht is None:
            return self.async_abort(reason="fehlgeschlagen", description_placeholders={"fehler": self._fehler or "?"})
        reg = dr.async_get(self.hass)
        for dev in dr.async_entries_for_config_entry(reg, self.config_entry.entry_id):
            if any(i[0] == DOMAIN and i[1].split("_")[0] == self._sn for i in dev.identifiers):
                reg.async_remove_device(dev.id)  # Gerät und ggf. seine Kanal-Geräte
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        offen = self._bericht["offen"]
        return self.async_abort(reason="entfernt_offen" if offen else "entfernt",
                                description_placeholders={"name": self._name, "offen": "; ".join(offen)})


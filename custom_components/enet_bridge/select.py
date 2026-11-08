"""Auswahl-Parameter der Aktoren (Betriebsart, Lastart, Verhalten bei Netzwiederkehr …).
Sender: Betriebsart und bis zu zwei Lampen je Wippe (vorgemerkt bis „Sender programmieren“) und Aktion je
Einzeltaste (sofort)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_added_domain, async_track_state_removed_domain

from . import EnetConfigEntry, ha_wippe, sender, wippe_info
from .bridge import BridgeError
from .const import DOMAIN
from .entity import EnetEntity, EnetParamEntity, param_entitaeten
from .params import auswahl
from .texte import tr


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(param_entitaeten(hub, EnetParamSelect, lambda p: p.art == "auswahl" and not p.nur_lesen))
    neu = []
    for g in hub.anlage["geraete"]:
        if g.get("rolle") != "sender":
            continue
        try:
            bel = sender.geplante_belegung(g)
        except BridgeError:
            continue  # Belegung unbekannt (Anlage ohne „ziele“): nur über den Dialog
        neu += [WippeBetriebsart(hub, g, n) for n in range(1, sender.wippen_anzahl(hub, g) + 1)]
        neu += [WippeLampe(hub, g, n, i) for n in range(1, sender.wippen_anzahl(hub, g) + 1)
                for i in range(1, sender.lampen_auswahlen(g, n) + 1)]
        neu += [WippeAbdimmen(hub, g, n) for n, b in sorted(bel.items()) if b["art"] == "wippe" and ha_wippe.abdimmen_sinnvoll(hub, b)]
        neu += [TasteAktion(hub, g, k) for n, b in sorted(bel.items()) if b["art"] == "tasten"
                for k in (2 * n - 1, 2 * n)]
    async_add_entities(neu)


class EnetParamSelect(EnetParamEntity, SelectEntity):
    def __init__(self, hub, gruppe, param) -> None:
        super().__init__(hub, gruppe, param)
        self._schluessel = {auswahl(k): k for k in param.typ.get("werte", {})}
        self._attr_options = list(self._schluessel)

    @property
    def current_option(self) -> str | None:
        w = self.wert
        return auswahl(w) if w is not None else None

    async def async_select_option(self, option: str) -> None:
        await self.schreiben(self._schluessel[option])


class WippeBetriebsart(EnetEntity, SelectEntity):
    """Wippe (ohne Lampen = nicht benutzt) / Einzeltasten – wird erst mit „Sender programmieren“ geschrieben."""

    _attr_translation_key = "wippe_betriebsart"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(sender.BETRIEBSARTEN)

    def __init__(self, hub, geraet, n: int) -> None:
        super().__init__(hub)
        self.geraet, self.n = geraet, n
        self._attr_unique_id = f"{geraet['sn']}_wippe{n}_betriebsart"
        self._attr_device_info = wippe_info(hub, geraet, n)

    @property
    def current_option(self) -> str:
        return sender.betriebsart(self.geraet, self.n)

    @property
    def extra_state_attributes(self) -> dict:
        return {"vorgemerkt": str(self.n) in self.geraet.get("geplant", {}),
                "mehrere_lampen": sender.komplex(self.geraet, self.n)}

    async def async_select_option(self, option: str) -> None:
        if option != self.current_option and sender.komplex(self.geraet, self.n):
            raise HomeAssistantError(tr("wippe_komplex", name=self.geraet["name"], n=self.n))
        if option == "tasten" and str(self.n) in self.geraet.get("geplant_lampen", {}):
            sender.lampen_vormerken(self.geraet, self.n, sender.lampen_der_wippe(dict(self.geraet, geplant_lampen={}), self.n))
        geplant = self.geraet.setdefault("geplant", {})
        geplant[str(self.n)] = option
        g = dict(self.geraet, geplant={})
        if sender.betriebsart(g, self.n) == option:  # zurück auf den programmierten Stand
            del geplant[str(self.n)]
        if not geplant:
            self.geraet.pop("geplant")
        await self.hub.anlage_speichern()
        _vorgemerkt(self.hub, self.geraet)
        self.hass.config_entries.async_schedule_reload(self.hub.entry_id)  # Tasten-/Lampen-Auswahl neu




class WippeAbdimmen(EnetEntity, SelectEntity):
    """Nur bei Wippen mit HA-Lampen: Schalter beim Abdimmen ausschalten (wie eNet-Schaltaktoren) oder anlassen.
    Wirkt sofort (nur Home Assistant, nichts am Sender)."""

    _attr_translation_key = "wippe_abdimmen"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["aus", "an"]

    def __init__(self, hub, geraet, n: int) -> None:
        super().__init__(hub)
        self.geraet, self.n = geraet, n
        self._attr_unique_id = f"{geraet['sn']}_wippe{n}_abdimmen"
        self._attr_device_info = wippe_info(hub, geraet, n)

    @property
    def current_option(self) -> str:
        return "an" if ha_wippe.abdimmen_an(self.geraet, self.n) else "aus"

    async def async_select_option(self, option: str) -> None:
        an = set(self.geraet.get("abdimmen_an", [])) - {str(self.n)} | ({str(self.n)} if option == "an" else set())
        if an:
            self.geraet["abdimmen_an"] = sorted(an)
        else:
            self.geraet.pop("abdimmen_an", None)
        await self.hub.anlage_speichern()
        self.async_write_ha_state()


def _vorgemerkt(hub, geraet) -> None:
    if sender.vorgemerkt(geraet):
        sender.aktivitaet(hub, geraet, tr("akt_vorgemerkt"))


class WippeLampe(EnetEntity, SelectEntity):
    """Lampe 1/2 einer Wippe – vorgemerkt bis „Sender programmieren“. Hat die Wippe mehr Lampen (aus dem
    Dialog), zeigt sie „Mehrere“ und lässt sich hier nicht ändern."""

    _attr_translation_key = "wippe_lampe"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def KEINE(self) -> str:  # noqa: N802 – Anzeige in der eingestellten Sprache
        return tr("lampe_keine")

    @property
    def MEHRERE(self) -> str:  # noqa: N802
        return tr("lampe_mehrere")

    def __init__(self, hub, geraet, n: int, i: int) -> None:
        super().__init__(hub)
        self.geraet, self.n, self.i = geraet, n, i
        self._attr_unique_id = f"{geraet['sn']}_wippe{n}_lampe{i}"
        self._attr_translation_placeholders = {"lampe": str(i)}
        self._attr_device_info = wippe_info(hub, geraet, n)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # HA liefert die Auswahlliste nur mit dem Zustand: kommt eine Lampe einer anderen Integration hinzu (z. B. ein
        # neu eingebundener Shelly) oder fällt weg, Zustand neu schreiben – sonst fehlt sie bis zum Neuladen
        self.async_on_remove(async_track_state_added_domain(self.hass, ha_wippe.DOMAENEN, self._lampen_geaendert))
        self.async_on_remove(async_track_state_removed_domain(self.hass, ha_wippe.DOMAENEN, self._lampen_geaendert))

    @callback
    def _lampen_geaendert(self, _event: Event) -> None:
        self.async_write_ha_state()

    def _kanaele(self) -> dict[str, str]:
        """Anzeige → Lampen-Schlüssel: eNet-Kanäle, dann Lichter/Schalter anderer Integrationen (HA schaltet)."""
        ha = ha_lampen(self.hass)
        namen = list(ha.values())
        # kurz „Name (HA)“, damit die Auswahl auf die Karte passt; nur bei doppeltem Namen mit entity_id
        return {**{f"{k.geraet['name']} K{k.nr} ({k.key})": k.key for k in self.hub.kanaele},
                **{tr("lampe_ha_eid" if namen.count(name) > 1 else "lampe_ha", name=name, eid=eid): eid
                   for eid, name in ha.items()}}

    @property
    def options(self) -> list[str]:
        if sender.komplex(self.geraet, self.n):
            return [self.MEHRERE]
        return [self.KEINE, *self._kanaele()]

    @property
    def current_option(self) -> str | None:
        if sender.komplex(self.geraet, self.n):
            return self.MEHRERE
        lampen = sender.lampen_der_wippe(self.geraet, self.n)
        if len(lampen) < self.i:
            return self.KEINE
        return next((a for a, k in self._kanaele().items() if k == lampen[self.i - 1]), None)

    @property
    def extra_state_attributes(self) -> dict:
        return {"vorgemerkt": str(self.n) in self.geraet.get("geplant_lampen", {}),
                "mehrere_lampen": sender.komplex(self.geraet, self.n)}

    async def async_select_option(self, option: str) -> None:
        if sender.komplex(self.geraet, self.n):
            raise HomeAssistantError(tr("wippe_komplex", name=self.geraet["name"], n=self.n))
        lampen = sender.lampen_der_wippe(self.geraet, self.n)
        neu = [*lampen[:self.i - 1], *([self._kanaele()[option]] if option != self.KEINE else []), *lampen[self.i:]]
        sender.lampen_vormerken(self.geraet, self.n, neu)
        await self.hub.anlage_speichern()
        _vorgemerkt(self.hub, self.geraet)
        self.hass.config_entries.async_schedule_reload(self.hub.entry_id)  # Lampe 2 rückt ggf. auf


def ha_lampen(hass: HomeAssistant) -> dict[str, str]:
    """Lichter und Schalter, die nicht von dieser Integration stammen: entity_id → Name."""
    reg = er.async_get(hass)
    out = {}
    for st in sorted(hass.states.async_all(ha_wippe.DOMAENEN), key=lambda st: st.name.lower()):
        e = reg.async_get(st.entity_id)
        if e is None or (e.platform != DOMAIN and not e.disabled and not e.hidden and e.entity_category is None):
            out[st.entity_id] = st.name
    return out


class TasteAktion(EnetEntity, SelectEntity):
    """Was die Einzeltaste auslöst (Server-Taste in der Bridge): sofort wirksam, ohne Programmieren.
    Ohne Kategorie, damit die Belegung auf der Geräteseite getrennt von der Konfiguration (Betriebsart) steht."""

    _attr_translation_key = "taste_aktion"

    def __init__(self, hub, geraet, k: int) -> None:
        super().__init__(hub)
        self.geraet, self.k = geraet, k
        self._attr_unique_id = f"{geraet['sn']}_taste{k}_aktion"
        self._attr_translation_placeholders = {"nr": str(k)}
        self._attr_device_info = wippe_info(hub, geraet, (k + 1) // 2)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._listen("ha_szenen")  # HA lädt die Auswahlliste nur mit dem Zustand: nach dem Einlesen neu schreiben

    def _optionen(self) -> dict[str, str]:
        return {anzeige: wert for wert, anzeige in sender.aktion_optionen(self.hub)}

    @property
    def options(self) -> list[str]:
        return list(self._optionen())

    @property
    def current_option(self) -> str | None:
        wert = sender.aktion_der_taste(self.hub, self.geraet, self.k)
        return next((a for a, w in self._optionen().items() if w == wert), None)

    async def async_select_option(self, option: str) -> None:
        try:
            await sender.aktion_setzen(self.hub, self.geraet, self.k, self._optionen()[option])
        except BridgeError as err:
            raise HomeAssistantError(tr("taste_fehler", name=self.geraet["name"], k=self.k, err=err)) from err
        self.async_write_ha_state()

"""Aktor: Parameter neu vom Gerät lesen. eNet-Szene: aktuellen Zustand als Szenenwerte speichern.
Sender: vorgemerkte Belegung programmieren. (Frequenzkorrektur: automatisch, hub._nachfuehren.)"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


from . import EnetConfigEntry, geraet_info, sender, szenen_info, wippe_info
from .bridge import BridgeError
from .entity import EnetEntity
from .texte import tr


async def async_setup_entry(hass: HomeAssistant, entry: EnetConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    geraete = {g.geraet["sn"]: g.geraet for g in hub.param_gruppen}
    sender_ = [g for g in hub.anlage["geraete"] if g.get("rolle") == "sender"]
    async_add_entities([ParamLesen(hub, g) for g in geraete.values()] +
                       [SzeneSpeichern(hub, s) for s in hub.szenen.szenen if s.art != "ha"] +
                       [SenderProgrammieren(hub, g) for g in sender_] +
                       [SenderProgrammieren(hub, g, n) for g in sender_ if (w := sender.wippen_anzahl(hub, g)) > 1
                        for n in range(1, w + 1)])


class ParamLesen(EnetEntity, ButtonEntity):
    _attr_translation_key = "parameter_lesen"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, geraet) -> None:
        super().__init__(hub)
        self.geraet = geraet
        self._attr_unique_id = f"{geraet['sn']}_parameter_lesen"
        self._attr_device_info = geraet_info(hub, geraet)

    async def async_press(self) -> None:
        try:
            await self.hub.parameter_lesen(self.geraet)
        except BridgeError as err:
            raise HomeAssistantError(f"{self.geraet['name']}: {err}") from err


class SzeneSpeichern(EnetEntity, ButtonEntity):
    """Lernbefehl: Die Mitglieder übernehmen ihren aktuellen Zustand als Werte dieser Szene."""

    _attr_translation_key = "szene_speichern"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, szene) -> None:
        super().__init__(hub)
        self.szene = szene
        self._attr_unique_id = f"{hub.entry_id}_szene_{szene.id}_speichern"
        self._attr_translation_placeholders = {"szene": szene.name}
        self._attr_device_info = szenen_info(hub)

    async def async_press(self) -> None:
        try:
            await self.hub.szenen.lernen(self.szene)
        except BridgeError as err:
            raise HomeAssistantError(tr("szene_fehler", name=self.szene.name, err=err)) from err


class SenderProgrammieren(EnetEntity, ButtonEntity):
    """Vorgemerkte Betriebsarten (und die übrige Belegung) in den Sender schreiben. Wartet bis zum Ende, damit
    ein Fehler als Meldung in der Oberfläche erscheint; „Taste am Sender drücken“ steht in der Aktivität.
    Zusätzlich auf jeder Wippen-Seite (schreibt ebenfalls den ganzen Sender)."""

    _attr_translation_key = "sender_programmieren"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, geraet, wippe: int | None = None) -> None:
        super().__init__(hub)
        self.geraet = geraet
        if wippe is None:
            self._attr_unique_id = f"{geraet['sn']}_sender_programmieren"
            self._attr_device_info = geraet_info(hub, geraet)
        else:
            self._attr_unique_id = f"{geraet['sn']}_wippe{wippe}_sender_programmieren"
            self._attr_device_info = wippe_info(hub, geraet, wippe)

    async def async_press(self) -> None:
        # Zweiter Druck (auch über den Knopf einer Wippen-Seite) während eines Laufs: ablehnen, nicht einreihen –
        # sonst wartet er auf den Funk und wird vom Neuladen am Ende des ersten Laufs abgeschnitten.
        if self.geraet["sn"] in self.hub.sender_aktiv:
            raise HomeAssistantError(tr("programmierung_laeuft_knopf", name=self.geraet["name"]))
        try:
            await sender.geplant_programmieren(self.hub, self.geraet)
        except BridgeError as err:
            raise HomeAssistantError(f"{self.geraet['name']}: {err}") from err
        finally:
            self.hub.neu_laden_nach_funk()

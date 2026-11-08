"""Wippe → HA-Lampen: Home Assistant schaltet und dimmt Lampen anderer Integrationen wie ein eNet-Aktor.

Die Wippe sendet beim Drücken und Loslassen ihren Tastenwert (0x40xx linke Taste = ein/heller, 0x80xx rechte =
aus/dunkler, untere 14 Bit Haltedauer in 10 ms; 0 = gedrückt). Kurz drücken schaltet beim Loslassen, gehalten
(≥ DIMMEN_AB_S) dimmt in Schritten, bis losgelassen wird. Geht das Loslassen verloren, endet das Dimmen nach
DIMMEN_MAX_S; geht das Drücken verloren, zählt die Haltedauer im Loslassen.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_SUPPORTED_COLOR_MODES, ColorMode
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON

from .const import ART_DIMMEN

if TYPE_CHECKING:
    from .hub import Hub, Taste

_LOGGER = logging.getLogger(__name__)

DIMMEN_AB_S = 0.5      # so lange gehalten: dimmen statt schalten
DIMMEN_TAKT_S = 0.3    # Abstand der Dimmschritte
DIMMEN_SCHRITT = 7     # Prozent je Schritt (0 → 100 % in ≈ 4,5 s)
DIMMEN_MAX_S = 10.0    # Sicherheit, falls das Loslassen nicht ankommt
MIN_HELLIGKEIT = 3     # nicht unter diese Helligkeit (0–255) dimmen: Dunkler schaltet nicht aus (wie eNet)
DOMAENEN = ("light", "switch")


def abdimmen_an(geraet: dict, n: int) -> bool:
    """Einstellung je Wippe: Schalter (nicht dimmbare HA-Lampen) bleiben beim Abdimmen an. Steht beim Gerät, nicht
    im Tasten-Eintrag, damit sie das Neuprogrammieren des Senders übersteht. Standard: aus wie ein eNet-Schaltaktor."""
    return str(n) in geraet.get("abdimmen_an", [])


def dimmbar(hass, eid: str, unbekannt: bool = False) -> bool:
    """HA-Lampe dimmbar? Ohne Zustand (z. B. beim Start, Integration noch nicht geladen) gilt „unbekannt“."""
    if not eid.startswith("light."):
        return False
    if (st := hass.states.get(eid)) is None:
        return unbekannt
    modi = st.attributes.get(ATTR_SUPPORTED_COLOR_MODES) or []
    return any(m not in (ColorMode.ONOFF, ColorMode.UNKNOWN) for m in modi)


def abdimmen_sinnvoll(hub: Hub, belegung: dict) -> bool:
    """Auswahl „Schalter beim Abdimmen“ nur, wenn an der Wippe ein HA-Schalter UND etwas Dimmbares hängt (eNet-Dimmer
    oder dimmbare HA-Lampe) – sonst gibt es kein Abdimmen bzw. keinen Schalter, auf den sie wirkt."""
    ha = belegung.get("ha_ziele") or []
    if not any(not dimmbar(hub.hass, e, unbekannt=True) for e in ha):
        return False
    enet_dimmer = any(k.art == ART_DIMMEN for z in belegung.get("ziele") or []
                      for k in hub.kanaele if k.ia == z["ia"] and k.nr == z["ch"])
    return enet_dimmer or any(dimmbar(hub.hass, e, unbekannt=True) for e in ha)


def ha_ziele(taste: Taste) -> list[str]:
    eintrag = next((t for t in taste.geraet.get("tasten", []) if t["nr"] == taste.nr), {})
    return list(eintrag.get("ha_ziele", []))


class HaWippe:
    def __init__(self, hub: Hub) -> None:
        self.hub, self.hass = hub, hub.hass
        self._laeuft: dict[str, tuple[asyncio.Task, bool]] = {}  # Taste → (Druck-Ablauf, links)
        self._dimmt: set[str] = set()  # Tasten, deren Druck schon dimmt (Loslassen schaltet dann nicht)

    def stop(self) -> None:
        for task, _ in self._laeuft.values():
            task.cancel()
        self._laeuft.clear()

    def tastendruck(self, taste: Taste, msg: dict[str, Any]) -> None:
        ziele = [e for e in ha_ziele(taste) if e.split(".")[0] in DOMAENEN]
        if not ziele:
            return
        cmd = int(msg.get("cmd", "0") or "0", 16)
        richtung = cmd & 0xC000
        if richtung not in (0x4000, 0x8000):
            return  # beide Tasten (Einschalthelligkeit speichern) – betrifft nur eNet-Dimmer
        links = richtung == 0x4000
        halten = (cmd & 0x3FFF) * 10
        laufend = self._laeuft.pop(taste.key, None)
        if halten == 0:  # Drücken
            if laufend:
                laufend[0].cancel()
                self._dimmt.discard(taste.key)
            anlassen = abdimmen_an(taste.geraet, (taste.nr + 1) // 2)
            task = self.hass.async_create_background_task(self._gedrueckt(taste.key, ziele, links, anlassen),
                                                          "enet Wippe HA-Lampen")
            self._laeuft[taste.key] = (task, links)
            return
        # Loslassen: noch in der Wartezeit = kurz gedrückt; dimmt es schon, hört es auf
        if laufend and not laufend[0].done():
            laufend[0].cancel()
            if laufend[1] == links and taste.key not in self._dimmt:
                self._schalten(ziele, links)
        elif laufend is None and halten < DIMMEN_AB_S * 1000:
            self._schalten(ziele, links)  # Drücken nicht empfangen

    def _schalten(self, ziele: list[str], ein: bool) -> None:
        self.hass.async_create_background_task(
            self.hass.services.async_call("homeassistant", "turn_on" if ein else "turn_off", {ATTR_ENTITY_ID: ziele}),
            "enet Wippe schalten")

    async def _gedrueckt(self, key: str, ziele: list[str], heller: bool, anlassen: bool = False) -> None:
        await asyncio.sleep(DIMMEN_AB_S)
        task = asyncio.current_task()
        self._dimmt.add(key)  # ab hier: Loslassen beendet nur das Dimmen
        dimmbare = [e for e in ziele if dimmbar(self.hass, e)]
        schalter = [e for e in ziele if e not in dimmbare]
        try:
            # Schalter: Aufdimmen schaltet ein; Abdimmen schaltet aus wie ein eNet-Schaltaktor – außer „anlassen“
            if schalter and (heller or not anlassen):
                self._schalten(schalter, heller)
            if not dimmbare:
                return
            for _ in range(int(DIMMEN_MAX_S / DIMMEN_TAKT_S)):
                if heller:
                    schritt = dimmbare
                else:  # dunkler nur, solange die Lampe an ist und über der Mindesthelligkeit liegt
                    schritt = [e for e in dimmbare if (st := self.hass.states.get(e)) is not None
                               and st.state == STATE_ON
                               and (st.attributes.get(ATTR_BRIGHTNESS) or 0) > MIN_HELLIGKEIT + DIMMEN_SCHRITT * 255 / 100]
                if not schritt:
                    return
                await self.hass.services.async_call(
                    "light", "turn_on", {ATTR_ENTITY_ID: schritt,
                                         "brightness_step_pct": DIMMEN_SCHRITT if heller else -DIMMEN_SCHRITT})
                await asyncio.sleep(DIMMEN_TAKT_S)
        except Exception:  # noqa: BLE001 – eine fremde Lampe darf die Tastenverarbeitung nicht stören
            _LOGGER.exception("Dimming %s failed", dimmbare)
        finally:
            self._dimmt.discard(key)
            if self._laeuft.get(key, (None,))[0] is task:
                self._laeuft.pop(key, None)

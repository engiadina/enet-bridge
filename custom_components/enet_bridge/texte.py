"""Feste Texte der Integration auf Deutsch und Englisch.

Für alles, was Home Assistant nicht selbst übersetzt: Einträge in der Aktivität (Logbuch), Fehlermeldungen in
der Oberfläche, dynamische Auswahllisten, Parameternamen. Texte in Einrichtung, Entitäten, Diensten und
Reparaturen stehen wie üblich in strings.json / translations/*.json.

Die Sprache kommt aus der HA-Einstellung (setzen() beim Einrichten des Eintrags); alles außer Deutsch → Englisch.
Log-Meldungen (_LOGGER) sind Englisch und werden nicht übersetzt.

Neue Texte: Schlüssel hier eintragen, (Deutsch, Englisch), Platzhalter im str.format-Stil.
"""
from __future__ import annotations

_DE = True  # bis setzen() aufgerufen ist (Tests, Werkzeuge): Deutsch


def setzen(sprache: str | None) -> None:
    """Sprache aus hass.config.language übernehmen."""
    global _DE
    _DE = (sprache or "").lower().startswith("de")


def deutsch() -> bool:
    return _DE


def tr(schluessel: str, **werte: object) -> str:
    """Text in der eingestellten Sprache, Platzhalter ersetzt."""
    de, en = TEXTE[schluessel]
    return (de if _DE else en).format(**werte)


TEXTE: dict[str, tuple[str, str]] = {
    # --- Sender programmieren / Tasten ---------------------------------------------------------------
    "wippe_ziele_unbekannt": ("{name} Wippe {n}: Ziele unbekannt (Anlage neu importieren)",
                              "{name} rocker {n}: targets unknown (import the installation again)"),
    "links_reset_unbestaetigt": ("Kanal {kanal}: Links zurücksetzen nicht bestätigt",
                                 "Channel {kanal}: link reset not confirmed"),
    "nicht_aufgewacht": ("{name} nicht aufgewacht (Taste am Sender drücken)",
                         "{name} did not wake up (press a button on the transmitter)"),
    "ziel_fehlt": ("Ziel {ia}/{ch} nicht in der Anlage", "Target {ia}/{ch} is not part of the installation"),
    "werkseintrag_fehlt": ("Werkseintrag des Senders nicht gefunden", "Factory entry of the transmitter not found"),
    "zentral_unbekannt": ("{name}: Ziele des Zentralkanals unbekannt (Anlage neu importieren)",
                          "{name}: targets of the central channel unknown (import the installation again)"),
    "keine_antwort_ga": ("Taste {k}: keine Antwort-GA", "Button {k}: no reply group address"),
    "zweck_verknuepfungen": ("Verknüpfungen mit {name}", "links with {name}"),
    "linktabelle_unlesbar": ("Link-Tabelle von {key} nicht lesbar ({err})", "Link table of {key} not readable ({err})"),
    "programmierung_laeuft": ("Programmierung läuft bereits – bitte warten, bis sie beendet ist",
                              "Programming is already running – please wait until it has finished"),
    "programmierung_laeuft_knopf": ("{name}: Programmierung läuft bereits – bitte warten, bis „Programmierung "
                                    "beendet“ in der Aktivität steht",
                                    "{name}: programming is already running – please wait until the activity shows "
                                    "“Programming finished”"),
    "akt_programmierung_fehlgeschlagen": ("– Programmierung fehlgeschlagen: {err}", "– programming failed: {err}"),
    "akt_eingelernt_taste": ("– eingelernt, falls die Lern-LED nicht mehr blinkt: eine Taste drücken",
                             "– paired; if the learn LED has stopped flashing: press a button"),
    "akt_sender_wecken": ("– am Sender bitte eine Taste drücken (aufwecken), danach etwa eine Minute nicht weglegen",
                          "– please press a button on the transmitter (wake it up), then keep it nearby for about "
                          "a minute"),
    "akt_sender_aktiv": ("– Sender ist aktiv, wird programmiert", "– transmitter is awake, programming"),
    "akt_links_unvollstaendig": ("– Sender programmiert, aber Aktor-Verknüpfungen nicht vollständig abgeglichen "
                                 "({err}) – erneut programmieren",
                                 "– transmitter programmed, but actuator links not fully updated ({err}) – "
                                 "program again"),
    "akt_programmierung_beendet": ("– Programmierung beendet", "– programming finished"),
    "aktion_melden": ("Nur melden (Automation in Home Assistant)", "Report only (automation in Home Assistant)"),
    "aktion_szene": ("Szene: {name}", "Scene: {name}"),
    "aktion_enet_szene": ("eNet-Szene: {name}", "eNet scene: {name}"),
    "aktion_bridge_szene": ("Bridge-Szene: {name}", "Bridge scene: {name}"),
    "taste_name": ("{name} Taste {k}", "{name} button {k}"),
    # --- Dienste, Geräte löschen, Einrichten -------------------------------------------------------------
    "fremdes_funkmodul_start": ("{port} ist ein Funkmodul einer anderen Integration (Thread/Zigbee/Z-Wave) – Port "
                                "der Bridge über „Neu konfigurieren“ ändern",
                                "{port} is the radio of another integration (Thread/Zigbee/Z-Wave) – change the "
                                "bridge port via “Reconfigure”"),
    "keine_anlage": ("Keine eNet-Anlage geladen", "No eNet installation loaded"),
    "akt_einlernen_fehlgeschlagen": ("– Einlernen fehlgeschlagen: {err}", "– pairing failed: {err}"),
    "einlernen_fehler": ("Einlernen: {err}", "Pairing: {err}"),
    "kein_geraet": ("Kein eNet-Gerät (Aktor oder Sender) ausgewählt", "No eNet device (actuator or transmitter) selected"),
    "kein_enet_geraet": ("Kein eNet-Gerät ausgewählt", "No eNet device selected"),
    "kein_sender": ("{name} ist kein Sender", "{name} is not a transmitter"),
    "nicht_bridge_entitaet": ("{entity_id} gehört nicht zur eNet-Bridge ({domain})",
                              "{entity_id} does not belong to the eNet bridge ({domain})"),
    "bridge_nicht_geladen": ("{entity_id}: eNet-Bridge nicht geladen", "{entity_id}: eNet bridge not loaded"),
    "lampen_eine_bridge": ("Alle Lampen müssen an derselben eNet-Bridge hängen",
                           "All lights must belong to the same eNet bridge"),
    "kanal_fehlt": ("{entity_id}: Kanal nicht in der Anlage", "{entity_id}: channel is not part of the installation"),
    "szene_fehler": ("Szene {name}: {err}", "Scene {name}: {err}"),
    "szene_unbekannt": ("{entity_id}: Szene unbekannt", "{entity_id}: unknown scene"),
    "akt_szene_geloescht_offen": ("– Szene {name} gelöscht, aber nicht erreichbar: {geraete} (dort reagiert die "
                                  "Szene weiter, bis die Verknüpfung entfernt wird)",
                                  "– scene {name} deleted, but not reachable: {geraete} (the scene still works "
                                  "there until the link is removed)"),
    "loeschen_bridge_szenen": ("Bridge und Szenen lassen sich nicht löschen.", "The bridge and scenes cannot be deleted."),
    "loeschen_nicht_verbunden": ("Die Bridge ist nicht verbunden – das Gerät kann jetzt nicht zurückgesetzt werden.",
                                 "The bridge is not connected – the device cannot be reset now."),
    "loeschen_kanal": ("Ein einzelner Kanal lässt sich nicht löschen – bitte das Gerät „{name}“ löschen.",
                       "A single channel cannot be deleted – please delete the device “{name}”."),
    "nicht_geloescht": ("{name} wurde nicht gelöscht: {err}", "{name} was not deleted: {err}"),
    "wippe_geraet": ("Wippe {n}", "Rocker {n}"),
    # --- Aktivität beim Programmieren von Aktoren, Sensoren -------------------------------------------
    "akt_wird_programmiert": ("– {zweck} wird programmiert", "– programming {zweck}"),
    "akt_programmiert": ("– {zweck} programmiert", "– {zweck} programmed"),
    "akt_programmierung_fehler": ("– {zweck}: Programmierung fehlgeschlagen ({fehler})",
                                  "– {zweck}: programming failed ({fehler})"),
    "zweck_szenen_links": ("Szenen-Verknüpfungen", "scene links"),
    "kein_aktor": ("{name} ist kein Aktor (nur Aktoren haben eine Link-Tabelle)",
                   "{name} is not an actuator (only actuators have a link table)"),
    "nur_mithoeren_lesen": ("„Nur mithören“ ist an – die Bridge sendet nichts, auch keine Leseanfragen",
                            "\"Listen only\" is on – the bridge sends nothing, not even read requests"),
    "links_noch_da": ("{name}: {n} Einträge nach dem Löschen noch vorhanden", "{name}: {n} entries still present after deleting"),
    "zweck_alt_links": ("Alt-Verknüpfungen entfernen", "removing old links"),
    # --- Link-Tabelle (Reparatur „auffällige Verknüpfungen“) ---
    "link_zeile": ("Kanal {ch}, GO {go} ← GA {ga}: {grund}", "channel {ch}, GO {go} ← GA {ga}: {grund}"),
    "link_kein_wippen_link": ("von {sender}, keine Wippen-Verknüpfung", "from {sender}, not a rocker link"),
    "link_keine_wippe": ("von {sender} Wippe {wippe}, die ist aber keine Wippe (Einzeltasten oder unbelegt)",
                         "from {sender} rocker {wippe}, which is not a rocker (single buttons or unassigned)"),
    "link_wippe_ohne_kanal": ("von {sender} Wippe {wippe}, die diese Lampe nicht schaltet",
                              "from {sender} rocker {wippe}, which does not switch this lamp"),
    "link_szene_ohne_kanal": ("Szene {szene}, in der diese Lampe nicht ist",
                              "scene {szene}, which does not contain this lamp"),
    "link_keine_szene": ("vom Server, zu keiner Szene", "from the server, no scene"),
    "link_fremd": ("von Gerät {sn}, das nicht in der Anlage ist", "from device {sn}, not part of the installation"),
    "zweck_einstellung": ("Einstellung {name}", "setting {name}"),
    "zweck_szenenwert": ("Szenenwert {name}", "scene value {name}"),
    "zweck_parameterspeicher": ("Parameterspeicher", "parameter memory"),
    "akt_sensor_wecken": ("– am Sensor bitte eine Testtaste drücken (aufwecken)",
                          "– please press a test button on the sensor (wake it up)"),
    "akt_sensor_wach": ("– Sensor ist wach", "– sensor is awake"),
    "keine_bridge_port": ("{port} ist keine Bridge (Funkmodul einer anderen Integration)",
                          "{port} is not a bridge (radio of another integration)"),
    # --- Einlernen / Entfernen --------------------------------------------------------------------------
    "akt_einlernen_start": ("– Einlernen: Gerät jetzt in den Lernmodus versetzen (Mini-Aktoren: Taste am Gerät 4 s "
                            "drücken, Lernmodus 1 min), Suche läuft 2 Minuten",
                            "– pairing: put the device into learn mode now (mini actuators: press the button on "
                            "the device for 4 s, learn mode lasts 1 min), searching for 2 minutes"),
    "schon_in_anlage": ("gehört schon zu dieser Anlage ({name}, {ia})", "already belongs to this installation ({name}, {ia})"),
    "andere_anlage": ("gehört zu einer anderen Anlage (Domäne {domaene}) – dort entfernen oder am Gerät auf "
                      "Werkszustand zurücksetzen",
                      "belongs to another installation (domain {domaene}) – remove it there or reset the device "
                      "to factory settings"),
    "nichts_gefunden": ("kein Gerät im Werkszustand im Lernmodus gefunden",
                        "no device in factory state and learn mode found"),
    "uebergangen": ("; übergangen: {liste}", "; skipped: {liste}"),
    "typ_unbekannt": ("Gerätetyp unbekannt (SN {sn}, Produktnummer {produkt}) – nichts geändert",
                      "Unknown device type (SN {sn}, product number {produkt}) – nothing changed"),
    "domaene_nicht_gesetzt": ("Domäne nicht gesetzt ({wert})", "Domain not set ({wert})"),
    "keine_antwort": ("keine Antwort", "no answer"),
    "adresse_unbestaetigt": ("Adresse nicht bestätigt", "Address not confirmed"),
    "akt_eingelernt": ("– {name} ({typ}) als {ia} eingelernt", "– {name} ({typ}) paired as {ia}"),
    "verweis_aktor_wippe": ("{aktor} K{ch} (Wippe {n})", "{aktor} ch{ch} (rocker {n})"),
    "verweis_wippe": ("{name} Wippe {n}", "{name} rocker {n}"),
    "verweis_zentral": ("{name} Zentralkanal", "{name} central channel"),
    "akt_entfernen_wecken": ("– {name} entfernen: jetzt eine Taste am Sender drücken (aufwecken)",
                             "– removing {name}: press a button on the transmitter now (wake it up)"),
    "reset_unbestaetigt": ("Gerät hat das Zurücksetzen nicht bestätigt (ggf. am Gerät auf Werkszustand setzen)",
                           "The device did not confirm the reset (reset it to factory settings on the device if needed)"),
    "links_nicht_geloescht": ("Aktor-Verknüpfungen nicht vollständig gelöscht: {err}", "Actuator links not fully deleted: {err}"),
    "config_nicht_uebertragen": ("Bridge-Konfiguration nicht übertragen: {err}", "Bridge configuration not transferred: {err}"),
    "akt_entfernt": ("– {name} aus der Anlage genommen", "– {name} removed from the installation"),
    "akt_entfernt_offen": ("– {name} aus der Anlage genommen, offen: {offen}",
                           "– {name} removed from the installation, open: {offen}"),
    # --- HA-Szenen ---------------------------------------------------------------------------------------
    "akt_szene_uebertragen": ("– wird in die eNet-Aktoren übertragen", "– transferring to the eNet actuators"),
    "akt_szene_uebertragen_ok": ("– Übertragung beendet", "– transfer finished"),
    "akt_szene_uebertragen_teil": ("– Übertragung unvollständig: {fehler}", "– transfer incomplete: {fehler}"),
    "links_in_nicht_geloescht": ("{name}: Links in {wo} nicht gelöscht", "{name}: links in {wo} not deleted"),
    "akt_szene_entfernt_offen": ("– eNet-Teil der Szene {name} entfernt, aber nicht erreichbar: {geraete} (dort "
                                 "reagiert die Szene weiter)",
                                 "– eNet part of scene {name} removed, but not reachable: {geraete} (the scene "
                                 "still works there)"),
    "akt_szenen_uebernommen": ("– als Home-Assistant-Szenen übernommen (Einstellungen → Automationen & Szenen → "
                               "Szenen): {namen}",
                               "– taken over as Home Assistant scenes (Settings → Automations & scenes → Scenes): "
                               "{namen}"),
    # --- Konfigurieren-Dialoge -------------------------------------------------------------------------
    "unbekannt": ("unbekannt", "unknown"),
    "folge_sender_wecken": ("- Sender: nach „Absenden“ eine Taste am Sender drücken (aufwecken)",
                            "- Transmitter: after “Submit” press a button on the transmitter (wake it up)"),
    "folge_links_geloescht": ("- Verknüpfungen in den Aktoren werden gelöscht: {liste}",
                              "- Links in the actuators are deleted: {liste}"),
    "folge_tasten_entfallen": ("- Server-Tasten der Bridge entfallen: {liste}", "- Server buttons of the bridge are removed: {liste}"),
    "folge_verweise": ("- Sender, die darauf zeigen (Wippe wird frei, Link im Sender bleibt bis zum nächsten "
                       "Programmieren): {liste}",
                       "- Transmitters pointing to it (the rocker becomes free, the link in the transmitter stays "
                       "until it is programmed next): {liste}"),
    "folge_esp_szenen": ("- Lokale Szenen der Bridge verlieren den Befehl: {liste}",
                         "- Local scenes of the bridge lose the command: {liste}"),
    "folge_szenen": ("- Fällt aus den eNet-Szenen: {liste}", "- Removed from the eNet scenes: {liste}"),
    # --- Geräteseite des Senders, Bridge, Szenen ------------------------------------------------------
    "wippe_komplex": ("{name} Wippe {n} hat mehr als zwei Lampen – nur unter „eNet → Konfigurieren → Sender belegen“ "
                      "ändern, sonst gingen Verknüpfungen verloren.",
                      "{name} rocker {n} has more than two lights – change it only under “eNet → Configure → "
                      "Assign transmitter”, otherwise links would be lost."),
    "akt_vorgemerkt": ("– Änderung vorgemerkt, zum Übernehmen „Sender programmieren“ drücken",
                       "– change noted, press “Program transmitter” to apply it"),
    "lampe_keine": ("Keine", "None"),
    "lampe_ha": ("{name} (HA)", "{name} (HA)"),
    "lampe_ha_eid": ("{name} (HA, {eid})", "{name} (HA, {eid})"),
    "lampe_mehrere": ("Mehrere (unter Konfigurieren → Sender belegen)", "Several (under Configure → Assign transmitter)"),
    "taste_fehler": ("{name} Taste {k}: {err}", "{name} button {k}: {err}"),
    "bridge_nicht_verbunden": ("Bridge nicht verbunden", "Bridge not connected"),
    "befehl_zu_lang": ("Befehl länger als 512 Byte", "Command longer than 512 bytes"),
    "mithoeren_sendet_nicht": ("„Nur mithören“ ist eingeschaltet – die Bridge sendet nicht",
                               "“Listen only” is on – the bridge does not transmit"),
    "keine_antwort_auf": ("keine Antwort auf {cmd}", "no answer to {cmd}"),
    "kein_ergebnis": ("kein Ergebnis für {cmd}", "no result for {cmd}"),
    "config_unbestaetigt": ("Konfiguration nicht bestätigt", "Configuration not confirmed"),
    "keine_szenennummer": ("keine freie Szenennummer mehr in diesen Aktoren", "no free scene number left in these actuators"),
    "szenenspeicher_voll": ("keine freie Szenennummer mehr (Szenenspeicher der Aktoren voll)",
                            "no free scene number left (scene memory of the actuators full)"),
    "keine_mitglieder": ("keine Mitglieder", "no members"),
    "wert_laenge": ("Wert {wert} muss 1–4 Byte haben", "Value {wert} must have 1–4 bytes"),
    "mithoeren_fehler": ("Nur mithören: {err}", "Listen only: {err}"),
    "keine_enet_anlage": ("keine eNet-Anlage (geraete/server fehlt)", "not an eNet installation (geraete/server missing)"),
    "esptool_ende": ("esptool beendet mit {rc}", "esptool exited with {rc}"),
    "keine_bridge_am_port": ("Am Port antwortet keine eNet-Bridge. Installieren überschreibt das angeschlossene ESP32.",
                             "No eNet bridge answers on the port. Installing overwrites the connected ESP32."),
    "firmware_fehler": ("Bridge-Firmware: {err}", "Bridge firmware: {err}"),
    # --- Parameter --------------------------------------------------------------------------------------
    "verify_fehler": ("{name}: geschrieben {neu}, gelesen {gelesen}", "{name}: written {neu}, read {gelesen}"),
    "zweck_verknuepfungen_kurz": ("Verknüpfungen", "links"),
    "folge_nichts": ("- sonst nichts betroffen", "- nothing else affected"),
    "verbindung_verloren": ("Verbindung verloren", "Connection lost"),
    "entfernen_fehler": ("Entfernen: {err}", "Removing: {err}"),
    "server_funktionen_fehler": ("Server-Funktionen: {err}", "Server functions: {err}"),
    "geraet_szenen": ("eNet-Szenen", "eNet scenes"),
}

# Roadmap

Stand: 2026-11-07 (Integration 0.11.0, Firmware 0.8.11). Reihenfolge innerhalb der Abschnitte = ungefähre
Priorität. Erledigtes wird hier gestrichen.

## Vor der ersten Veröffentlichung
- Kompletter Test „Neue Anlage ohne eNet Server“: Integration als neue Anlage einrichten, einen Handsender
  einlernen, Wippen belegen, Einzeltasten mit Aktion, Sender programmieren, Ereignisse in HA.
- Bereinigte Protokoll-Beschreibung (Funk, Telegramme, Programmier-Rahmen, Szenen, Sender) als Doku.
- Englische Oberfläche in HA testen, Begriffe in en.json vereinheitlichen (sender/transmitter, „teach in“).
- Fotos des Aufbaus für docs/HARDWARE.md.

## Veröffentlichung und Bekanntmachung
- Projekt auf GitHub mit Hardware-Beschreibung (README, docs/HARDWARE.md, Teileliste, Fotos, ggf. Platine/Gehäuse).
- Bekanntmachen: Home-Assistant-Community-Forum („Share your Projects“), Reddit r/homeassistant, deutschsprachige
  Foren (z. B. simon42-Community, KNX-User-Forum), Nutzer der bestehenden eNet-Server-Integration
  (mnordseth/enet-homeassistant), später Aufnahme in die HACS-Standardliste.

## Geräte
- Fehlende Komponenten: Jalousieaktoren (Belegung aus dem Katalog bekannt, ungetestet), REG-Aktoren/-Module,
  Bewegungsmelder, Energiesensoren. Je Typ: Gerät + Mitschnitt (am besten mit echtem Server).
- Verschlüsselte Anlagen (eNet Server 2.x mit AES) – zurückgestellt.

## Bridge
- Anbindung über Thread (statt USB; nie per WLAN).
- Mehrere Hardware-Plattformen: ESP32-WROOM (heute), ESP32-C6, weitere.

## Zurückgestellt
- Export-Datei für den eNet Server aus der Bridge-Konfiguration (2026-11-07 besprochen): Export = komplette
  Objekt-Datenbank (1193 Tabellen, Zeitstempel je Feld, Inbetriebnahme-Stand). Realistisch nur als Änderung
  eines echten Exports; zuerst an einem echten Server testen, ob der Server eine minimal geänderte Datei annimmt.

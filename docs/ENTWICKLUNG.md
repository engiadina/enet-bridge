# Entwicklung (eNet-Bridge)

Für Anwender: [`README.md`](../README.md) (Englisch) bzw. [`README.de.md`](../README.de.md),
Hardware: [`HARDWARE.md`](HARDWARE.md). Hier: Bauen, Werkzeuge, serielle Befehle, Aufbau des Codes.
Schnittstelle Bridge ↔ Host: [`SCHNITTSTELLE.md`](SCHNITTSTELLE.md).

## Bauen und Flashen

**Über den HA-Rechner (Bridge hängt per USB am HA, z. B. Raspberry Pi; Ziel per `ENET_HA_HOST`):**

```bash
tools/flash_raspi.sh            # baut, kopiert per SFTP, flasht mit esptool auf dem Raspi
```

Konfiguration, Frequenzkorrektur und Server-Schalter (NVS) bleiben erhalten.

**Firmware über HA:** `tools/fw_bundle.sh` baut und legt die Firmware in die Integration
(`custom_components/enet_bridge/bridge_fw/`). Nach dem Einspielen der Integration aktualisiert HA jede angeschlossene
Bridge mit älterer Firmware automatisch; ein ESP32 ohne Bridge-Firmware wird über die Update-Entität
„Bridge-Firmware“ installiert (nach Bestätigung).

**Integration einrichten** (Einstellungen → Geräte & Dienste → eNet-Bridge), drei Wege:

- *Aus einem Export des eNet Servers übernehmen* (Umzug): .iex hochladen; Domäne, Server-Kennung, Geräte,
  Szenen werden übernommen, umprogrammiert wird nichts. Läuft der echte Server noch, Häkchen „eNet Server ist
  ausgeschaltet“ weglassen: Die Bridge startet dann mit **„Nur mithören“** – sie empfängt (Tasten als Ereignisse,
  Lampenzustände aus den Meldungen der Aktoren), sendet aber nichts, kalibriert sich aus empfangenen Telegrammen
  und schreibt nichts in die Aktoren. Sobald der Server aus ist, den Schalter „Nur mithören“ an der Bridge
  ausschalten; dann folgen Kalibrierung, Statusabfragen, Szenen-Abgleich und die Server-Funktionen.
- *Neue Anlage ohne eNet Server*: Domäne und Server-Kennung werden erzeugt, Geräte danach einlernen.
- *Bestehende Anlagendatei* (`anlage.json` aus `tools/iex.py`).

Danach verwaltet HA die Anlage selbst (`.storage/enet_anlage_<Eintrag>`); `anlage.json` ist nur noch Import.
Gerätedefinitionen aller 41 Katalog-Typen bringt die Integration mit (`custom_components/enet_bridge/definitionen.json`,
erzeugt mit `tools/katalog.py --alle`); eine `/config/enet/definitionen.json` ergänzt bzw. überschreibt sie.

*Herkunft der Gerätedefinitionen:* `definitionen.json` enthält ausschließlich technische Funktionsdaten, die für
die Ansteuerung der Geräte nötig sind (Kanäle, Gruppenobjekt-Offsets, Parameteradressen, Kodierungen,
Wertebereiche, Auswahlwerte, Produktnummern) – keine Beschreibungstexte oder Bilder. Sie wurden aus dem
Gerätekatalog in der Update-Datei des eNet Servers in eine eigene Struktur überführt und für die Geräte der
Testanlage durch eigene Messungen und Mitschnitte geprüft bzw. ergänzt (Parameterschreiben, Link-Tabellen,
Produktkennung). Zweck ist die Interoperabilität mit vorhandenen eNet-Geräten.

**Neue Bridge in Betrieb nehmen:** anstecken (ein leerer ESP32: einmal „Bridge-Firmware“ installieren) – fertig.
Die HA-Integration erkennt eine Bridge ohne passende Konfiguration bzw. ohne Kalibrierung und

1. überträgt die Konfiguration aus der Anlage (Kanäle; Server-Tasten und lokale Szenen aus dem Abschnitt
   `"esp"`, Format siehe SCHNITTSTELLE.md „config“) und setzt den Server-Schalter so, wie er in HA steht,
2. sucht per Statusabfragen die Frequenzkorrektur des Funkmoduls und speichert sie auf der Bridge
   und führt sie im Betrieb automatisch nach (aus den empfangenen Telegrammen, ohne zu senden; fängt Drift
   und Modultausch ab). Zur Fehlersuche gibt es die ausgeblendete Diagnose-Entität „Frequenzkorrektur“.

Der Server-Schalter gehört zur Anlage in HA (Standard an) und wird auf jede Bridge übertragen. Hört sie einen echten eNet Server, zeigt HA
„Echter eNet Server aktiv“ – abgeschaltet wird nur von Hand. Fällt das Funkmodul im Betrieb aus, meldet das
die Entität „Funkmodul“ (die Bridge versucht alle 5 s, es neu zu starten).

**Direkt am Mac (Bridge per USB am Mac):**

```bash
cd ~/Develop/Arduino/eNet/enet-bridge
pio run -t upload
pio device monitor          # Rohausgabe (JSON), Befehle siehe unten
```

Erwartete erste Zeile:

```json
{"event":"start","fw":"enet-bridge 0.3","cc1101_version":20,"ok":true}
```

`cc1101_version` ist bei echten Chips 20 (0x14) oder 4. Bei 0 oder 255: Verkabelung/3,3 V prüfen.

## Testprogramm (Gegenstelle statt Home Assistant)

```bash
python3 -m venv .venv && .venv/bin/pip install pyserial    # einmalig
.venv/bin/python tools/iex.py "Anlage.iex" -o anlage.json    # Anlage aus dem Server-Export
.venv/bin/python tools/enet.py --port /dev/cu.usbserial-0001 --anlage anlage.json --log test.jsonl
```

```
> liste
> ein stylos 1
> dimm decke tür 50
> status indirekte
```

**Standalone-Server im ESP** (nur bei ausgestecktem eNet Server!) – Konfiguration hochladen und einschalten:

```bash
.venv/bin/python tools/server_cfg.py --anlage anlage.json --szenen szenen.json --port /dev/cu.usbserial-0001
# danach im Monitor: server an   (bleibt gespeichert; aus mit: server aus)
```

Der ESP beantwortet dann Server-Tasten, führt lokale Szenen aus, fragt den Status ab und meldet alles als
JSON (`key`, `state`, `result`, `scene_done`). Befehle über die Warteschlange: `set <IA> <Handle> <Hex>`,
`read <IA> <Handle>`, `szene <Nr>`, `zustand`. Eine Taste ohne `szene` in der Szenendatei wird nur gemeldet.

**Server-Emulation auf dem PC** (Vorläufer, gleiche Funktion, nur bei ausgestecktem eNet Server!):

```bash
.venv/bin/python tools/enet.py --port /dev/cu.usbserial-0001 --anlage anlage.json --server szenen.json
> server an          # ab jetzt werden Server-Tasten beantwortet und Szenen ausgeführt
> szene alles an     # Szene von Hand (geht auch bei laufendem Server, sind nur Aktor-Befehle)
> zustand            # Zustand aller Kanäle (Statusmeldungen, sonst Abfrage 8 s nach jedem Befehl)
```

Die Emulation auf dem PC startet ausgeschaltet und schaltet sich selbst ab, sobald ein Telegramm des echten
Servers empfangen wird. (Die Firmware ab 0.8.2 meldet einen echten Server nur, sie schaltet nicht ab.)

Zeigt alle Telegramme mit Gerätenamen, wartet auf die Antwort des Aktors und meldet die Antwortzeit.
`anlage.json` kann von Hand ergänzt werden (z. B. neu eingelernte Geräte); sie wird nicht eingecheckt.
Beim Öffnen des Ports startet der ESP32 neu (DTR) – das ist normal.

## Live-Dekodierung (älter, ohne Anlage-Datei)

```bash
pip3 install pyserial
ls /dev/cu.*                                    # Port der Bridge finden
python3 tools/enet_live.py --port /dev/cu.usbserial-XXXX --log bridge.jsonl --kein-broadcast
```

Zum Vergleich parallel das SDR:

```bash
rtl_433 -f 868.236M -s 1000k -R 105 -M time:usec -F json 2>/dev/null | python3 tools/enet_live.py --kein-broadcast
```

Hinweis: `pio device monitor` und `enet_live.py --port` können nicht gleichzeitig am selben Port hängen.
Mit `--port` gehen eingetippte Zeilen als Befehl an die Bridge.

## Befehle (Zeile + Enter)

| Befehl | Wirkung |
|---|---|
| `v` | ausführliche Ausgabe an/aus (CRC-Fehler, Rohdaten) |
| `r` | CC1101-Register ausgeben |
| `s` | Statistik |
| `cmd <IA> <Handle> <Hex>` | Aktor schalten wie der Server, z. B. `cmd 0.0.4 2 01` = Stylos K1 ein, `cmd 0.0.4 28 00` = K2 aus |
| `get <IA> <Handle>` | Status abfragen, z. B. `get 0.0.4 20` |
| `send <Hex>` | beliebiges Telegramm (ohne CRCs) |
| `d` | Diagnose: GDO-Leitungen (`0/1` = ok), Chip-Zustand (13 = RX), RSSI und Carrier-Sense-Anteil über 6 s |
| `w <Reg> <Wert>` | CC1101-Register schreiben (Hex), z. B. `w 0c 3c` – nur zum Experimentieren, gilt bis zum Neustart |

GO-Handle = Kanaloffset (Kanal 1: 0, Kanal 2: 26) + GO-Nummer (2 = Ein/Aus, 4 = Wert 0–255, 20 = Status).
Domäne und Server-SN: Startwerte in `src/config.h`, im Betrieb kommen sie per Konfiguration von HA.

**Solange der echte eNet Server läuft:** nur `cmd`/`get` an Aktoren senden, keine Tasten beantworten
und keinen Server-Broadcast senden.

## Fehlersuche

- **Gar nichts kommt an:** Mit `v` die ausführliche Ausgabe einschalten. Kommen `invalid`/`manchester_error`-Meldungen,
  wird zwar ein Sync erkannt, aber die Dekodierung passt nicht → `KNXRF_INVERT_CHIPS` in `src/config.h` auf 1 setzen.
  Kommt auch das nicht: Register mit `r` ausgeben und in einem Issue mitschicken.
- **Frequenzablage:** Die Bridge speichert ihre Korrektur im NVS (Befehl `radio`); HA sucht und führt sie automatisch nach.
  `CC1101_FREQ_OFFSET` in `src/config.h` ist nur der Startwert für eine noch nicht kalibrierte Platine (60:
  bisher alle Module, ≈ 95 kHz). In der Ausgabe steht `foff_khz` (Ablage des empfangenen Signals); mit `radio`
  ohne Felder liefert die Bridge den Mittelwert `freqest` (Schritte zu ≈ 1,59 kHz, richtig: Offset += freqest).
- **Sendet, aber kein Aktor antwortet (`tx` steigt, `rx_ok` bleibt 0):** Frequenzkorrektur (s. o.), Antenne,
  868-MHz-Modul? Ohne gültige Konfiguration (`hello` → `config.valid:false`) sendet die Bridge gar nichts.
- **Statistik:** `s` + Enter.

## Tests (Protokoll-Logik, ohne Hardware)

```bash
pio test -e native
```

## Aufbau

```
src/config.h      Pins, Frequenzkorrektur
src/cc1101.*      CC1101-Treiber, Funkparameter KNX RF (868,3 MHz, 2-FSK, 32,768 kBaud, Sync 0x7696)
src/knxrf.*       Manchester, CRC (0x3D65), Telegramme zerlegen/bauen, Sende-Bitstrom – plattformunabhängig
src/main.cpp      Empfangsschleife, JSON-Ausgabe, serielle Befehle
test/             Unit-Tests (echte, mitgeschnittene Telegramme)
tools/enet.py       Testprogramm: Aktoren schalten, Telegramme mit Namen anzeigen
tools/iex.py        Import des eNet-Server-Exports (.iex) -> anlage.json (Auswertung: custom_components/enet_bridge/iex.py)
tools/katalog.py    Gerätedefinitionen aus dem lokalen Insta-Katalog (katalog/, nicht im Repo);
                    --alle -> custom_components/enet_bridge/definitionen.json (im Repo, alle Typen)
tools/funktest.py   Störpegel und Erreichbarkeit aller Aktoren messen
tools/flash_raspi.sh Firmware über den Raspi flashen
custom_components/enet_bridge/  Home-Assistant-Integration
```

## Fahrplan

1. Empfang – mit SDR vergleichen – erledigt
2. Senden: Aktor schalten wie der eNet Server (FunctionPropertyCommand) – erledigt
3. Server-Emulation: Tasten beantworten (SumstatusGet → SumstatusInfo), Broadcast – erledigt
4. Eigene Home-Assistant-Integration (Python), Bridge als Funkmodem über USB – erledigt
5. Import des Projekt-Exports (.iex), Einlernen ohne Server – erledigt
6. Umstieg einer zweiten Anlage, englische Übersetzung, Veröffentlichung – erledigt

Weiter: [`ROADMAP.md`](ROADMAP.md)

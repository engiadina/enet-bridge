# Schnittstelle Bridge (ESP) ↔ Host (Home Assistant)

Protokollversion **1** · Stand 2026-10-29 · Status: **Firmware 0.8.3 setzt um:** radio, scene_call, fp_read, fp_cmd, prop_read, prop_write, np_write, hello, time, stats, server, config,
monitor, switch, dim, set, read, poll, state, scene, send, diag, reboot; Ereignisse start, key, state, result,
scene_done, config, real_server, radio, telegram, rx_error. **Noch offen:** learn, remove, link/unlink, sender_config,
cancel, device_seen, progress/wake_*.

Begriffe: **ESP** = Bridge (ESP32 + CC1101, Firmware dieses Repos). **Host** = Gegenstelle am USB, später die
HA-Integration, vorerst `tools/enet.py`.

---

## 1. Aufgabenteilung

| Aufgabe | ESP | Host |
|---|---|---|
| Funk: Senden/Empfangen, Manchester, CRC, LFN, Frequenzkorrektur | ✔ | |
| Sende-Warteschlange (ein Auftrag zur Zeit, Quittung, Wiederholung) | ✔ | |
| Server-Tasten beantworten (SumstatusInfo, < 50 ms) | ✔ | |
| Tastendrücke melden (alle Arten) | ✔ | wertet aus (Ereignis-Entitäten, Automationen) |
| Lokale Szenen (Standalone ohne Host) | ✔ optional | Szenen/Automationen in HA |
| Zustandstabelle + Status-Polling 8 s nach jedem Befehl | ✔ | spiegelt Zustände in Entitäten |
| Prozeduren (Einlernen, Entfernen, Verknüpfen, Sender konfigurieren, Parameter schreiben) | ✔ Ablauf | stößt an, liefert alle Daten |
| Selbstabschaltung bei echtem eNet Server | ✔ | zeigt Warnung |
| Projekt: Geräte, Namen, Räume, Typen, .iex-Import, Gerätedefinitionen | | ✔ Master |
| Vergabe von IAs und Antwort-GAs | | ✔ |
| Konfiguration für Standalone dauerhaft speichern | ✔ | überträgt sie |
| Uhrzeit (Zeitstempel beim Programmieren) | nutzt | liefert (`hello`/Befehlsfeld) |

Grundsätze:
- **Host = Master der Daten, ESP = Master der Funkstrecke.** Der ESP hält nur, was er für zeitkritische Aufgaben
  und Standalone braucht; Prozeduren bekommen alle Daten im Befehl.
- **Der ESP kennt keine Parameterbedeutungen.** Parameter sind Bytes an Adressen; Bedeutung, Kodierung und
  Wertebereiche stehen in den Gerätedefinitionen des Hosts.
- **Kein stiller Funkverkehr:** Der ESP sendet nur auf Befehl, als Antwort auf Server-Tasten (nur bei `server on`),
  für Status-Polling und für lokale Szenen.

## 2. Transport

| Punkt | Festlegung |
|---|---|
| Physisch | USB, ESP32-DevKit: CP210x-UART; ESP32-C6: USB-CDC |
| Parameter | 115200 Baud, 8N1, keine Flusskontrolle |
| Steuerleitungen | Host öffnet den Port mit **DTR = 0 und RTS = 0**. Unter Linux startet das DevKit beim Öffnen trotzdem neu (Kernel setzt die Leitungen kurz) – der Host hält den Port daher dauerhaft offen und behandelt `start` als Neusynchronisation. Beim C6 (USB-CDC) ohne Bedeutung. |
| Rahmung | eine Nachricht = eine Zeile UTF-8-JSON, Ende `\n` (ESP sendet `\r\n` nicht) |
| Länge | max. **512 Byte** pro Zeile inkl. `\n`; längere Zeilen verwirft der ESP mit `error` `line_too_long` |
| Puffer | ESP-Empfangspuffer 4 KB (ab 0.5.4): der Host darf mehrere Befehle direkt hintereinander schicken, ohne auf `re` zu warten |
| Nicht-JSON | Zeilen, die nicht mit `{` beginnen, sind Textbefehle zur Fehlersuche (Abschnitt 13) |
| Ausgaben des Bootloaders | nach Reset können Nicht-JSON-Zeilen kommen (ROM-Bootmeldung) – Host ignoriert alles, was kein gültiges JSON ist |

## 3. Datentypen und Schreibweisen

| Typ | Form | Beispiel |
|---|---|---|
| IA (Einzeladresse) | String `"b.l.g"` | `"0.0.4"` |
| SN / Domäne | 12 Hex-Zeichen, klein | `"000a34567812"` |
| GA | Ganzzahl 0…65535 | `22`, `790` |
| Kanal | Ganzzahl, Nummer wie im Export (1, 2, …) | `2` |
| Handle | Ganzzahl = Kanaloffset + GO | `28` |
| Hex-Daten | gerade Anzahl Hex-Zeichen, klein, ohne Trenner | `"01"`, `"3e1a"` |
| Prozent | Ganzzahl 0…100 | `50` |
| Zeit | ISO 8601 lokale Zeit `"2026-10-27T09:15:00"` | |
| Wahrheitswert | JSON `true`/`false` | |

Unbekannte Felder werden ignoriert (Vorwärtskompatibilität). Pflichtfelder fehlen → `bad_args`.

## 4. Nachrichtenmodell

### 4.1 Befehl (Host → ESP)
```json
{"cmd":"switch","id":17,"ia":"0.0.4","ch":1,"on":true}
```
`id`: Ganzzahl 1…2³¹, vom Host vergeben, eindeutig für die Laufzeit eines Befehls. Ohne `id` keine Antwort.

### 4.2 Antwort (ESP → Host), sofort (< 20 ms)
```json
{"re":17,"ok":true}
{"re":17,"ok":false,"err":"queue_full","msg":"Warteschlange voll"}
```
Bedeutet nur **angenommen / abgelehnt**. Befehle ohne Funk liefern ihr Ergebnis direkt in der Antwort.

### 4.3 Ereignis (ESP → Host), asynchron
```json
{"ev":"result","id":17,"ok":true,"ia":"0.0.4","ch":1,"tries":1}
```
Funk-Befehle enden **immer** mit genau einem `result` (Erfolg, Fehler oder Abbruch) mit derselben `id`.

### 4.4 Fehlercodes (`err`)

| Code | Bedeutung |
|---|---|
| `unknown_cmd` | Befehl unbekannt |
| `bad_args` | Feld fehlt, falscher Typ oder außerhalb des Wertebereichs |
| `no_radio` | CC1101 nicht erkannt |
| `no_config` | keine gültige Konfiguration (Domäne fehlt) – Senden gesperrt |
| `unknown_channel` | `ia`/`ch` nicht in der Konfiguration |
| `queue_full` | Warteschlange voll (64 Aufträge) |
| `busy` | eine Prozedur läuft (Einlernen, Verknüpfen …), Befehl nicht zulässig |
| `real_server` | (nicht mehr verwendet; bis 0.8.1: echter eNet Server aktiv – Server-Funktionen gesperrt) |
| `timeout` | keine Quittung nach allen Versuchen (`result`) |
| `nack` | Gerät antwortet mit Fehlercode `rc` ≠ 0 (`result`, Feld `rc`) |
| `aborted` | abgebrochen (`cancel`, Neustart, Zeitüberschreitung der Prozedur) |
| `not_found` | Gerät/Sender nicht gefunden bzw. nicht aufgewacht |
| `verify_failed` | Kontrolllesung nach dem Schreiben stimmt nicht |

## 5. Zeitverhalten (Festwerte der Firmware)

| Wert | Größe | Herkunft |
|---|---|---|
| Quittungs-Timeout je Versuch | 400 ms, je Wiederholung +150 ms (400/550/700/850/1000) | Aktoren antworten nach 40–60 ms |
| Versuche | 5 (≈ 3,5 s bis `timeout`) | Server: 3 im 400-ms-Raster; ab Firmware 0.5.2 mehr gegen kurze Störungen |
| Kanal belegt (LBT, 30 ms gewartet) | erneut nach 20 ms, zählt nicht als Versuch, höchstens 20× je Auftrag | ab 0.5.3; `stats.tx_busy` zählt jeden Fall |
| Abstand Befehle in einer Szene | nach Quittung (~70 ms) | Server |
| Status-Polling nach Befehl | 8000 ms, entfällt bei Statusmeldung | Server-Code |
| Antwort auf Server-Taste | < 50 ms nach Empfang | gemessen 36–44 ms |
| Sender wecken: Wiederholung / Höchstdauer | 100 ms / 60 s | Server ≈ 70–130 ms |
| Einlernen: Suchintervall / Höchstdauer | 3 s / `timeout_s` (Standard 120 s) | Server |
| Warteschlange | 64 Aufträge | |

## 6. Kanäle, Zustände, Tasten

### 6.1 Adressierung
Aktor-Kanal `{"ia","ch"}`; der ESP rechnet aus seiner Konfiguration (Abschnitt 9) Handle-Offset, Kanal-Objekt und
Status-GA: `Handle = Offset + GO`, `Status-GA = GA-Basis·256 + Offset + 22`, `Kanal-Objekt = obj`.

### 6.2 Zustand eines Kanals
`on` (bool), `percent` (0…100, bei Schaltkanälen 0 oder 100), `known` (bool), `source`
(`status` = Statusmeldung des Aktors, `poll` = Abfrage, `cache` = Abruf aus der Tabelle). Der ESP meldet `state`
nur bei Änderung bzw. auf Anfrage.

### 6.3 Tastendrücke – Arten (`kind`)
| `kind` | Erkennung | Antwort durch ESP | Felder |
|---|---|---|---|
| `server` | SumstatusGet, Antwort-SN = Server-SN, CC 0x08 | ja (bei `server on`) | `code`, `press`, `seq`, `reply_ga` |
| `direkt` | SumstatusGet an einen Aktor (CC 0xF0) | nein (Aktor antwortet) | `press`, `hold_ms`, `richtung`, `seq`, `reply_sn`, `reply_ga` |
| `wert` | nackter Tastenwert (2 Byte) einer unbelegten Wippe | – | `press`, `hold_ms`, `richtung` |

Kodierung `cmd`: Server-Taste: Bit 14 = gedrückt, Bits 11–8 = Tastencode. Direkt/Wert: `0x40xx` = linke (ungerade)
Taste, `0x80xx` = rechte (gerade) Taste der Wippe (`richtung`: `links`/`rechts`; ab Firmware 0.5.1, vorher `oben`/`unten`), untere 14 Bit = Haltedauer in 10 ms (0 = Drücken, > 0 = Loslassen nach dieser Zeit).

## 7. Befehle

Legende: **F** = braucht Funk (Abschluss über `result`), **P** = Prozedur (exklusiv, andere Funk-Befehle → `busy`;
Server-Tasten werden weiter beantwortet).

### 7.1 Verwaltung

| `cmd` | Felder | Antwort / Ergebnis |
|---|---|---|
| `hello` | `time` (optional, setzt die Uhr) | `product` (`"enet-bridge"`, ab 0.8.1), `fw`, `proto`, `chip` (`esp32`/`esp32c6`, ab 0.8.14; fehlt = `esp32`), `id` (MAC der Platine, ab 0.8), `radio` {`ok`,`version`,`freq_offset`,`calibrated`,`freqest`,`freqest_n`}, `server` {`on`}, `config` {`crc`,`channels`,`scenes`,`keys`}, `queue` {`len`,`max`}, `uptime_s`, `procedure` (laufende Prozedur oder `null`) |
| `time` | `time` | setzt die Uhr (für Zeitstempel beim Programmieren) |
| `stats` | – | Zähler: `rx_ok`, `crc_error`, `corrected` (Telegramme mit 1–3 korrigierten Chip-Paaren, ab 0.5.5), `invalid`, `overflow`, `tx`, `tx_busy` |
| `server` | `on` (optional) | setzt bzw. liefert `on` (bleibt gespeichert) |
| `config` | `part` (0…n), `last` (bool), `data` (hex, ≤ 400 Zeichen), beim letzten Teil `crc` | je Teil `ok`; nach `last` prüft und speichert der ESP und sendet `ev:"config"` |
| `radio` | `freq_offset` (−128…127, optional), `save` (bool, optional) | Frequenzkorrektur FSCTRL0 sofort setzen (setzt den FREQEST-Mittelwert zurück) bzw. im NVS speichern; liefert `radio` {`ok`,`version`,`freq_offset`,`calibrated` (gespeichert), `freqest` (mittlere Abweichung der gültigen Telegramme in FSCTRL0-Schritten, richtig: FSCTRL0 += freqest), `freqest_n`} – ab 0.8 |
| `monitor` | `on` | schaltet `telegram`-Ereignisse für alle Telegramme (RX und eigene TX) |
| `cancel` | – | bricht die laufende Prozedur ab (`result` mit `aborted`) |
| `reboot` | – | Neustart; danach `start` |

### 7.2 Bedienen (F)

| `cmd` | Felder | Wirkung |
|---|---|---|
| `switch` | `ia`, `ch`, `on` | GO 2 = 01/00 |
| `dim` | `ia`, `ch`, `percent` | GO 4 = floor(p·255/100); 0 % schaltet aus |
| `set` | `ia`, `handle`, `value` (hex 1–4 Byte) | beliebiges GO-Handle (FuncPropCmd obj 0 PID 62) |
| `read` | `ia`, `handle` | FuncPropStateRead; `result.value` = Wert |
| `poll` | `ia`, `ch` | Status jetzt lesen (GO 20, bei Dimmern GO 21) → `state` |
| `state` | `ia`, `ch` (optional) | **ohne Funk:** `state`-Ereignisse aus der Tabelle (`source:"cache"`), Antwort mit `count` |
| `scene` | `nr` | lokale Szene ausführen; Einzel-`result` je Aktion entfällt, Abschluss `scene_done` + `result` |

Ergebnis (`result`): `ok`, `ia`, `ch` bzw. `handle`, `tries`, bei Fehler `err` (`timeout`/`nack`) und `rc`.

### 7.3 Geräteverwaltung (P)

| `cmd` | Felder | Ablauf | Ergebnis |
|---|---|---|---|
| `learn` | `ia`, `timeout_s` (Std. 120), `sn` (optional: nur dieses Gerät) | Suche (DoA_Read/DoA_SelRead) → `learn_found` → Auswahl (PID 71) → Domäne setzen+prüfen → IA setzen+prüfen → Link-Tabelle GO 1 lesen | `sn`, `ia`, `product` (hex), `ga_basis` |
| `remove` | `ia` | A_Restart Typ 1, Erase-Code 2 → Kontrolle DoA_SN_Read | `ok`, `sn` |
| `link` / `unlink` | `sender` {`ia`,`sn`,`ch`,`art` = `wippe`/`taste`}, `aktor` {`ia`,`sn`,`ch`} | Sender wecken (`wake_wait`/`wake_ok`) → Aktor: LSC, Links GA n→GO 1, n+1→GO 19 → Sender: Tasten-Objekte, Link Aktor-Status-GA → GO 1 → Abschluss → Sender entlassen | `ok`, Einzelschritte im Fehlerfall in `msg` |
| `sender_config` | `ia`, `sn`, `channels`: [{`ch`, `mode` (`taste`/`wippe`/`aus`), `code` (Taste), `reply` {`sn`,`ga`} (Server-Taste) bzw. `status` [{`sn`,`ga`}] (direkt)}] | Sender wecken → je Kanal: Modus, Kanal-Reset (A_Restart Erase 6), Werkseinträge, Rückmeldungs-Links → entlassen | `ok` |
| `prop_read` | `ia`, `obj`, `pid`, `start` (Element, 12 Bit), `count` (1…15) | A_PropertyValue_Read 0x3D5, Antwort 0x3D6 (zugeordnet über Objekt/PID/Start); Parameterspeicher = Kanal-Objekt PID 79, Element = Byte | `value` (hex), `rssi`; `nack` bei Anzahl 0 |
| `prop_write` | wie `prop_read` + `data` (hex, ≤ 16 Byte); `count` Standard = Bytes | A_PropertyValue_Write 0x3D7, Antwort 0x3D6 | `value` |
| `np_write` | `ia`, `data` (hex: Objekttyp, PID, Wert) | A_NetworkParameter_Write P2P, ohne Antwort – gilt nach dem Senden als erledigt | – |
| `fp_read` / `fp_cmd` | `ia`, `obj`, `pid`, `data` (hex, ≤ 16 Byte) | FuncPropStateRead 0x2C8 / FuncPropCmd 0x2C7 auf Objekt/PID, Antwort 0x2C9 (zugeordnet über Objekt/PID). Link-Tabelle = Kanal-Objekt PID 63: lesen `00 00 GO idx`, anlegen `00 00 SN GA GO`, löschen `02 00 SN GA GO` | `value` (hex, ohne rc; Link: Typ(1, 01 = Werk) SN(6) GA(2) GO(2)), `rssi`; `nack` + `rc` (Link-Tabelle: `rc` 255 = kein Eintrag) |
| `scene_call` | `ga`, `nr` (0…63), `learn` (Std. false) | KNX-Szene in den Aktoren: Gruppentelegramm unter Server-SN an die Szenen-GA, 1 Byte = `nr` (+ 0x80 = aktuellen Zustand als Szenenwert speichern). Ohne Quittung, 3× im Abstand 80 ms | `ga`, `tries` (= gesendet). Die Aktoren melden geänderte Zustände per `state` |

Eine eNet-Szene besteht in jedem Mitglied aus zwei Links unter der Server-SN: Szenen-GA n → GO 6 (Aufruf) und
n+1 → GO 19 (Sammelstatus). Anlegen/Löschen der Links wie Parameter schreiben (LSC 02 … Links … D2 = 04 …
LSC 01, siehe unten). Bestätigt 2026-10-28: Anlegen, Lernen (`learn`), Aufruf und Löschen.

Parameter schreiben setzt der **Host** aus diesen Grundbausteinen zusammen (Integration `params.py`, Ablauf wie
der eNet Server): LSC (Obj 1 PID 6) `02` → Geräte-Objekt PID 39 = `…03`,
3A = 00, 41 = 01, 42 = 07 → Wert in Kanal-Objekt PID 79 → Prüfsumme (PID D1) lesen → D0 = 04, D2 = 04 →
Zeitstempel (Geräte-Objekt PID 39) → 3× `np_write 00123c00` → LSC `01` → Kontrolle. Geräte-Objekt = Katalog
`interfaceObjectIndexUserObjectSystem` (4 bei einkanaligen Aktoren, 5 bei SA2M). Ein eigener Befehl
`param_write` in der Firmware entfällt (früherer Entwurf).

Zeitstempel kommen aus der ESP-Uhr; ohne gesetzte Uhr lehnt der ESP `param_write`, `link`, `sender_config` mit
`bad_args` „Uhr nicht gesetzt“ ab.

### 7.4 Fehlersuche

| `cmd` | Felder | Wirkung |
|---|---|---|
| `send` | `frame` (hex, ohne CRC) | Roh-Telegramm, LFN setzt der ESP (F) |
| `diag` | – | GDO-Test, Chip-Zustand, RSSI/CS über 6 s |

## 8. Ereignisse

| `ev` | Felder | Auslöser |
|---|---|---|
| `start` | `fw`, `proto`, `radio` {`ok`,`version`}, `server` {`on`}, `config` {`crc`}, `reset_reason` | jeder Start; Host synchronisiert neu |
| `key` | `sn`, `ga`, `kind`, `cmd` (hex), `press`, `code` / `hold_ms` / `richtung`, `seq`, `reply_sn`, `reply_ga`, `answered`, `scene` (Nr. oder −1), `battery` (`ok`/`weak`, ab FW 0.8.8), `rssi` | jeder Tastendruck; `kind` `server`/`direkt`/`wert` (2 Byte, auch Helligkeit des Sonnensensors) /`wert1` (1 Byte unter Sender-SN, z. B. Szenentelegramm des Sonnensensors, `cmd` 2-stellig, ab FW 0.8.9) |
| `state` | `ia`, `ch`, `on`, `percent`, `source` | Änderung (Statusmeldung des Aktors), jede Abfrage (`source:"poll"`, auch unverändert, ab 0.7.1) oder Anfrage (`cache`) |
| `result` | `id`, `ok`, `err`, `msg`, weitere je Befehl | Abschluss eines F/P-Befehls |
| `scene_done` | `nr`, `failed`, `id` (falls per Befehl) | lokale Szene fertig |
| `config` | `ok`, `crc`, `channels`, `scenes`, `keys`, `err` | nach `config … last` |
| `learn_found` | `sn`, `domain` | Gerät im Lernmodus gefunden |
| `wake_wait` | `sn`, `timeout_s` | Benutzer soll am Sender eine Taste drücken |
| `wake_ok` | `sn` | Sender wach |
| `progress` | `step`, `of`, `text` | Fortschritt einer Prozedur |
| `real_server` | – | Telegramm des echten Servers (höchstens 1× je Minute); nur Meldung, ab 0.8.2 ohne Abschalten |
| `radio` | `radio` {wie Befehl `radio`} | CC1101 antwortet nicht mehr (Version ≠ Startwert, 2× im Abstand von 1 s) bzw. wieder (Neuinitialisierung alle 5 s) – ab 0.8 |
| `device_seen` | `sn`, `ia`, `rssi` | erstes Telegramm eines Geräts seit Start (für Diagnose/Erreichbarkeit) |
| `telegram` | `dir` (`rx`/`tx`), `data`, `rssi`, `foff_khz`, `ms` | nur bei `monitor` |
| `error` | `err`, `msg` | Fehler ohne Bezug zu einem Befehl |

## 9. Konfiguration im ESP (Binärformat Version 3)

Big Endian, abgeschlossen mit CRC16 (knxrf::crc16). Überträgt der Host nach `hello`, wenn `config.crc` abweicht.

```
'E' 0x03
Domäne(6) Server-SN(6) Server-IA(2)
nCh(1) nAct(1) nScenes(1) nKeys(1)
Kanäle  nCh ×:   IA(2) SN(6) Kanalnr(1) Offset(1) Objekt(1) Flags(1: Bit0 Dimmer) GA-Basis(1)
Aktionen nAct ×: IA(2) Handle(2) Länge(1) Wert(Länge ≤ 4) – IA 0: Szenenaufruf, Gruppentelegramm an GA = Handle
Szenen  nScenes ×: erste Aktion(1) Anzahl(1)
Tasten  nKeys ×: SN(6) GA(2) Szene(1, signed, −1 = nur melden) – ab 0.8.3 zählt nur das untere GA-Byte (Basis egal)
CRC16(2)
```
Grenzen: 48 Kanäle, 128 Aktionen, 32 Szenen, 32 Tasten (≈ 2 KB). Neu gegenüber V2: Kanalnummer und Objekt.

## 10. Abläufe

### 10.1 Verbindungsaufbau
```
Host öffnet Port (DTR=0, RTS=0)
Host → {"cmd":"hello","id":1,"time":"…"}
ESP  → {"re":1,"ok":true,"fw":"0.8.0","proto":1,"id":"c8f09e6d1e94","radio":{…,"calibrated":true},"config":{"crc":"1a2b",…},…}
Host → config … (nur wenn crc abweicht oder valid = false; Host baut sie aus anlage.json, esp_config.py)
Host → Kalibrierung (nur wenn calibrated = false oder kein Aktor antwortet, siehe 10.6)
Host → {"cmd":"state","id":2}            → ESP: state … (cache) ; Host fragt unbekannte Kanäle mit poll ab
```
Kommt `start` unerwartet (ESP-Neustart), wiederholt der Host den Ablauf.

### 10.2 Schalten mit Status
```
Host → switch 0.0.4/1 on (id 5)       ESP → re 5 ok
ESP  ⇢ Funk: FuncPropCmd → Aktor quittiert (≈ 45 ms)
ESP  → result 5 ok tries 1
Aktor ⇢ SumstatusInfo (1–4 s)        ESP → state 0.0.4/1 on (status)
   (kommt keine Meldung: nach 8 s poll → state … (poll))
```

### 10.3 Server-Taste
```
Sender ⇢ SumstatusGet (CC 08, Antwort an Server-SN/GA)
ESP  ⇢ SumstatusInfo ff + seq (< 50 ms)          (nur server on)
ESP  → key kind=server code press answered scene
      lokale Szene? → Befehle nacheinander → scene_done
      sonst → Host entscheidet (Automation)
```

### 10.4 Verknüpfen
```
Host → link {sender, aktor} (id 9)     ESP → re 9 ok
ESP  → wake_wait sn …                  (Host zeigt: „Taste am Sender drücken“)
ESP  ⇢ PID 72 an Sender alle 100 ms … Sender antwortet
ESP  → wake_ok ; progress 1/n … n/n
ESP  → result 9 ok
```

### 10.5 Firmware aktualisieren (über den Host-Rechner)
Die Integration bringt die Firmware je Chip mit (`bridge_fw/<chip>/`, erzeugt mit `tools/fw_bundle.sh`) und flasht
selbst das Paket zum `chip` aus `hello` (leerer ESP: esptool erkennt den Chip)
(esptool aus den requirements): automatisch, wenn `hello` als Bridge antwortet (`product` = `enet-bridge`,
bei Firmware < 0.8.1 genügt `proto`) und die Version älter ist (einmal je Version und HA-Start); antwortet
nach 3 Versuchen nichts, bietet die Update-Entität „Bridge-Firmware“ die Installation an (Bestätigung,
weil HA ein fremdes Gerät am Port nicht ausschließen kann).
```
Host schließt den Port (Integration: „Bridge freigeben“)
esptool --port <by-id> write_flash 0x10000 firmware.bin   (DTR/RTS-Reset durch esptool)
Host öffnet den Port wieder (DTR=0, RTS=0) → wartet auf start → 10.1
```
Konfiguration, Frequenzkorrektur und `server`-Schalter bleiben im Flash (NVS) erhalten.

### 10.6 Neue Bridge in Betrieb nehmen (ohne Eingaben)
```
Firmware flashen (10.5, beim ersten Mal mit Bootloader/Partitionen), anstecken
Host: hello → config.valid = false   → Konfiguration aus anlage.json übertragen
Host: radio.calibrated = false       → Grobsuche: radio freq_offset = 60, 0, ±30, 90, −60, 120, −90, −120,
                                       je Kandidat poll an bis zu 2 Aktoren, bis einer antwortet
                                     → Feinabgleich: alle Kanäle 2× poll, freq_offset += round(freqest), save
Host: normaler Abgleich (state, poll)
```
Nur Statusabfragen – auch zulässig, solange der echte Server läuft. Der Offset gehört zur Platine
(Quarz des CC1101-Moduls): Module aus derselben Charge lagen bei 60 (≈ 95 kHz), Messung 2026-10-29.
Vorzeichen geprüft: Offset 50 → freqest +10,3, Offset 70 → −9,7, Offset 60 → +0,3.
Der `server`-Schalter steht ab 0.8.1 bei einer neuen Bridge (kein NVS-Eintrag) auf an. Ein echter Server
wird nur gemeldet, nie selbst abgeschaltet (0.8.2) – eine Fehlerkennung darf die Bridge nicht stilllegen.

## 11. Abbildung in Home Assistant

- Ein eNet-Gerät = ein HA-Gerät (Name, Raum, Typ, SN, Firmware), Bridge = eigenes Gerät.
- Entitäten: Schaltkanal → `switch`/`light`; Dimmkanal → `light` (Helligkeit); Sender-Kanal → `event`
  (`gedrückt`, `losgelassen`, `lang`); Parameter → `number`/`select`/`switch` (Kategorie Konfiguration);
  Diagnose → `sensor` (RSSI, letzte Meldung, GA-Basis).
- Bridge-Entitäten: Server-Funktionen (`switch`), echter Server erkannt (`binary_sensor`), Funkmodul
  (`binary_sensor`, Konnektivität), Frequenzkorrektur (`sensor`, kHz, standardmäßig deaktiviert; Suche und Nachführen automatisch), Warteschlange,
  Empfangsstatistik (`sensor`), Firmware (`update`).
- Geräteverzeichnis im Host (HA-Speicher `enet_anlage_<Eintrag>`, ab Integration 0.3): Einrichtung aus dem
  .iex-Export (Umzug), als neue Anlage (Domäne/Server-SN erzeugt: `000a` + 4 Zufallsbytes) oder aus anlage.json;
  neue Geräte über `learn` + `param_read`. Der Server-Schalter (`server.funktionen`) gehört zur Anlage und wird
  beim Abgleich auf die Bridge übertragen.
- Gerätedefinitionen pro Typ (eigenes Format): Kanäle, Parameter (Objekt, Start, Länge, Kodierung, Bereich,
  Standard). Erzeugt lokal aus dem Katalog der Server-Firmware oder unabhängig gemessen.

## 12. Sicherheit und Betrieb

- Server-Funktionen starten nach Werkszustand **an** (ab 0.8.1); Schalter `server` bleibt gespeichert und wird
  nur vom Host geändert.
- Erkennt der ESP ein Telegramm des echten Servers (Absender 7.15.255 unter Domäne oder Server-SN), meldet er
  `real_server` (≤ 1×/min); HA zeigt „Echter eNet Server aktiv“ bis 5 min danach. Abschalten nur von Hand
  (Entscheidung 2026-10-29: Selbstabschaltung = Risiko, dass die Bridge dauerhaft stillsteht).
- Ohne Konfiguration (Domäne) kein Senden außer `send` und `learn` mit `bad_args`-Prüfung.
- Verschlüsselte Anlagen (Security-Telegramme) werden nicht unterstützt; der Host erkennt sie am Export.

## 13. Übergang von Firmware 0.4

| heute (Text) | künftig (JSON) |
|---|---|
| `cmd <IA> <Handle> <Hex>` / `get` | `set` / `read` (direkt, ohne Warteschlange: entfällt) |
| `set` / `read` (Warteschlange) | `set` / `read` |
| `server an\|aus`, `szene <n>`, `zustand`, `cfg begin/…/end` | `server`, `scene`, `state`, `config` |
| `send`, `v`, `r`, `s`, `d`, `w` | bleiben als Textbefehle zur Fehlersuche |
| Ereignisse `event:"…"` | `ev:"…"` mit den Feldern aus Abschnitt 8 |
| Prozeduren in `tools/einlernen.py`, `tools/verknuepfen.py` | `learn`, `link`, `unlink` im ESP |

## 14. Lücken und offene Punkte

| # | Lücke | Auswirkung | Vorschlag |
|---|---|---|---|
| 1 | **Server-Identität bei Neuanlage** (Domäne, Server-SN) | ohne eNet Server keine Domäne | Domäne/SN aus ESP-MAC ableiten, Präfix prüfen (Insta-SN beginnen mit 000a) |
| 2 | ~~Batteriestand der Sender~~ | erledigt (FW 0.8.8 / Integration 0.9.10) | RF-Info Bit 1 im `key`-Ereignis als `battery`; HA: Binärsensor „Batterie“ je Sender. Wert „weak“ noch nicht im Mitschnitt gesehen |
| 3 | **Raumszenen im Aktor** (GO 6, SCV1) | evtl. schneller/eleganter als Einzelbefehle | Mitschnitt „Licht aus Raum“ |
| 4 | **Aktor-Kanal 2 verknüpfen** | nur gelesen, nie geschrieben | Test Stylos K2 ↔ freie Wippe |
| 5 | **Szenen-Inhalte aus dem .iex** | Migration der Szenen | Wertelisten in .blb entschlüsseln |
| 6 | **Bedeutung `00 12 3c 00`**, PID 72 `04 05 …`, Load-State-Werte | nur nachgebildet | beibehalten; im Katalog/Code nachsehen |
| 7 | **Sender mit mehreren Aktoren je Wippe** (W2 → Stylos K1+K2) | `link` fügt nur einen Status-Link hinzu | `sender_config` setzt alle Status-Links eines Kanals in einem Durchgang |
| 8 | **Gerätetyp aus Produktnummer** beim Einlernen | Host muss Typ kennen | Zuordnung Produktnummer → Typ im Katalog (DeviceSoftware.xml) |
| 9 | **Parameter-Prüfsumme** (PID D1) | nur gelesen, Bedeutung unklar | Vergleich vor/nach `param_write` als Erfolgskriterium |
| 10 | **Erreichbarkeit** eines Geräts | HA „nicht verfügbar“ | aus `result timeout` + `device_seen` ableiten, kein eigenes Polling |
| 11 | **Mehrere Bridges / Reichweite** | großes Haus | nicht in v1; Hops-Feld ignoriert |
| 12 | **Jalousie, REG, Sensoren** | nicht in v1 | über Gerätedefinitionen + Connection-Codes später |
| 13 | **Firmware-Update aus HA** (`update`-Entität) | Komfort | Abschnitt 10.5 in die Integration übernehmen |
| 14 | **Uhr im ESP** | Zeitstempel beim Programmieren | Host setzt sie per `hello`/`time` (kein RTC/NTP nötig) |

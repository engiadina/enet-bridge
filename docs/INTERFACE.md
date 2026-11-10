# Interface Bridge (ESP) ↔ Host (Home Assistant)

Protocol version **1** · As of 2026-10-29 · Status: **Firmware 0.8.3 implements:** radio, scene_call, fp_read, fp_cmd, prop_read, prop_write, np_write, hello, time, stats, server, config,
monitor, switch, dim, set, read, poll, state, scene, send, diag, reboot; events start, key, state, result,
scene_done, config, real_server, radio, telegram, rx_error. **Still open:** learn, remove, link/unlink, sender_config,
cancel, device_seen, progress/wake_*.

Terms: **ESP** = bridge (ESP32 + CC1101, firmware in this repository). **Host** = the peer on USB, later the
HA integration, for now `tools/enet.py`.

---

## 1. Division of responsibilities

| Task | ESP | Host |
|---|---|---|
| Radio: transmit/receive, Manchester, CRC, LFN, frequency correction | ✔ | |
| Transmit queue (one job at a time, acknowledgement, retries) | ✔ | |
| Answer server keys (SumstatusInfo, < 50 ms) | ✔ | |
| Report key presses (all kinds) | ✔ | evaluates them (event entities, automations) |
| Local scenes (standalone without host) | ✔ optional | scenes/automations in HA |
| State table + status polling 8 s after each command | ✔ | mirrors states into entities |
| Procedures (pairing, removing, linking, configuring transmitters, writing parameters) | ✔ sequence | triggers them, supplies all data |
| Self-shutdown when a real eNet Server is present | ✔ | shows a warning |
| Project: devices, names, rooms, types, .iex import, device definitions | | ✔ master |
| Assignment of IAs and reply GAs | | ✔ |
| Persistently store the configuration for standalone operation | ✔ | transfers it |
| Clock (timestamps when programming) | uses it | supplies it (`hello`/command field) |

Principles:
- **Host = master of the data, ESP = master of the radio link.** The ESP holds only what it needs for time-critical
  tasks and standalone operation; procedures receive all data in the command.
- **The ESP knows no parameter meanings.** Parameters are bytes at addresses; meaning, encoding and value ranges
  are defined in the host's device definitions.
- **No silent radio traffic:** The ESP transmits only on command, in reply to server keys (only with `server on`),
  for status polling and for local scenes.

## 2. Transport

| Item | Specification |
|---|---|
| Physical | USB, ESP32-DevKit: CP210x UART; ESP32-C6: USB CDC |
| Parameters | 115200 baud, 8N1, no flow control |
| Control lines | The host opens the port with **DTR = 0 and RTS = 0**. Under Linux the DevKit still restarts when the port is opened (the kernel briefly toggles the lines) – the host therefore keeps the port open permanently and treats `start` as a resynchronisation. Irrelevant for the C6 (USB CDC). |
| Framing | one message = one line of UTF-8 JSON, terminated by `\n` (the ESP does not send `\r\n`) |
| Length | max. **512 bytes** per line including `\n`; the ESP discards longer lines with `error` `line_too_long` |
| Buffer | ESP receive buffer 4 KB (since 0.5.4): the host may send several commands back to back without waiting for `re` |
| Non-JSON | Lines that do not start with `{` are text commands for debugging (section 13) |
| Bootloader output | after a reset, non-JSON lines may arrive (ROM boot message) – the host ignores anything that is not valid JSON |

## 3. Data types and notation

| Type | Form | Example |
|---|---|---|
| IA (individual address) | string `"b.l.g"` | `"0.0.4"` |
| SN / domain | 12 hex characters, lower case | `"000a34567812"` |
| GA | integer 0…65535 | `22`, `790` |
| Channel | integer, number as in the export (1, 2, …) | `2` |
| Handle | integer = channel offset + GO | `28` |
| Hex data | even number of hex characters, lower case, no separators | `"01"`, `"3e1a"` |
| Percent | integer 0…100 | `50` |
| Time | ISO 8601 local time `"2026-10-27T09:15:00"` | |
| Boolean | JSON `true`/`false` | |

Unknown fields are ignored (forward compatibility). Missing mandatory fields → `bad_args`.

## 4. Message model

### 4.1 Command (host → ESP)
```json
{"cmd":"switch","id":17,"ia":"0.0.4","ch":1,"on":true}
```
`id`: integer 1…2³¹, assigned by the host, unique for the lifetime of a command. Without `id` there is no response.

### 4.2 Response (ESP → host), immediate (< 20 ms)
```json
{"re":17,"ok":true}
{"re":17,"ok":false,"err":"queue_full","msg":"Warteschlange voll"}
```
Means only **accepted / rejected**. Commands without radio traffic return their result directly in the response.

### 4.3 Event (ESP → host), asynchronous
```json
{"ev":"result","id":17,"ok":true,"ia":"0.0.4","ch":1,"tries":1}
```
Radio commands **always** end with exactly one `result` (success, error or abort) carrying the same `id`.

### 4.4 Error codes (`err`)

| Code | Meaning |
|---|---|
| `unknown_cmd` | unknown command |
| `bad_args` | field missing, wrong type or out of range |
| `no_radio` | CC1101 not detected |
| `no_config` | no valid configuration (domain missing) – transmitting blocked |
| `unknown_channel` | `ia`/`ch` not in the configuration |
| `queue_full` | queue full (64 jobs) |
| `busy` | a procedure is running (pairing, linking …), command not allowed |
| `real_server` | (no longer used; up to 0.8.1: real eNet Server active – server functions blocked) |
| `timeout` | no acknowledgement after all attempts (`result`) |
| `nack` | device replies with error code `rc` ≠ 0 (`result`, field `rc`) |
| `aborted` | aborted (`cancel`, restart, procedure timeout) |
| `not_found` | device/transmitter not found or did not wake up |
| `verify_failed` | verification read after writing does not match |

## 5. Timing (fixed values in the firmware)

| Value | Size | Origin |
|---|---|---|
| Acknowledgement timeout per attempt | 400 ms, +150 ms per retry (400/550/700/850/1000) | actuators reply after 40–60 ms |
| Attempts | 5 (≈ 3.5 s until `timeout`) | server: 3 in a 400 ms grid; since firmware 0.5.2 more, against short interference |
| Channel busy (LBT, waited 30 ms) | retry after 20 ms, does not count as an attempt, at most 20× per job | since 0.5.3; `stats.tx_busy` counts every occurrence |
| Spacing of commands within a scene | after acknowledgement (~70 ms) | server |
| Status polling after a command | 8000 ms, skipped if a status message arrives | server code |
| Reply to a server key | < 50 ms after reception | measured 36–44 ms |
| Waking a transmitter: repetition / maximum duration | 100 ms / 60 s | server ≈ 70–130 ms |
| Pairing: search interval / maximum duration | 3 s / `timeout_s` (default 120 s) | server |
| Queue | 64 jobs | |

## 6. Channels, states, keys

### 6.1 Addressing
Actuator channel `{"ia","ch"}`; from its configuration (section 9) the ESP computes handle offset, channel object and
status GA: `Handle = Offset + GO`, `Status GA = GA base·256 + Offset + 22`, `Channel object = obj`.

### 6.2 State of a channel
`on` (bool), `percent` (0…100, 0 or 100 for switching channels), `known` (bool), `source`
(`status` = status message from the actuator, `poll` = query, `cache` = retrieved from the table). The ESP reports `state`
only on change or on request.

### 6.3 Key presses – kinds (`kind`)
| `kind` | Detection | Reply by ESP | Fields |
|---|---|---|---|
| `server` | SumstatusGet, reply SN = server SN, CC 0x08 | yes (with `server on`) | `code`, `press`, `seq`, `reply_ga` |
| `direkt` (direct) | SumstatusGet to an actuator (CC 0xF0) | no (the actuator replies) | `press`, `hold_ms`, `richtung`, `seq`, `reply_sn`, `reply_ga` |
| `wert` (value) | bare key value (2 bytes) of an unassigned rocker | – | `press`, `hold_ms`, `richtung` |

Encoding of `cmd`: server key: bit 14 = pressed, bits 11–8 = key code. Direct/value: `0x40xx` = left (odd)
key, `0x80xx` = right (even) key of the rocker (`richtung` (direction): `links`/`rechts`; since firmware 0.5.1, previously `oben`/`unten`), lower 14 bits = hold time in 10 ms units (0 = press, > 0 = release after this time).

## 7. Commands

Legend: **F** = requires radio (completion via `result`), **P** = procedure (exclusive, other radio commands → `busy`;
server keys continue to be answered).

### 7.1 Management

| `cmd` | Fields | Response / result |
|---|---|---|
| `hello` | `time` (optional, sets the clock) | `product` (`"enet-bridge"`, since 0.8.1), `fw`, `proto`, `chip` (`esp32`/`esp32c6`, since 0.8.14; missing = `esp32`), `id` (MAC of the board, since 0.8), `radio` {`ok`,`version`,`freq_offset`,`calibrated`,`freqest`,`freqest_n`}, `server` {`on`}, `config` {`crc`,`channels`,`scenes`,`keys`}, `queue` {`len`,`max`}, `uptime_s`, `procedure` (running procedure or `null`) |
| `time` | `time` | sets the clock (for timestamps when programming) |
| `stats` | – | counters: `rx_ok`, `crc_error`, `corrected` (telegrams with 1–3 corrected chip pairs, since 0.5.5), `invalid`, `overflow`, `tx`, `tx_busy` |
| `server` | `on` (optional) | sets or returns `on` (persisted) |
| `config` | `part` (0…n), `last` (bool), `data` (hex, ≤ 400 characters), `crc` with the last part | `ok` for each part; after `last` the ESP verifies and stores and sends `ev:"config"` |
| `radio` | `freq_offset` (−128…127, optional), `save` (bool, optional) | set frequency correction FSCTRL0 immediately (resets the FREQEST average) or store it in NVS; returns `radio` {`ok`,`version`,`freq_offset`,`calibrated` (stored), `freqest` (mean deviation of valid telegrams in FSCTRL0 steps, correct as: FSCTRL0 += freqest), `freqest_n`} – since 0.8 |
| `monitor` | `on` | switches `telegram` events for all telegrams (RX and own TX) |
| `cancel` | – | aborts the running procedure (`result` with `aborted`) |
| `reboot` | – | restart; followed by `start` |

### 7.2 Operation (F)

| `cmd` | Fields | Effect |
|---|---|---|
| `switch` | `ia`, `ch`, `on` | GO 2 = 01/00 |
| `dim` | `ia`, `ch`, `percent` | GO 4 = floor(p·255/100); 0 % switches off |
| `cover` | `ia`, `ch`, `action` (`up`, `down`, `stop`, `step_up`, `step_down`, `position`, `slat`) [, `percent`] | blind (since 0.8.20, untested): GO 2 up/down, GO 3 step, GO 4 stop, GO 5 position, GO 6 slat (0 % = top or open) |
| `set` | `ia`, `handle`, `value` (hex 1–4 bytes) | any GO handle (FuncPropCmd obj 0 PID 62) |
| `read` | `ia`, `handle` | FuncPropStateRead; `result.value` = value |
| `poll` | `ia`, `ch` | read status now (GO 20, GO 21 for dimmers, GO 13 and 12 for blinds) → `state` |
| `state` | `ia`, `ch` (optional) | **no radio:** `state` events from the table (`source:"cache"`), response with `count` |
| `scene` | `nr` | execute a local scene; no individual `result` per action, completion via `scene_done` + `result` |

Result (`result`): `ok`, `ia`, `ch` or `handle`, `tries`, on error `err` (`timeout`/`nack`) and `rc`.

### 7.3 Device management (P)

| `cmd` | Fields | Sequence | Result |
|---|---|---|---|
| `learn` | `ia`, `timeout_s` (default 120), `sn` (optional: only this device) | search (DoA_Read/DoA_SelRead) → `learn_found` → selection (PID 71) → set+verify domain → set+verify IA → read link table GO 1 | `sn`, `ia`, `product` (hex), `ga_basis` |
| `remove` | `ia` | A_Restart type 1, erase code 2 → verification DoA_SN_Read | `ok`, `sn` |
| `link` / `unlink` | `sender` {`ia`,`sn`,`ch`,`art` = `wippe`/`taste`}, `aktor` {`ia`,`sn`,`ch`} | wake transmitter (`wake_wait`/`wake_ok`) → actuator: LSC, links GA n→GO 1, n+1→GO 19 → transmitter: key objects, link actuator status GA → GO 1 → completion → release transmitter | `ok`, individual steps in `msg` on failure |
| `sender_config` | `ia`, `sn`, `channels`: [{`ch`, `mode` (`taste`/`wippe`/`aus`), `code` (key), `reply` {`sn`,`ga`} (server key) or `status` [{`sn`,`ga`}] (direct)}] | wake transmitter → per channel: mode, channel reset (A_Restart erase 6), factory entries, feedback links → release | `ok` |
| `prop_read` | `ia`, `obj`, `pid`, `start` (element, 12 bits), `count` (1…15) | A_PropertyValue_Read 0x3D5, response 0x3D6 (matched via object/PID/start); parameter memory = channel object PID 79, element = byte | `value` (hex), `rssi`; `nack` if count is 0 |
| `prop_write` | like `prop_read` + `data` (hex, ≤ 16 bytes); `count` defaults to the number of bytes | A_PropertyValue_Write 0x3D7, response 0x3D6 | `value` |
| `np_write` | `ia`, `data` (hex: object type, PID, value) | A_NetworkParameter_Write P2P, no response – considered done once sent | – |
| `fp_read` / `fp_cmd` | `ia`, `obj`, `pid`, `data` (hex, ≤ 16 bytes) | FuncPropStateRead 0x2C8 / FuncPropCmd 0x2C7 on object/PID, response 0x2C9 (matched via object/PID). Link table = channel object PID 63: read `00 00 GO idx`, create `00 00 SN GA GO`, delete `02 00 SN GA GO` | `value` (hex, without rc; link: type(1, 01 = factory) SN(6) GA(2) GO(2)), `rssi`; `nack` + `rc` (link table: `rc` 255 = no entry) |
| `scene_call` | `ga`, `nr` (0…63), `learn` (default false) | KNX scene in the actuators: group telegram under the server SN to the scene GA, 1 byte = `nr` (+ 0x80 = store the current state as scene value). No acknowledgement, 3× at 80 ms intervals | `ga`, `tries` (= sent). The actuators report changed states via `state` |

In each member, an eNet scene consists of two links under the server SN: scene GA n → GO 6 (call) and
n+1 → GO 19 (sum status). Creating/deleting the links works like writing parameters (LSC 02 … links … D2 = 04 …
LSC 01, see below). Confirmed 2026-10-28: creating, learning (`learn`), calling and deleting.

The **host** composes parameter writes from these building blocks (integration `params.py`, same sequence as
the eNet Server): LSC (obj 1 PID 6) `02` → device object PID 39 = `…03`,
3A = 00, 41 = 01, 42 = 07 → value in channel object PID 79 → read checksum (PID D1) → D0 = 04, D2 = 04 →
timestamp (device object PID 39) → 3× `np_write 00123c00` → LSC `01` → verification. Device object = catalog
`interfaceObjectIndexUserObjectSystem` (4 for single-channel actuators, 5 for SA2M). A dedicated
`param_write` command in the firmware is not needed (earlier draft).

Timestamps come from the ESP clock; if the clock has not been set, the ESP rejects `param_write`, `link`, `sender_config` with
`bad_args` "Uhr nicht gesetzt" (clock not set).

### 7.4 Debugging

| `cmd` | Fields | Effect |
|---|---|---|
| `send` | `frame` (hex, without CRC) | raw telegram, the ESP sets the LFN (F) |
| `diag` | – | GDO test, chip state, RSSI/CS over 6 s |

## 8. Events

| `ev` | Fields | Trigger |
|---|---|---|
| `start` | `fw`, `proto`, `radio` {`ok`,`version`}, `server` {`on`}, `config` {`crc`}, `reset_reason` | every start; the host resynchronises |
| `key` | `sn`, `ga`, `kind`, `cmd` (hex), `press`, `code` / `hold_ms` / `richtung`, `seq`, `reply_sn`, `reply_ga`, `answered`, `scene` (no. or −1), `battery` (`ok`/`weak`, since FW 0.8.8), `rssi` | every key press; `kind` `server`/`direkt`/`wert` (2 bytes, also the brightness of the sun sensor) /`wert1` (1 byte under the transmitter SN, e.g. scene telegram of the sun sensor, `cmd` 2 digits, since FW 0.8.9) /`wert4` (4 bytes under the transmitter SN, e.g. power/energy of the energy sensor, `cmd` 8 digits, since FW 0.8.21) |
| `state` | `ia`, `ch`, `on`, `percent`, `source` [, blind: `kind`:"blind", `slat`, `move`] | change (status message from the actuator), every query (`source:"poll"`, even if unchanged, since 0.7.1) or request (`cache`) |
| `result` | `id`, `ok`, `err`, `msg`, more depending on the command | completion of an F/P command |
| `scene_done` | `nr`, `failed`, `id` (if triggered by command) | local scene finished |
| `config` | `ok`, `crc`, `channels`, `scenes`, `keys`, `err` | after `config … last` |
| `learn_found` | `sn`, `domain` | device found in learn mode |
| `wake_wait` | `sn`, `timeout_s` | the user should press a key on the transmitter |
| `wake_ok` | `sn` | transmitter awake |
| `progress` | `step`, `of`, `text` | progress of a procedure |
| `real_server` | – | telegram from the real server (at most once per minute); report only, since 0.8.2 without shutting down |
| `radio` | `radio` {as for the `radio` command} | CC1101 no longer responds (version ≠ start value, 2× at 1 s interval) or responds again (re-initialisation every 5 s) – since 0.8 |
| `device_seen` | `sn`, `ia`, `rssi` | first telegram of a device since start (for diagnostics/reachability) |
| `telegram` | `dir` (`rx`/`tx`), `data`, `rssi`, `foff_khz`, `ms` | only with `monitor` |
| `error` | `err`, `msg` | error not related to a command |

## 9. Configuration in the ESP (binary format version 4)

Big endian, terminated by CRC16 (knxrf::crc16). The host transfers it after `hello` if `config.crc` differs.

```
'E' 0x04
Domain(6) ServerSN(6) ServerIA(2)
nCh(1) nAct(1) nScenes(1) nKeys(1)
Channels nCh ×:     IA(2) SN(6) ChannelNo(1) Offset(1) Object(1) Flags(1: bit0 dimmer, bit1 blind) GABase(1)
                    GO(10) – group object numbers of the channel type, 0 = default (see below)
Actions  nAct ×:    IA(2) Handle(2) Length(1) Value(Length ≤ 4) – IA 0: scene call, group telegram to GA = Handle
Scenes   nScenes ×: FirstAction(1) Count(1)
Keys     nKeys ×:   SN(6) GA(2) Scene(1, signed, −1 = report only) – since 0.8.3 only the low GA byte counts (base irrelevant)
CRC16(2)
```
Limits: 48 channels, 128 actions, 32 scenes, 32 keys (≈ 3 KB). New compared to V2: channel number and object.
New in V4 (firmware 0.8.23): GO numbers per channel. Installations switch channels to variants of the default channel
type whose group objects are numbered differently (catalog `ChannelTypes.xml`, `kanaltypen` in `definitionen.json`).
Order of the 10 bytes (`GoSlot` in `src/server.h`):
light `SOO ASC IOO ADV SSGI 0 0 0 0 span`, blind `MUD SSUD STOP SAPBP SAPSP IMUDS SSGI CAPBP CAPSP span`
(span = handles per channel). All 0 for the default types CT_1F01/CT_1F02/CT_1F03. The firmware still accepts
version 3 (without the GO bytes); the host sends version 3 to firmware older than 0.8.23.

## 10. Sequences

### 10.1 Establishing the connection
```
Host opens the port (DTR=0, RTS=0)
Host → {"cmd":"hello","id":1,"time":"…"}
ESP  → {"re":1,"ok":true,"fw":"0.8.0","proto":1,"id":"c8f09e6d1e94","radio":{…,"calibrated":true},"config":{"crc":"1a2b",…},…}
Host → config … (only if crc differs or valid = false; the host builds it from anlage.json, esp_config.py)
Host → calibration (only if calibrated = false or no actuator responds, see 10.6)
Host → {"cmd":"state","id":2}            → ESP: state … (cache) ; the host queries unknown channels with poll
```
If `start` arrives unexpectedly (ESP restart), the host repeats the sequence.

### 10.2 Switching with status
```
Host → switch 0.0.4/1 on (id 5)       ESP → re 5 ok
ESP  ⇢ radio: FuncPropCmd → actuator acknowledges (≈ 45 ms)
ESP  → result 5 ok tries 1
Actuator ⇢ SumstatusInfo (1–4 s)     ESP → state 0.0.4/1 on (status)
   (if no message arrives: poll after 8 s → state … (poll))
```

### 10.3 Server key
```
Transmitter ⇢ SumstatusGet (CC 08, reply to server SN/GA)
ESP  ⇢ SumstatusInfo ff + seq (< 50 ms)          (only with server on)
ESP  → key kind=server code press answered scene
      local scene? → commands one after another → scene_done
      otherwise → host decides (automation)
```

### 10.4 Linking
```
Host → link {sender, aktor} (id 9)     ESP → re 9 ok
ESP  → wake_wait sn …                  (host shows: "Press a key on the transmitter")
ESP  ⇢ PID 72 to the transmitter every 100 ms … transmitter replies
ESP  → wake_ok ; progress 1/n … n/n
ESP  → result 9 ok
```

### 10.5 Updating the firmware (via the host computer)
The integration ships the firmware per chip (`bridge_fw/<chip>/`, built with `tools/fw_bundle.sh`) and flashes
the package matching the `chip` from `hello` itself (empty ESP: esptool detects the chip)
(esptool from the requirements): automatically when `hello` answers as a bridge (`product` = `enet-bridge`;
for firmware < 0.8.1 `proto` is sufficient) and the version is older (once per version and HA start); if nothing
answers after 3 attempts, the update entity "Bridge firmware" offers the installation (with confirmation,
because HA cannot rule out a foreign device on the port).
```
Host closes the port (integration: "Release bridge")
esptool --port <by-id> write_flash 0x10000 firmware.bin   (DTR/RTS reset by esptool)
Host reopens the port (DTR=0, RTS=0) → waits for start → 10.1
```
Configuration, frequency correction and the `server` switch are retained in flash (NVS).

### 10.6 Commissioning a new bridge (without user input)
```
Flash the firmware (10.5, the first time with bootloader/partitions), plug in
Host: hello → config.valid = false   → transfer the configuration from anlage.json
Host: radio.calibrated = false       → coarse search: radio freq_offset = 60, 0, ±30, 90, −60, 120, −90, −120,
                                       for each candidate poll up to 2 actuators until one responds
                                     → fine adjustment: poll all channels 2×, freq_offset += round(freqest), save
Host: normal synchronisation (state, poll)
```
Status queries only – also permitted while the real server is running. The offset belongs to the board
(crystal of the CC1101 module): modules from the same batch were at 60 (≈ 95 kHz), measured 2026-10-29.
Sign verified: offset 50 → freqest +10.3, offset 70 → −9.7, offset 60 → +0.3.
Since 0.8.1 the `server` switch is on for a new bridge (no NVS entry). A real server
is only reported, never shut down automatically (0.8.2) – a false detection must not disable the bridge.

## 11. Mapping in Home Assistant

- One eNet device = one HA device (name, room, type, SN, firmware); the bridge is a device of its own.
- Entities: switching channel → `switch`/`light`; dimming channel → `light` (brightness); blind channel → `cover`; transmitter channel → `event`
  (`gedrückt` (pressed), `losgelassen` (released), `lang` (long)); parameters → `number`/`select`/`switch` (configuration category);
  diagnostics → `sensor` (RSSI, last message, GA base).
- Bridge entities: server functions (`switch`), real server detected (`binary_sensor`), radio module
  (`binary_sensor`, connectivity), frequency correction (`sensor`, kHz, disabled by default; search and tracking are automatic), queue,
  reception statistics (`sensor`), firmware (`update`).
- Device registry in the host (HA store `enet_anlage_<entry>`, since integration 0.3): set up from the
  .iex export (migration), as a new installation (domain/server SN generated: `000a` + 4 random bytes) or from anlage.json;
  new devices via `learn` + `param_read`. The server switch (`server.funktionen`) belongs to the installation and is
  transferred to the bridge during synchronisation.
- Device definitions per type (own format): channels, parameters (object, start, length, encoding, range,
  default). Generated locally from the catalog of the server firmware or measured independently.

## 12. Safety and operation

- Server functions start **on** from the factory state (since 0.8.1); the `server` switch is persisted and is
  changed only by the host.
- If the ESP detects a telegram from the real server (sender 7.15.255 under the domain or server SN), it reports
  `real_server` (≤ 1×/min); HA shows "Real eNet Server active" until 5 min afterwards. Switching off is manual only
  (decision 2026-10-29: self-shutdown = risk that the bridge stays permanently inactive).
- Without a configuration (domain) no transmitting except `send` and `learn` with a `bad_args` check.
- Encrypted installations (security telegrams) are not supported; the host detects them from the export.

## 13. Transition from firmware 0.4

| today (text) | future (JSON) |
|---|---|
| `cmd <IA> <Handle> <Hex>` / `get` | `set` / `read` (direct, without queue: dropped) |
| `set` / `read` (queue) | `set` / `read` |
| `server an\|aus`, `szene <n>`, `zustand`, `cfg begin/…/end` | `server`, `scene`, `state`, `config` |
| `send`, `v`, `r`, `s`, `d`, `w` | remain as text commands for debugging |
| events `event:"…"` | `ev:"…"` with the fields from section 8 |
| procedures in `tools/pair.py`, `tools/link_devices.py` | `learn`, `link`, `unlink` in the ESP |

## 14. Gaps and open issues

| # | Gap | Impact | Proposal |
|---|---|---|---|
| 1 | **Server identity for a new installation** (domain, server SN) | no domain without an eNet Server | derive domain/SN from the ESP MAC, check prefix (Insta SNs start with 000a) |
| 2 | ~~Battery level of transmitters~~ | done (FW 0.8.8 / integration 0.9.10) | RF info bit 1 in the `key` event as `battery`; HA: binary sensor "Battery" per transmitter. Value "weak" not yet seen in a capture |
| 3 | **Room scenes in the actuator** (GO 6, SCV1) | possibly faster/more elegant than individual commands | capture "light off room" |
| 4 | **Linking actuator channel 2** | only read, never written | test Stylos K2 ↔ free rocker |
| 5 | **Scene contents from the .iex** | migration of scenes | decrypt value lists in .blb |
| 6 | **Meaning of `00 12 3c 00`**, PID 72 `04 05 …`, load state values | only replicated | keep; look up in catalog/code |
| 7 | **Transmitter with several actuators per rocker** (W2 → Stylos K1+K2) | `link` adds only one status link | `sender_config` sets all status links of a channel in one pass |
| 8 | **Device type from product number** during pairing | host must know the type | mapping product number → type in the catalog (DeviceSoftware.xml) |
| 9 | **Parameter checksum** (PID D1) | only read, meaning unclear | compare before/after `param_write` as success criterion |
| 10 | **Reachability** of a device | HA "unavailable" | derive from `result timeout` + `device_seen`, no separate polling |
| 11 | **Multiple bridges / range** | large house | not in v1; hops field ignored |
| 12 | **Blinds, REG, sensors** | not in v1 | later via device definitions + connection codes |
| 13 | **Firmware update from HA** (`update` entity) | convenience | adopt section 10.5 into the integration |
| 14 | **Clock in the ESP** | timestamps when programming | host sets it via `hello`/`time` (no RTC/NTP needed) |

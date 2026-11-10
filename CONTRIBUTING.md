# Contributing

Contributions are welcome – pull requests, bug reports, test results from other installations and device
captures alike. This page explains how the project is organised and what to keep in mind.

## Repository layout

| Path | Content |
|---|---|
| `custom_components/enet_bridge/` | Home Assistant integration (Python) |
| `custom_components/enet_bridge/bridge_fw/` | bundled bridge firmware per chip (built by `tools/fw_bundle.sh`) |
| `custom_components/enet_bridge/definitionen.json` | device definitions generated from the manufacturer catalogue (`tools/katalog.py`) |
| `src/` | bridge firmware (ESP32 / ESP32-C6 + CC1101, PlatformIO, Arduino framework) |
| `src/knxrf.*`, `src/server.*`, `src/rauschen.h` | platform-independent protocol logic with host tests |
| `test/` | host tests of the protocol logic (`pio test -e native`) |
| `tools/` | command-line helpers (catalogue import, firmware bundle, radio tests, pairing/linking without HA) |
| `docs/` | hardware guide, serial interface between bridge and HA (`docs/INTERFACE.md`) |

Important modules of the integration: `hub.py` (central state, sync with the bridge), `bridge.py` (serial link),
`pair.py` (pairing/removing devices), `sender.py` (programming transmitters), `scenes.py` / `ha_scenes.py`
(KNX scenes stored in the actuators, HA scenes as the source), `params.py` (device parameters), `check.py`
(installation checks, repairs), `measurements.py` (energy sensor, motion detector, repeater), `texts.py`
(UI texts not covered by Home Assistant's translations).

## Before you open a pull request

- **Host tests:** `pio test -e native` – all tests must pass. New protocol logic in `src/` gets a test.
- **Firmware:** changes in `src/` must build for both chips: `tools/fw_bundle.sh` builds `esp32dev` and
  `xiao_esp32c6` and copies the result into `bridge_fw/`. Bump `kFwVersion` in `src/main.cpp` when the firmware
  changes; the integration updates bridges automatically.
- **Integration:** bump `version` in `manifest.json`. The code has to run on the current Home Assistant release.
- **UI texts:** never hard-code user-visible text. Setup, entities, services and repairs go into `strings.json` and
  `translations/*.json` (English, German, Norwegian Bokmål). Everything else goes through `texts.tr()` with an
  entry in `texts.py` (German and English) and optionally `texts_nb.py` (Norwegian; missing keys fall back to
  English). Log messages are English and not translated.
- **Compatibility:** do not rename stored keys, entity unique IDs, service names, event fields or JSON protocol
  fields (see below) – they live in existing installations and user automations.
- **No private data:** never commit exports (`.iex`), installation files, captures with real serial numbers or
  domains, or IP addresses. Findings from captures are welcome, raw data stays local.

## Code and language

Code, comments and documentation are English. A few names are German for historical reasons and stay German
because they are stored in installations or used in automations and in the protocol to the bridge.

### Glossary of German names that stay

**Stored installation data** (HA storage `enet_bridge_anlage_<entry>`, also `anlage.json` from older versions):

| Key | Meaning |
|---|---|
| `geraete` | devices |
| `name`, `ia`, `sn`, `typ`, `raum` | name, individual address, serial number, device type (`DVT_…`), room |
| `rolle` | role: `aktor` (actuator), `sender` (transmitter), `sensor`, `unbekannt` (unknown) |
| `beschreibung`, `produkt`, `eingelernt` | description, product number, paired at |
| `kanaele` | channels: `nr`, `art`, `offset` (group object offset), `obj` (interface object) |
| `art` (channel) | `schalten` switch, `dimmen` dimmer, `jalousie` blind, `energie` energy sensor, `bewegung` motion detector, `repeater` |
| `tasten` | keys/rockers of a transmitter: `art` `wippe` (rocker) / `taste` (single key), `ziele` (target channels), `ha_ziele` (HA lamps), `antwort_ga` (reply group address) |
| `ga_basis`, `ga_basis_geprueft` | group address base of a device (changes with every factory reset), checked flag |
| `zentral` | central key targets of a wall transmitter |
| `sensor_aktionen` | actions of the sun sensor channels |
| `server` | emulated eNet server: `domain`, `sn`, `ia` |
| `esp` | configuration for the bridge: `szenen` (local scenes), `server_tasten` (server keys) |
| `szenen` | scenes from a server export |
| `entfernt` | removed devices (name/assignment kept for re-pairing) |

**Stored scenes** (`enet_bridge_szenen_<entry>`): `mitglieder` (members), `werte` (values written into the
actuators), `offen` (members whose sync failed); mapped to English field names in `scenes.py`.

**Event `enet_event`** (used in automations and blueprints): `richtung` (direction: `links` left / `rechts`
right), `hold_ms`, `press`.

**Serial protocol to the bridge** (`docs/INTERFACE.md`): key event `kind` values `direkt` (direct link), `wert`
(2-byte value), `wert1` / `wert4` (1-/4-byte value); diagnostics fields `grundrauschen` (noise floor),
`schwelle` (threshold), `cs_anteil` (busy share).

**Config flow steps and translation keys** in `strings.json` partly use German ids (e.g. `async_step_suchen`) –
they are internal ids, the texts themselves are translated.

## Testing on real hardware

Devices marked "(untested)" in Home Assistant were implemented from the manufacturer catalogue only. If you own
one, a report is very valuable: describe what works and attach the file from "Download diagnostics" of the
integration – telegrams the integration cannot assign yet are listed there.

#pragma once

// ---------------------------------------------------------------------------
// Wiring <-> CC1101; CC1101 VCC -> 3V3 (NOT 5 V!), GND -> GND
// ---------------------------------------------------------------------------
#if defined(CONFIG_IDF_TARGET_ESP32C6)
// ESP32-C6: pinout of the busware CUL32 – one firmware for CUL32 and Seeed XIAO ESP32-C6 (XIAO pin in brackets).
// GDO2 is not needed (carrier sense via PKTSTATUS): on the CUL32 GPIO 3, on the XIAO not broken out
// (antenna switch 2.4 GHz there). Leave free: strapping 8/9/15.
#define PIN_SCK   19   // (D8)
#define PIN_MISO  20   // (D9)
#define PIN_MOSI  21   // (D3)
#define PIN_CS    18   // (D10)
#define PIN_GDO0   2   // (D2)
#define PIN_GDO2  -1   // not connected
#define PIN_LED   15   // XIAO: yellow user LED, active low – flashes on transmit/receive (CUL32: GPIO 15 free)
#else
// ESP32-DevKit (VSPI)
#define PIN_SCK   18
#define PIN_MISO  19
#define PIN_MOSI  23
#define PIN_CS     5
#define PIN_GDO0   4   // IOCFG0 = 0x06: high from sync word, low at end of packet
#define PIN_GDO2  16   // IOCFG2 = 0x0E: carrier sense (diagnostics only)
#define PIN_LED   -1   // DevKit: no LED for radio traffic
#endif

// ---------------------------------------------------------------------------
// Radio
// ---------------------------------------------------------------------------
// Fine frequency correction (FSCTRL0, steps of ~1.59 kHz, signed): only the initial value while the
// board is not calibrated. Afterwards the value in NVS applies ("foff", command "radio", HA "Funk kalibrieren").
// Green 26 MHz modules: crystal ~110 ppm off -> 60 (measured 2026-10-26, second bridge 2026-10-29).
#define CC1101_FREQ_OFFSET  60

// If nothing is received although telegrams are present: FSK polarity
// might be inverted. Then set to 1.
#define KNXRF_INVERT_CHIPS  0

// Transmit power (PATABLE[0]). 868 MHz: 0xC0 ~ +10 dBm, 0x50 ~ 0 dBm, 0x27 ~ -10 dBm.
#define CC1101_PA_POWER  0xC0

// ---------------------------------------------------------------------------
// eNet installation: initial values of the test installation; in operation HA transfers domain and server ID (config)
// ---------------------------------------------------------------------------
#define ENET_DOMAIN     {0x00, 0x0A, 0x12, 0x34, 0x56, 0x00}
#define ENET_SERVER_SN  {0x00, 0x0A, 0x65, 0x43, 0x21, 0x15}

// Serial output
#define SERIAL_BAUD 115200

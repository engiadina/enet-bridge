#pragma once

// ---------------------------------------------------------------------------
// Wiring <-> CC1101; CC1101 VCC -> 3V3 (NOT 5 V!), GND -> GND
// ---------------------------------------------------------------------------
#if defined(CONFIG_IDF_TARGET_ESP32C6)
// ESP32-C6: pinout of the busware CUL32 – one firmware for CUL32 and Seeed XIAO ESP32-C6 (XIAO pin in brackets).
// GDO2 is not needed (carrier sense via PKTSTATUS): on the CUL32 GPIO 3, on the XIAO not broken out
// (antenna switch 2.4 GHz there). Strapping 8/9/15: only outputs after boot (the LEDs), no other use.
#define PIN_SCK   19   // (D8)
#define PIN_MISO  20   // (D9)
#define PIN_MOSI  21   // (D3)
#define PIN_CS    18   // (D10)
#define PIN_GDO0   2   // (D2)
#define PIN_GDO2  -1   // not connected
#define PIN_HIGH  15   // kept high: XIAO yellow user LED off (active low, not used); CUL32 EEPROM write protection WC
#define PIN_LED2   8   // CUL32: blue status LED, flashes on transmit/receive (XIAO: GPIO 8 not connected)
#define LED2_ON   LOW  // CUL32 LED is active low (like the XIAO one)
#define PIN_LED3   0   // (D0) optional external LED, same function: D0 -> 330 ohm -> LED -> GND
#define PIN_LED3_CUL32 -1 // none on the CUL32: GPIO 0 is INTn of the W5500 on busware's NET4CUL, GPIO 6/7 are kept
                          // free for the REG bus; the blue LED shows radio traffic
#define LED3_ON  HIGH  // same function as the on-board LEDs
#else
// ESP32-DevKit (VSPI)
#define PIN_SCK   18
#define PIN_MISO  19
#define PIN_MOSI  23
#define PIN_CS     5
#define PIN_GDO0   4   // IOCFG0 = 0x06: high from sync word, low at end of packet
#define PIN_GDO2  16   // IOCFG2 = 0x0E: carrier sense (diagnostics only)
#define PIN_HIGH  -1
#define PIN_LED2  -1
#define LED2_ON  HIGH
#define PIN_LED3  -1
#define PIN_LED3_CUL32 -1
#define LED3_ON  HIGH
#endif

// ---------------------------------------------------------------------------
// Radio
// ---------------------------------------------------------------------------
// Fine frequency correction (FSCTRL0, steps of ~1.59 kHz, signed): only the initial value while the
// board is not calibrated. Afterwards the value in NVS applies ("foff", command "radio", HA "Funk kalibrieren").
// Cheap 8-pin "CC1101 868MHz" modules: crystal ~110 ppm off -> 60 (measured 2026-10-26, second bridge 2026-10-29).
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

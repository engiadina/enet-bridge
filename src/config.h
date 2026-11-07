#pragma once

// ---------------------------------------------------------------------------
// Verkabelung ESP32-DevKit <-> CC1101 (VSPI)
//   CC1101 VCC -> 3V3 (NICHT 5 V!), GND -> GND
// ---------------------------------------------------------------------------
#define PIN_SCK   18
#define PIN_MISO  19
#define PIN_MOSI  23
#define PIN_CS     5
#define PIN_GDO0   4   // IOCFG0 = 0x06: high ab Sync-Wort, low bei Paketende
#define PIN_GDO2  16   // IOCFG2 = 0x0E: Carrier Sense (nur Diagnose)

// ---------------------------------------------------------------------------
// Funk
// ---------------------------------------------------------------------------
// Feinkorrektur der Frequenz (FSCTRL0, Schritte zu ~1,59 kHz, signed): nur der Startwert, solange die
// Platine nicht kalibriert ist. Danach gilt der Wert im NVS ("foff", Befehl "radio", HA "Funk kalibrieren").
// Gruene 26-MHz-Module: Quarz ~110 ppm daneben -> 60 (gemessen 2026-10-26, zweite Bridge 2026-10-29).
#define CC1101_FREQ_OFFSET  60

// Falls nichts empfangen wird, obwohl Telegramme da sind: FSK-Polaritaet
// koennte vertauscht sein. Dann auf 1 setzen.
#define KNXRF_INVERT_CHIPS  0

// Sendeleistung (PATABLE[0]). 868 MHz: 0xC0 ~ +10 dBm, 0x50 ~ 0 dBm, 0x27 ~ -10 dBm.
#define CC1101_PA_POWER  0xC0

// ---------------------------------------------------------------------------
// eNet-Anlage: Startwerte der Testanlage; im Betrieb ueberträgt HA Domaene und Server-Kennung (Konfiguration)
// ---------------------------------------------------------------------------
#define ENET_DOMAIN     {0x00, 0x0A, 0x12, 0x34, 0x56, 0x00}
#define ENET_SERVER_SN  {0x00, 0x0A, 0x65, 0x43, 0x21, 0x15}

// Serielle Ausgabe
#define SERIAL_BAUD 115200

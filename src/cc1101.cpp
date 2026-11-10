#include "cc1101.h"

#include "config.h"

// KNX RF 1.1 / wM-Bus Mode S: 868,3 MHz, 2-FSK, 32,768 kBaud (Chips), Hub 47,6 kHz,
// Kanalbandbreite 270 kHz, Sync 0x7696 (16/16, ohne Carrier Sense), Laenge unendlich,
// kein HW-Manchester, keine HW-CRC (beides in Software, siehe knxrf.cpp).
static const uint8_t kConfig[][2] = {
    {CC1101::IOCFG2, 0x0E},    // GDO2: Carrier Sense
    {CC1101::IOCFG1, 0x2E},    // GDO1: hochohmig (= MISO)
    {CC1101::IOCFG0, 0x06},    // GDO0: Sync empfangen .. Paketende
    {CC1101::FIFOTHR, 0x07},
    {CC1101::SYNC1, 0x76},
    {CC1101::SYNC0, 0x96},
    {CC1101::PKTLEN, 0xFF},
    {CC1101::PKTCTRL1, 0x00},  // kein Status-Anhang, keine Adresspruefung
    {CC1101::PKTCTRL0, 0x02},  // FIFO, kein CRC, unendliche Laenge
    {CC1101::ADDR, 0x00},
    {CC1101::CHANNR, 0x00},
    {CC1101::FSCTRL1, 0x08},   // ZF 203 kHz
    {CC1101::FSCTRL0, 0x00},   // wird durch freqOffset ueberschrieben
    {CC1101::FREQ2, 0x21},     // 868,300 MHz
    {CC1101::FREQ1, 0x65},
    {CC1101::FREQ0, 0x6A},
    {CC1101::MDMCFG4, 0x6A},   // BW 270 kHz, DRATE_E 10
    {CC1101::MDMCFG3, 0x4A},   // DRATE_M 74 -> 32,73 kBaud
    {CC1101::MDMCFG2, 0x02},   // 2-FSK, kein Manchester, Sync 16/16 (ohne CS: +6..8 dB, Messung 2026-11-05)
    {CC1101::MDMCFG1, 0x22},   // 4 Byte Praeambel (TX), CHANSPC_E 2
    {CC1101::MDMCFG0, 0xF8},
    {CC1101::DEVIATN, 0x47},   // 47,6 kHz
    {CC1101::MCSM2, 0x07},
    {CC1101::MCSM1, 0x00},     // nach RX/TX -> IDLE
    {CC1101::MCSM0, 0x18},     // Autokalibrierung IDLE->RX/TX
    {CC1101::FOCCFG, 0x2E},
    {CC1101::BSCFG, 0xBF},
    {CC1101::AGCCTRL2, 0x03},  // volle DVGA-Verstaerkung (0x43 sperrte die hoechste Stufe)
    {CC1101::AGCCTRL1, 0x06},  // CS-Schwelle MAGN_TARGET +6 dB, nur fuer Listen-before-talk (GDO2): mit voller
                               // DVGA meldete 0x00 den leeren Kanal mit Antenne zu ~45 % belegt, 0x06 zu ~0 %
    {CC1101::AGCCTRL0, 0xB5},
    {CC1101::FREND1, 0xB6},
    {CC1101::FREND0, 0x10},
    {CC1101::FSCAL3, 0xEA},
    {CC1101::FSCAL2, 0x2A},
    {CC1101::FSCAL1, 0x00},
    {CC1101::FSCAL0, 0x1F},
    {CC1101::TEST2, 0x81},
    {CC1101::TEST1, 0x35},
    {CC1101::TEST0, 0x09},
};

void CC1101::select() {
  SPI.beginTransaction(spi_);
  digitalWrite(cs_, LOW);
  uint32_t t0 = micros();
  while (digitalRead(PIN_MISO) && micros() - t0 < 2000) {
  }  // CHIP_RDYn abwarten
}

void CC1101::deselect() {
  digitalWrite(cs_, HIGH);
  SPI.endTransaction();
}

void CC1101::strobe(uint8_t s) {
  select();
  SPI.transfer(s);
  deselect();
}

void CC1101::writeReg(uint8_t addr, uint8_t v) {
  select();
  SPI.transfer(addr);
  SPI.transfer(v);
  deselect();
}

uint8_t CC1101::readReg(uint8_t addr) {
  select();
  SPI.transfer(addr | 0x80);
  uint8_t v = SPI.transfer(0);
  deselect();
  return v;
}

uint8_t CC1101::readStatus(uint8_t addr) {
  select();
  SPI.transfer(addr | 0xC0);
  uint8_t v = SPI.transfer(0);
  deselect();
  return v;
}

void CC1101::writeBurst(uint8_t addr, const uint8_t* buf, size_t n) {
  select();
  SPI.transfer(addr | 0x40);
  for (size_t i = 0; i < n; ++i) SPI.transfer(buf[i]);
  deselect();
}

bool CC1101::begin(int8_t freqOffset) {
  pinMode(cs_, OUTPUT);
  digitalWrite(cs_, HIGH);
  pinMode(gdo0_, INPUT);
  if (gdo2_ >= 0) pinMode(gdo2_, INPUT_PULLDOWN);  // offen = "Kanal frei"
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, cs_);

  // Manueller Power-on-Reset laut Datenblatt
  digitalWrite(cs_, LOW);
  delayMicroseconds(10);
  digitalWrite(cs_, HIGH);
  delayMicroseconds(50);
  strobe(SRES);
  delay(2);

  uint8_t v = version();
  if (v == 0x00 || v == 0xFF) return false;  // kein Chip / Verkabelung

  for (auto& r : kConfig) writeReg(r[0], r[1]);
  writeReg(FSCTRL0, (uint8_t)freqOffset);
  writeReg(PATABLE, CC1101_PA_POWER);
  strobe(SCAL);
  delay(1);
  return readReg(SYNC1) == 0x76 && readReg(MDMCFG3) == 0x4A;
}

void CC1101::idle() {
  strobe(SIDLE);
  uint32_t t0 = millis();
  while (marcState() != 0x01 && millis() - t0 < 5) {
  }
}

void CC1101::startRx() {
  idle();
  strobe(SFRX);
  strobe(SRX);
}

uint8_t CC1101::rxBytes() {
  // Errata: Statusregister zweimal gleich lesen
  uint8_t a, b;
  do {
    a = readStatus(RXBYTES);
    b = readStatus(RXBYTES);
  } while (a != b);
  return a & 0x7F;
}

bool CC1101::carrierSense() {
  if (rauschen_.bereit()) return rauschen_.belegt(rssiDbm());
  return gdo2_ >= 0 ? digitalRead(gdo2_) : (readStatus(PKTSTATUS) & 0x40);
}

bool CC1101::rxOverflow() { return readStatus(RXBYTES) & 0x80; }

void CC1101::readFifo(uint8_t* buf, uint8_t n) {
  if (!n) return;
  select();
  SPI.transfer(0x3F | 0xC0);  // Burst-Read RX-FIFO
  for (uint8_t i = 0; i < n; ++i) buf[i] = SPI.transfer(0);
  deselect();
}

float CC1101::rssiDbm() {
  int8_t raw = (int8_t)readStatus(RSSI);
  return raw / 2.0f - 74.0f;
}

void CC1101::setFreqOffset(int8_t freqOffset) {
  idle();
  writeReg(FSCTRL0, (uint8_t)freqOffset);
  strobe(SCAL);
  delay(1);
  startRx();
}

// Senden ohne Hardware-Praeambel/Sync: Der Chip-Strom enthaelt Mode-S-Praeambel und
// 18-Chip-Sync selbst (nicht byte-ausgerichtet, daher nicht per SYNC1/0 moeglich).
// Feste Paketlaenge, FIFO (64 Byte) wird waehrend des Sendens nachgefuellt.
CC1101::TxResult CC1101::transmit(const uint8_t* chips, size_t len, uint16_t maxWaitMs) {
  if (len == 0 || len > 255) return TX_TOO_LONG;

  // Listen before talk: im RX auf freien Kanal warten
  uint32_t t0 = millis();
  lbtUeberschritten_ = false;
  while (carrierSense()) {
    if (millis() - t0 >= maxWaitMs) {
      lbtUeberschritten_ = true;
      break;
    }
    delayMicroseconds(500);
  }

  idle();
  strobe(SFTX);
  const uint8_t mdmcfg2Rx = readReg(MDMCFG2);  // Empfangs-Sync-Modus (auch per Befehl "w" geaendert)
  writeReg(MDMCFG2, 0x00);   // 2-FSK, ohne Praeambel/Sync
  writeReg(PKTCTRL0, 0x00);  // feste Laenge
  writeReg(PKTLEN, (uint8_t)len);

  size_t sent = len < 64 ? len : 64;
  writeBurst(FIFO, chips, sent);
  strobe(STX);

  TxResult res = TX_OK;
  // 1 Byte = 8 Chips / 32,768 kchip/s = 244 us
  const uint32_t timeoutUs = (uint32_t)len * 244 + 20000;
  const uint32_t t1 = micros();
  while (true) {
    uint8_t st = readStatus(TXBYTES);
    if (st & 0x80) {
      res = TX_UNDERFLOW;
      break;
    }
    if (sent < len) {
      uint8_t free = 64 - (st & 0x7F);
      if (free > 8) {
        size_t n = (len - sent) < free ? (len - sent) : free;
        writeBurst(FIFO, chips + sent, n);
        sent += n;
      }
    } else if (marcState() == 0x01) {  // IDLE -> fertig (MCSM1: TX -> IDLE)
      break;
    }
    if (micros() - t1 > timeoutUs) {
      res = TX_TIMEOUT;
      break;
    }
    delayMicroseconds(100);
  }

  idle();
  strobe(SFTX);
  writeReg(MDMCFG2, mdmcfg2Rx);  // zurueck auf den Empfangs-Sync-Modus
  writeReg(PKTCTRL0, 0x02);
  writeReg(PKTLEN, 0xFF);
  startRx();
  return res;
}

void CC1101::dumpRegisters(Print& out) {
  out.print("{\"event\":\"registers\",\"regs\":\"");
  for (uint8_t a = 0; a <= TEST0; ++a) {
    uint8_t v = readReg(a);
    if (v < 0x10) out.print('0');
    out.print(v, HEX);
  }
  out.println("\"}");
}

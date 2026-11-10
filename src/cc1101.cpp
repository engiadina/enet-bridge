#include "cc1101.h"

#include "config.h"

// KNX RF 1.1 / wM-Bus Mode S: 868.3 MHz, 2-FSK, 32.768 kBaud (chips), deviation 47.6 kHz,
// channel bandwidth 270 kHz, sync 0x7696 (16/16, without carrier sense), infinite length,
// no HW Manchester, no HW CRC (both in software, see knxrf.cpp).
static const uint8_t kConfig[][2] = {
    {CC1101::IOCFG2, 0x0E},    // GDO2: carrier sense
    {CC1101::IOCFG1, 0x2E},    // GDO1: high impedance (= MISO)
    {CC1101::IOCFG0, 0x06},    // GDO0: sync received .. end of packet
    {CC1101::FIFOTHR, 0x07},
    {CC1101::SYNC1, 0x76},
    {CC1101::SYNC0, 0x96},
    {CC1101::PKTLEN, 0xFF},
    {CC1101::PKTCTRL1, 0x00},  // no status append, no address check
    {CC1101::PKTCTRL0, 0x02},  // FIFO, no CRC, infinite length
    {CC1101::ADDR, 0x00},
    {CC1101::CHANNR, 0x00},
    {CC1101::FSCTRL1, 0x08},   // IF 203 kHz
    {CC1101::FSCTRL0, 0x00},   // overwritten by freqOffset
    {CC1101::FREQ2, 0x21},     // 868.300 MHz
    {CC1101::FREQ1, 0x65},
    {CC1101::FREQ0, 0x6A},
    {CC1101::MDMCFG4, 0x6A},   // BW 270 kHz, DRATE_E 10
    {CC1101::MDMCFG3, 0x4A},   // DRATE_M 74 -> 32,73 kBaud
    {CC1101::MDMCFG2, 0x02},   // 2-FSK, no Manchester, sync 16/16 (without CS: +6..8 dB, measured 2026-11-05)
    {CC1101::MDMCFG1, 0x22},   // 4 bytes preamble (TX), CHANSPC_E 2
    {CC1101::MDMCFG0, 0xF8},
    {CC1101::DEVIATN, 0x47},   // 47.6 kHz
    {CC1101::MCSM2, 0x07},
    {CC1101::MCSM1, 0x00},     // after RX/TX -> IDLE
    {CC1101::MCSM0, 0x18},     // auto-calibration IDLE->RX/TX
    {CC1101::FOCCFG, 0x2E},
    {CC1101::BSCFG, 0xBF},
    {CC1101::AGCCTRL2, 0x03},  // full DVGA gain (0x43 blocked the highest step)
    {CC1101::AGCCTRL1, 0x06},  // CS threshold MAGN_TARGET +6 dB, only for listen-before-talk (GDO2): with full
                               // DVGA, 0x00 reported the empty channel with antenna as ~45 % busy, 0x06 as ~0 %
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
  }  // wait for CHIP_RDYn
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
  if (gdo2_ >= 0) pinMode(gdo2_, INPUT_PULLDOWN);  // open = "channel free"
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, cs_);

  // Manual power-on reset according to the datasheet
  digitalWrite(cs_, LOW);
  delayMicroseconds(10);
  digitalWrite(cs_, HIGH);
  delayMicroseconds(50);
  strobe(SRES);
  delay(2);

  uint8_t v = version();
  if (v == 0x00 || v == 0xFF) return false;  // no chip / wiring

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
  // Errata: read the status register twice until equal
  uint8_t a, b;
  do {
    a = readStatus(RXBYTES);
    b = readStatus(RXBYTES);
  } while (a != b);
  return a & 0x7F;
}

bool CC1101::carrierSense() {
  if (noise_.ready()) return noise_.busy(rssiDbm());
  return gdo2_ >= 0 ? digitalRead(gdo2_) : (readStatus(PKTSTATUS) & 0x40);
}

bool CC1101::rxOverflow() { return readStatus(RXBYTES) & 0x80; }

void CC1101::readFifo(uint8_t* buf, uint8_t n) {
  if (!n) return;
  select();
  SPI.transfer(0x3F | 0xC0);  // burst read RX FIFO
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

// Transmit without hardware preamble/sync: the chip stream itself contains the Mode S preamble and
// 18-chip sync (not byte-aligned, hence not possible via SYNC1/0).
// Fixed packet length, the FIFO (64 bytes) is refilled during transmission.
CC1101::TxResult CC1101::transmit(const uint8_t* chips, size_t len, uint16_t maxWaitMs) {
  if (len == 0 || len > 255) return TX_TOO_LONG;

  // Listen before talk: wait in RX for a free channel
  uint32_t t0 = millis();
  lbtOverrun_ = false;
  while (carrierSense()) {
    if (millis() - t0 >= maxWaitMs) {
      lbtOverrun_ = true;
      break;
    }
    delayMicroseconds(500);
  }

  idle();
  strobe(SFTX);
  const uint8_t mdmcfg2Rx = readReg(MDMCFG2);  // receive sync mode (also changed via command "w")
  writeReg(MDMCFG2, 0x00);   // 2-FSK, without preamble/sync
  writeReg(PKTCTRL0, 0x00);  // fixed length
  writeReg(PKTLEN, (uint8_t)len);

  size_t sent = len < 64 ? len : 64;
  writeBurst(FIFO, chips, sent);
  strobe(STX);

  TxResult res = TX_OK;
  // 1 byte = 8 chips / 32.768 kchip/s = 244 us
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
    } else if (marcState() == 0x01) {  // IDLE -> done (MCSM1: TX -> IDLE)
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
  writeReg(MDMCFG2, mdmcfg2Rx);  // back to the receive sync mode
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

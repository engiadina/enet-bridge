#pragma once
// Minimal CC1101 driver for KNX RF (receive and transmit).
#include <Arduino.h>
#include <SPI.h>

#include "noise_floor.h"

class CC1101 {
 public:
  // Register
  enum : uint8_t {
    IOCFG2 = 0x00, IOCFG1, IOCFG0, FIFOTHR, SYNC1, SYNC0, PKTLEN, PKTCTRL1, PKTCTRL0, ADDR,
    CHANNR, FSCTRL1, FSCTRL0, FREQ2, FREQ1, FREQ0, MDMCFG4, MDMCFG3, MDMCFG2, MDMCFG1, MDMCFG0,
    DEVIATN, MCSM2, MCSM1, MCSM0, FOCCFG, BSCFG, AGCCTRL2, AGCCTRL1, AGCCTRL0, WOREVT1, WOREVT0,
    WORCTRL, FREND1, FREND0, FSCAL3, FSCAL2, FSCAL1, FSCAL0, RCCTRL1, RCCTRL0, FSTEST, PTEST,
    AGCTEST, TEST2, TEST1, TEST0
  };
  // Status registers (read with burst bit)
  enum : uint8_t {
    PARTNUM = 0x30, VERSION = 0x31, FREQEST = 0x32, LQI = 0x33, RSSI = 0x34, MARCSTATE = 0x35,
    PKTSTATUS = 0x38, TXBYTES = 0x3A, RXBYTES = 0x3B
  };
  // Strobes
  enum : uint8_t { PATABLE = 0x3E, FIFO = 0x3F };
  enum : uint8_t {
    SRES = 0x30, SFSTXON = 0x31, SXOFF = 0x32, SCAL = 0x33, SRX = 0x34, STX = 0x35, SIDLE = 0x36,
    SFRX = 0x3A, SFTX = 0x3B, SNOP = 0x3D
  };

  CC1101(uint8_t cs, uint8_t gdo0, int8_t gdo2) : cs_(cs), gdo0_(gdo0), gdo2_(gdo2) {}  // gdo2 -1: none

  bool begin(int8_t freqOffset);          // reset + KNX RF configuration
  void startRx();                         // IDLE, flush FIFO, RX
  bool syncDetected() { return digitalRead(gdo0_); }
  uint8_t rxBytes();                      // number of bytes in the RX FIFO (errata-safe)
  bool rxOverflow();
  void readFifo(uint8_t* buf, uint8_t n);
  float rssiDbm();
  int8_t freqEst() { return (int8_t)readStatus(FREQEST); }  // steps like FSCTRL0 (~1.59 kHz)
  float freqOffsetKhz() { return freqEst() * 26000.0f / 16384.0f; }
  void setFreqOffset(int8_t freqOffset);  // change FSCTRL0 during operation (IDLE, calibrate, RX)
  uint8_t version() { return readStatus(VERSION); }
  uint8_t marcState() { return readStatus(MARCSTATE) & 0x1F; }
  // Channel busy: RSSI > estimated noise floor + 10 dB (noise.h); before the first samples, as before,
  // GDO2 or PKTSTATUS.CS (bit 6, fixed threshold AGCCTRL1)
  bool carrierSense();
  void noiseSample() { noise_.sample(rssiDbm()); }  // call regularly in RX (loop, ~5 ms)
  const NoiseFloor& noise() const { return noise_; }
  bool lbtOverrun() const { return lbtOverrun_; }  // last transmit: sent despite busy channel
  void dumpRegisters(Print& out);

  enum TxResult : uint8_t { TX_OK, TX_BUSY, TX_UNDERFLOW, TX_TIMEOUT, TX_TOO_LONG };
  // Transmits a ready chip stream (preamble + sync + Manchester, see knxrf::buildTxChips).
  // Waits up to maxWaitMs for a free channel beforehand and then transmits anyway (lbtOverrun) – a
  // discarded telegram would be worse than a possible collision. TX_BUSY no longer occurs.
  // Then IDLE -> startRx().
  TxResult transmit(const uint8_t* chips, size_t len, uint16_t maxWaitMs = 30);

  void strobe(uint8_t s);
  void writeReg(uint8_t addr, uint8_t v);
  void writeBurst(uint8_t addr, const uint8_t* buf, size_t n);
  uint8_t readReg(uint8_t addr);
  uint8_t readStatus(uint8_t addr);

 private:
  void select();
  void deselect();
  void idle();
  uint8_t cs_, gdo0_;
  int8_t gdo2_;
  NoiseFloor noise_;
  bool lbtOverrun_ = false;
  SPISettings spi_{4000000, MSBFIRST, SPI_MODE0};
};

#pragma once
// Minimaler CC1101-Treiber fuer KNX RF (Empfang und Senden).
#include <Arduino.h>
#include <SPI.h>

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
  // Status-Register (Lesen mit Burst-Bit)
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

  CC1101(uint8_t cs, uint8_t gdo0, int8_t gdo2) : cs_(cs), gdo0_(gdo0), gdo2_(gdo2) {}  // gdo2 -1: ohne

  bool begin(int8_t freqOffset);          // Reset + KNX-RF-Konfiguration
  void startRx();                         // IDLE, FIFO leeren, RX
  bool syncDetected() { return digitalRead(gdo0_); }
  uint8_t rxBytes();                      // Anzahl Bytes im RX-FIFO (Errata-sicher)
  bool rxOverflow();
  void readFifo(uint8_t* buf, uint8_t n);
  float rssiDbm();
  int8_t freqEst() { return (int8_t)readStatus(FREQEST); }  // Schritte wie FSCTRL0 (~1,59 kHz)
  float freqOffsetKhz() { return freqEst() * 26000.0f / 16384.0f; }
  void setFreqOffset(int8_t freqOffset);  // FSCTRL0 im Betrieb aendern (IDLE, kalibrieren, RX)
  uint8_t version() { return readStatus(VERSION); }
  uint8_t marcState() { return readStatus(MARCSTATE) & 0x1F; }
  bool carrierSense();                    // Kanal belegt (GDO2 bzw. PKTSTATUS.CS, Bit 6)
  void dumpRegisters(Print& out);

  enum TxResult : uint8_t { TX_OK, TX_BUSY, TX_UNDERFLOW, TX_TIMEOUT, TX_TOO_LONG };
  // Sendet einen fertigen Chip-Strom (Praeambel + Sync + Manchester, siehe knxrf::buildTxChips).
  // Wartet vorher bis maxWaitMs auf freien Kanal (Carrier Sense an GDO2, ohne GDO2 aus PKTSTATUS).
  // Danach IDLE -> startRx().
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
  SPISettings spi_{4000000, MSBFIRST, SPI_MODE0};
};

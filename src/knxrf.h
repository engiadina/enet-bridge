#pragma once
// KNX RF 1.1 frame decoding (platform-independent, testable on the host).
//
// Radio layer (= Wireless M-Bus Mode S):
//   868.3 MHz, 2-FSK, 32.768 kchip/s, Manchester (chip pair 01 = 1, 10 = 0),
//   sync word at chip level 0x7696.
// Frame (after Manchester decoding):
//   Block 1: L, C(0x44), 0xFF, RF info, 6 bytes SN/domain, CRC16
//   Blocks 2..n: up to 16 data bytes each + CRC16
//   CRC: polynomial 0x3D65, init 0, result inverted, big endian.

#include <stddef.h>
#include <stdint.h>

namespace knxrf {

constexpr size_t kMaxFrame = 128;  // decoded bytes incl. CRC

// Number of bytes on air (incl. all CRCs) for a given L field.
// Returns 0 if L is invalid.
size_t frameLenOnAir(uint8_t L);

uint16_t crc16(const uint8_t* data, size_t len);

// Decodes Manchester chips (2 chip bytes per byte) into data bytes.
// Returns the number of decoded bytes; stops at an invalid chip pair.
size_t manchesterDecode(const uint8_t* chips, size_t chipBytes, uint8_t* out, bool invert);

// Encodes data bytes into Manchester chips (for tests and later for transmitting).
size_t manchesterEncode(const uint8_t* data, size_t len, uint8_t* chips);

struct Frame {
  uint8_t data[kMaxFrame];  // without CRCs (like rtl_433 "data")
  size_t len = 0;
  bool crcOk = false;
  int badBlock = -1;        // first block with CRC error
};

// Checks the CRCs of all blocks and removes them. raw = bytes on air.
bool stripCrc(const uint8_t* raw, size_t rawLen, Frame& out);

// Decode a telegram from chips with correction: invalid chip pairs (00/11) are erasures at known positions.
// Up to kMaxErasures of them are tried (2^k variants); only a variant where all block CRCs match is accepted.
// Returns: number of corrected positions (>= 0), -1 = not decodable, -2 = CRC wrong.
constexpr int kMaxErasures = 3;
int decodeFrame(const uint8_t* chips, size_t chipBytes, bool invert, uint8_t* raw, Frame& out);

// Adds CRCs (for tests and later for transmitting).
size_t addCrc(const uint8_t* data, size_t len, uint8_t* raw);

// ---------------------------------------------------------------------------
// Transmit: complete chip stream for the CC1101 (without its preamble/sync).
//   Preamble n x (01), sync 00 0111011010010110 (wM-Bus Mode S, 18 chips),
//   then Manchester(frame with CRCs). As bytes: preambleBytes x 0x55, 0x54,
//   0x76, 0x96 (= 4 * preambleBytes + 3 chip pairs of preamble).
// data = frame without CRCs. Returns the length in bytes, 0 if cap is too small.
// ---------------------------------------------------------------------------
constexpr uint8_t kTxPreambleBytes = 9;  // 39 chip pairs (Mode S requires >= 15)
size_t buildTxChips(const uint8_t* data, size_t len, uint8_t* chips, size_t cap, bool invert,
                    uint8_t preambleBytes = kTxPreambleBytes);

// ---------------------------------------------------------------------------
// Telegram (frame without CRCs):
//   [0] L  [1] C=0x44  [2] 0xFF  [3] RF info  [4..9] SN/domain
//   [10] Ctrl  [11,12] Src  [13,14] Dst  [15] L/NPCI  [16] TPCI|APCI-hi  [17] APCI-lo  [18..] data
// L/NPCI: bit7 group, bits6-4 hops, bits3-1 LFN, bit0 domain (1) / SN (0).
// ---------------------------------------------------------------------------
constexpr size_t kHeaderLen = 18;
constexpr size_t kMaxData = 64 - kHeaderLen;

constexpr uint16_t kServerIA = 0x7FFF;  // 7.15.255

enum Apci : uint16_t {
  APCI_GROUP_READ = 0x000,
  APCI_GROUP_RESPONSE = 0x040,
  APCI_GROUP_WRITE = 0x080,
  APCI_FUNC_PROP_CMD = 0x2C7,
  APCI_FUNC_PROP_STATE_READ = 0x2C8,
  APCI_FUNC_PROP_STATE_RESP = 0x2C9,
  APCI_PROP_VALUE_READ = 0x3D5,
  APCI_PROP_VALUE_RESP = 0x3D6,
  APCI_PROP_VALUE_WRITE = 0x3D7,
  APCI_NET_PARAM_WRITE = 0x3E4,
};

struct Telegram {
  uint8_t rfInfo = 0x02;
  uint8_t addr[6] = {};  // serial number of the sender or domain address
  bool doa = false;      // addr is a domain
  bool group = false;    // dst is a group address
  uint8_t hops = 6;
  uint8_t lfn = 0;
  uint8_t ctrl = 0x00;
  uint16_t src = 0, dst = 0;
  uint8_t tpci = 0;      // upper 6 bits of byte 16
  uint16_t apci = 0;     // 10 bits, for group APCIs incl. short value in bits 5-0
  uint8_t data[kMaxData];
  size_t dataLen = 0;

  // APCI without short value (group APCIs), otherwise unchanged
  uint16_t apciBase() const {
    uint16_t b = apci & 0x3C0;
    return (b == APCI_GROUP_READ || b == APCI_GROUP_RESPONSE || b == APCI_GROUP_WRITE) ? b : apci;
  }
};

bool parseTelegram(const uint8_t* d, size_t len, Telegram& t);
// Returns the frame length (without CRCs), 0 if there is too much data.
size_t buildTelegram(const Telegram& t, uint8_t* out);

// --- Server telegrams (sender 7.15.255) -----------------------------------
// FuncProp in general (apci = APCI_FUNC_PROP_CMD or _STATE_READ): object, PID, data. Link table of a
// channel = channel object PID 63: read `00 00 GO idx`, create entry `00 00 SN GA GO`, delete `02 00 SN GA GO`.
size_t buildFuncProp(const uint8_t domain[6], uint16_t dstIA, uint16_t apci, uint8_t obj, uint8_t pid,
                     const uint8_t* data, size_t len, uint8_t lfn, uint8_t* out);
// Group telegram under server SN, e.g. scene recall: GA of the scene, 1 byte no. (bit 7 = learn current state)
size_t buildServerGroupWrite(const uint8_t serverSN[6], uint16_t ga, const uint8_t* data, size_t len, uint8_t lfn,
                             uint8_t* out);
// Switch an actuator: obj 0, PID 62, GO handle, value (DPT-encoded).
size_t buildFuncPropCmd(const uint8_t domain[6], uint16_t dstIA, uint16_t handle, const uint8_t* value,
                        size_t valueLen, uint8_t lfn, uint8_t* out);
// Query status: obj 0, PID 62, GO handle.
size_t buildFuncPropStateRead(const uint8_t domain[6], uint16_t dstIA, uint16_t handle, uint8_t lfn,
                              uint8_t* out);
// Read/write a property (A_PropertyValue_Read/Write, P2P under domain): object, PID, count (1..15) and
// start element (12 bits). Parameter memory = channel object PID 0x4F, element = byte.
size_t buildPropRead(const uint8_t domain[6], uint16_t dstIA, uint8_t obj, uint8_t pid, uint16_t start,
                     uint8_t count, uint8_t lfn, uint8_t* out);
size_t buildPropWrite(const uint8_t domain[6], uint16_t dstIA, uint8_t obj, uint8_t pid, uint16_t start,
                      uint8_t count, const uint8_t* data, size_t len, uint8_t lfn, uint8_t* out);
// A_NetworkParameter_Write P2P under domain, raw data (object type, PID, value), e.g. 00 12 3c 00 after parameters.
size_t buildNetParamWriteP2P(const uint8_t domain[6], uint16_t dstIA, const uint8_t* data, size_t len,
                             uint8_t lfn, uint8_t* out);
// Response to a SumstatusGet of a push-button (under server SN, to the response GA).
size_t buildSumstatusInfo(const uint8_t serverSN[6], uint16_t ga, uint8_t status, uint16_t seq, uint8_t lfn,
                          uint8_t* out);
// Broadcast every ~5 s to GA 0/0/0: object 50001 PID 72 = 04 05 00 00 00 (meaning unknown).
size_t buildServerBroadcast(const uint8_t domain[6], uint8_t lfn, uint8_t* out);

inline uint8_t sumstatus(bool on, uint8_t percent) {
  return (uint8_t)((on ? 0x80 : 0x00) | (percent > 100 ? 100 : percent));
}

// --- Push-buttons ------------------------------------------------------------
// SumstatusGet (GroupWrite, 14 bytes): req, response SN, response GA, cmd, seq, CC.
struct SumstatusGet {
  uint8_t req;
  uint8_t replySN[6];
  uint16_t replyGA;
  uint16_t cmd;  // key value (GBR): 0x40xx up/on, 0x80xx down/off, lower 14 bits hold duration
  uint16_t seq;
  uint8_t cc;    // 0x08 server/app key, 0xF0 directly linked
};
bool parseSumstatusGet(const Telegram& t, SumstatusGet& s);

// FunctionProperty response (0x2C9): obj, PID, RC, value.
struct FuncPropResp {
  uint8_t obj, pid, rc;
  const uint8_t* value;
  size_t valueLen;
};
bool parseFuncPropResp(const Telegram& t, FuncPropResp& r);

// A_PropertyValue_Response: count 0 = error (access denied, invalid)
struct PropResp {
  uint8_t obj, pid, count;
  uint16_t start;
  const uint8_t* value;
  size_t valueLen;
};
bool parsePropResp(const Telegram& t, PropResp& r);

}  // namespace knxrf

#include "knxrf.h"

#include <string.h>

namespace knxrf {

size_t frameLenOnAir(uint8_t L) {
  if (L < 10 || L > 100) return 0;
  size_t rest = L - 9;                    // Datenbytes nach Block 1
  size_t blocks = (rest + 15) / 16;
  return 12 + rest + 2 * blocks;          // Block 1 (10+2) + Rest + CRCs
}

uint16_t crc16(const uint8_t* data, size_t len) {
  uint16_t r = 0;
  for (size_t i = 0; i < len; ++i) {
    r ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; ++b) r = (r & 0x8000) ? (uint16_t)((r << 1) ^ 0x3D65) : (uint16_t)(r << 1);
  }
  return (uint16_t)~r;
}

size_t manchesterDecode(const uint8_t* chips, size_t chipBytes, uint8_t* out, bool invert) {
  size_t n = 0;
  for (size_t i = 0; i + 1 < chipBytes; i += 2) {
    uint16_t w = ((uint16_t)chips[i] << 8) | chips[i + 1];
    if (invert) w = (uint16_t)~w;
    uint8_t v = 0;
    for (int k = 7; k >= 0; --k) {
      uint8_t pair = (w >> (2 * k)) & 0x3;
      if (pair == 0x1) v = (uint8_t)((v << 1) | 1);
      else if (pair == 0x2) v = (uint8_t)(v << 1);
      else return n;  // ungueltiges Chip-Paar -> Ende
    }
    out[n++] = v;
  }
  return n;
}

int decodeFrame(const uint8_t* chips, size_t chipBytes, bool invert, uint8_t* raw, Frame& out) {
  const size_t rawLen = chipBytes / 2;
  uint16_t erasures[kMaxErasures];
  int k = 0;
  for (size_t i = 0; i < rawLen; ++i) {
    uint16_t w = ((uint16_t)chips[2 * i] << 8) | chips[2 * i + 1];
    if (invert) w = (uint16_t)~w;
    uint8_t v = 0;
    for (int b = 7; b >= 0; --b) {
      uint8_t pair = (w >> (2 * b)) & 0x3;
      v = (uint8_t)(v << 1);
      if (pair == 0x1) {
        v |= 1;
      } else if (pair != 0x2) {
        if (k == kMaxErasures) return -1;
        erasures[k++] = (uint16_t)(i * 8 + (7 - b));
      }
    }
    raw[i] = v;
  }
  for (uint32_t m = 0; m < (1u << k); ++m) {
    for (int j = 0; j < k; ++j) {
      uint8_t& byte = raw[erasures[j] / 8];
      const uint8_t bit = (uint8_t)(0x80 >> (erasures[j] % 8));
      byte = (m >> j) & 1 ? (uint8_t)(byte | bit) : (uint8_t)(byte & ~bit);
    }
    if (!stripCrc(raw, rawLen, out)) return -1;
    if (out.crcOk) return k;
  }
  return k ? -1 : -2;
}

size_t manchesterEncode(const uint8_t* data, size_t len, uint8_t* chips) {
  for (size_t i = 0; i < len; ++i) {
    uint16_t w = 0;
    for (int k = 7; k >= 0; --k) w = (uint16_t)((w << 2) | (((data[i] >> k) & 1) ? 0x1 : 0x2));
    chips[2 * i] = (uint8_t)(w >> 8);
    chips[2 * i + 1] = (uint8_t)w;
  }
  return 2 * len;
}

bool stripCrc(const uint8_t* raw, size_t rawLen, Frame& out) {
  out.len = 0;
  out.crcOk = false;
  out.badBlock = -1;
  if (rawLen < 12) return false;
  size_t need = frameLenOnAir(raw[0]);
  if (!need || rawLen < need) return false;

  size_t pos = 0;
  int block = 0;
  size_t blockLen = 10;
  size_t remaining = (size_t)raw[0] + 1;  // L zaehlt sich selbst nicht mit
  bool ok = true;
  while (remaining > 0) {
    size_t take = remaining < blockLen ? remaining : blockLen;
    uint16_t want = crc16(raw + pos, take);
    uint16_t got = ((uint16_t)raw[pos + take] << 8) | raw[pos + take + 1];
    if (want != got && ok) {
      ok = false;
      out.badBlock = block;
    }
    if (out.len + take > kMaxFrame) return false;
    memcpy(out.data + out.len, raw + pos, take);
    out.len += take;
    pos += take + 2;
    remaining -= take;
    blockLen = 16;
    ++block;
  }
  out.crcOk = ok;
  return true;
}

size_t addCrc(const uint8_t* data, size_t len, uint8_t* raw) {
  size_t in = 0, o = 0, blockLen = 10;
  while (in < len) {
    size_t take = (len - in) < blockLen ? (len - in) : blockLen;
    memcpy(raw + o, data + in, take);
    uint16_t c = crc16(data + in, take);
    raw[o + take] = (uint8_t)(c >> 8);
    raw[o + take + 1] = (uint8_t)c;
    o += take + 2;
    in += take;
    blockLen = 16;
  }
  return o;
}

size_t buildTxChips(const uint8_t* data, size_t len, uint8_t* chips, size_t cap, bool invert,
                    uint8_t preambleBytes) {
  uint8_t raw[kMaxFrame + 32];
  if (len < 10 || len > kMaxFrame || (size_t)data[0] + 1 != len) return 0;
  size_t rawLen = addCrc(data, len, raw);
  size_t total = (size_t)preambleBytes + 3 + 2 * rawLen;
  if (total > cap) return 0;
  size_t o = 0;
  while (o < preambleBytes) chips[o++] = 0x55;
  chips[o++] = 0x54;  // 010101 + "00" vor dem Sync
  chips[o++] = 0x76;
  chips[o++] = 0x96;
  manchesterEncode(raw, rawLen, chips + o);
  if (invert)
    for (size_t i = o; i < total; ++i) chips[i] = (uint8_t)~chips[i];
  return total;
}

bool parseTelegram(const uint8_t* d, size_t len, Telegram& t) {
  if (len < kHeaderLen || d[1] != 0x44 || d[2] != 0xFF || (size_t)d[0] + 1 != len) return false;
  if (len - kHeaderLen > kMaxData) return false;
  t.rfInfo = d[3];
  memcpy(t.addr, d + 4, 6);
  t.ctrl = d[10];
  t.src = (uint16_t)(d[11] << 8 | d[12]);
  t.dst = (uint16_t)(d[13] << 8 | d[14]);
  t.group = d[15] & 0x80;
  t.hops = (d[15] >> 4) & 7;
  t.lfn = (d[15] >> 1) & 7;
  t.doa = d[15] & 1;
  t.tpci = d[16] & 0xFC;
  t.apci = (uint16_t)((d[16] & 3) << 8 | d[17]);
  t.dataLen = len - kHeaderLen;
  memcpy(t.data, d + kHeaderLen, t.dataLen);
  return true;
}

size_t buildTelegram(const Telegram& t, uint8_t* out) {
  if (t.dataLen > kMaxData) return 0;
  size_t len = kHeaderLen + t.dataLen;
  out[0] = (uint8_t)(len - 1);
  out[1] = 0x44;
  out[2] = 0xFF;
  out[3] = t.rfInfo;
  memcpy(out + 4, t.addr, 6);
  out[10] = t.ctrl;
  out[11] = (uint8_t)(t.src >> 8);
  out[12] = (uint8_t)t.src;
  out[13] = (uint8_t)(t.dst >> 8);
  out[14] = (uint8_t)t.dst;
  out[15] = (uint8_t)((t.group ? 0x80 : 0) | (t.hops & 7) << 4 | (t.lfn & 7) << 1 | (t.doa ? 1 : 0));
  out[16] = (uint8_t)((t.tpci & 0xFC) | ((t.apci >> 8) & 3));
  out[17] = (uint8_t)t.apci;
  memcpy(out + kHeaderLen, t.data, t.dataLen);
  return len;
}

static Telegram serverTelegram(const uint8_t addr[6], bool doa, bool group, uint16_t dst, uint16_t apci,
                               uint8_t lfn) {
  Telegram t;
  memcpy(t.addr, addr, 6);
  t.doa = doa;
  t.group = group;
  t.src = kServerIA;
  t.dst = dst;
  t.apci = apci;
  t.lfn = lfn;
  return t;
}

size_t buildFuncProp(const uint8_t domain[6], uint16_t dstIA, uint16_t apci, uint8_t obj, uint8_t pid,
                     const uint8_t* data, size_t len, uint8_t lfn, uint8_t* out) {
  if (len > kMaxData - 2) return 0;
  Telegram t = serverTelegram(domain, true, false, dstIA, apci, lfn);
  t.data[0] = obj;
  t.data[1] = pid;
  if (len) memcpy(t.data + 2, data, len);
  t.dataLen = 2 + len;
  return buildTelegram(t, out);
}

size_t buildFuncPropCmd(const uint8_t domain[6], uint16_t dstIA, uint16_t handle, const uint8_t* value,
                        size_t valueLen, uint8_t lfn, uint8_t* out) {
  if (valueLen > kMaxData - 4) return 0;
  uint8_t d[kMaxData];
  d[0] = (uint8_t)(handle >> 8);
  d[1] = (uint8_t)handle;
  memcpy(d + 2, value, valueLen);
  return buildFuncProp(domain, dstIA, APCI_FUNC_PROP_CMD, 0, 62, d, 2 + valueLen, lfn, out);
}

size_t buildFuncPropStateRead(const uint8_t domain[6], uint16_t dstIA, uint16_t handle, uint8_t lfn,
                              uint8_t* out) {
  const uint8_t d[2] = {(uint8_t)(handle >> 8), (uint8_t)handle};
  return buildFuncProp(domain, dstIA, APCI_FUNC_PROP_STATE_READ, 0, 62, d, 2, lfn, out);
}

size_t buildServerGroupWrite(const uint8_t serverSN[6], uint16_t ga, const uint8_t* data, size_t len, uint8_t lfn,
                             uint8_t* out) {
  if (len == 0 || len > kMaxData) return 0;
  Telegram t = serverTelegram(serverSN, false, true, ga, APCI_GROUP_WRITE, lfn);
  memcpy(t.data, data, len);
  t.dataLen = len;
  return buildTelegram(t, out);
}

static void propHeader(Telegram& t, uint8_t obj, uint8_t pid, uint16_t start, uint8_t count) {
  t.data[0] = obj;
  t.data[1] = pid;
  t.data[2] = (uint8_t)((count & 0x0F) << 4 | ((start >> 8) & 0x0F));
  t.data[3] = (uint8_t)start;
  t.dataLen = 4;
}

size_t buildPropRead(const uint8_t domain[6], uint16_t dstIA, uint8_t obj, uint8_t pid, uint16_t start,
                     uint8_t count, uint8_t lfn, uint8_t* out) {
  Telegram t = serverTelegram(domain, true, false, dstIA, APCI_PROP_VALUE_READ, lfn);
  propHeader(t, obj, pid, start, count);
  return buildTelegram(t, out);
}

size_t buildPropWrite(const uint8_t domain[6], uint16_t dstIA, uint8_t obj, uint8_t pid, uint16_t start,
                      uint8_t count, const uint8_t* data, size_t len, uint8_t lfn, uint8_t* out) {
  if (len > kMaxData - 4) return 0;
  Telegram t = serverTelegram(domain, true, false, dstIA, APCI_PROP_VALUE_WRITE, lfn);
  propHeader(t, obj, pid, start, count);
  memcpy(t.data + 4, data, len);
  t.dataLen = 4 + len;
  return buildTelegram(t, out);
}

size_t buildNetParamWriteP2P(const uint8_t domain[6], uint16_t dstIA, const uint8_t* data, size_t len,
                             uint8_t lfn, uint8_t* out) {
  if (len > kMaxData) return 0;
  Telegram t = serverTelegram(domain, true, false, dstIA, APCI_NET_PARAM_WRITE, lfn);
  memcpy(t.data, data, len);
  t.dataLen = len;
  return buildTelegram(t, out);
}

size_t buildSumstatusInfo(const uint8_t serverSN[6], uint16_t ga, uint8_t status, uint16_t seq, uint8_t lfn,
                          uint8_t* out) {
  Telegram t = serverTelegram(serverSN, false, true, ga, APCI_GROUP_WRITE, lfn);
  t.data[0] = status;
  t.data[1] = (uint8_t)(seq >> 8);
  t.data[2] = (uint8_t)seq;
  t.dataLen = 3;
  return buildTelegram(t, out);
}

size_t buildServerBroadcast(const uint8_t domain[6], uint8_t lfn, uint8_t* out) {
  static const uint8_t kPayload[] = {0xC3, 0x51, 72, 0x04, 0x05, 0x00, 0x00, 0x00};  // 50001, PID 72
  Telegram t = serverTelegram(domain, true, true, 0x0000, APCI_NET_PARAM_WRITE, lfn);
  memcpy(t.data, kPayload, sizeof(kPayload));
  t.dataLen = sizeof(kPayload);
  return buildTelegram(t, out);
}

bool parseSumstatusGet(const Telegram& t, SumstatusGet& s) {
  if (!t.group || t.apciBase() != APCI_GROUP_WRITE || t.dataLen != 14) return false;
  const uint8_t* d = t.data;
  s.req = d[0];
  memcpy(s.replySN, d + 1, 6);
  s.replyGA = (uint16_t)(d[7] << 8 | d[8]);
  s.cmd = (uint16_t)(d[9] << 8 | d[10]);
  s.seq = (uint16_t)(d[11] << 8 | d[12]);
  s.cc = d[13];
  return true;
}

bool parseFuncPropResp(const Telegram& t, FuncPropResp& r) {
  if (t.apci != APCI_FUNC_PROP_STATE_RESP || t.dataLen < 3) return false;
  r.obj = t.data[0];
  r.pid = t.data[1];
  r.rc = t.data[2];
  r.value = t.data + 3;
  r.valueLen = t.dataLen - 3;
  return true;
}

bool parsePropResp(const Telegram& t, PropResp& r) {
  if (t.apci != APCI_PROP_VALUE_RESP || t.dataLen < 4) return false;
  r.obj = t.data[0];
  r.pid = t.data[1];
  r.count = t.data[2] >> 4;
  r.start = (uint16_t)((t.data[2] & 0x0F) << 8 | t.data[3]);
  r.value = t.data + 4;
  r.valueLen = t.dataLen - 4;
  return true;
}

}  // namespace knxrf

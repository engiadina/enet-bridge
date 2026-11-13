// eNet / KNX-RF Bridge – firmware 0.8, protocol 1 (docs/SCHNITTSTELLE.md)
//
// Host interface: one JSON line per message.
//   Command  {"cmd":"switch","id":5,"ia":"0.0.4","ch":1,"on":true}
//   Response {"re":5,"ok":true}            immediately, only accepted/rejected
//   Event    {"ev":"result","id":5,...}   radio results, keys, states, ...
// Transmission happens only on command, for status polling, in reply to server keys (only with "server on")
// and for local scenes.
//
// Text commands for debugging (line does not start with "{"):
//   v              toggle reporting of receive errors (rx_error)
//   r              dump CC1101 registers
//   s              statistics
//   d              diagnostics: GDO lines, state, RSSI/carrier sense over 6 s
//   w <reg> <val>  write CC1101 register (hex), for experiments only
//   send <hex>     arbitrary telegram (without CRCs), the bridge sets the LFN
//   spi            line test without SPI: CSn by hand, MISO with pull-up/-down (CS low = 1/1: chip not
//                  ready, crystal not running; CS high = 1/0: high impedance, correct)

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <SPI.h>

#include "cc1101.h"
#include "config.h"
#include "knxrf.h"
#include "server.h"

static const char* kFwVersion = "0.8.26";
static const int kProtoVersion = 1;

static CC1101 radio(PIN_CS, PIN_GDO0, PIN_GDO2);
static bool radioOk = false;
static uint8_t radioVersion = 0;               // chip version at startup (monitoring compares against it)
static int8_t freqOffset = CC1101_FREQ_OFFSET;  // FSCTRL0; per board in NVS ("foff"), command "radio"
static int32_t estSum = 0;                      // FREQEST of valid telegrams since the last offset change
static uint16_t estN = 0;
static bool verbose = false;  // report receive errors
static bool monitor = false;  // report all telegrams
static uint32_t statOk = 0, statCrc = 0, statBad = 0, statFixed = 0, statOverflow = 0, statTx = 0, statTxBusy = 0;
static uint8_t lfn = 0;       // link frame number of the bridge (3 bits, one counter for everything)
static float lastRssi = 0;    // level of the most recently received telegram
static Preferences prefs;

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------
static void hexStr(const uint8_t* d, size_t n, char* out) {
  static const char* hx = "0123456789abcdef";
  for (size_t i = 0; i < n; ++i) {
    out[2 * i] = hx[d[i] >> 4];
    out[2 * i + 1] = hx[d[i] & 0xF];
  }
  out[2 * n] = 0;
}

static size_t parseHex(const char* s, uint8_t* out, size_t cap) {
  size_t n = 0;
  auto nib = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    c |= 0x20;
    return (c >= 'a' && c <= 'f') ? c - 'a' + 10 : -1;
  };
  while (s && s[0] && s[1] && n < cap) {
    int hi = nib(s[0]), lo = nib(s[1]);
    if (hi < 0 || lo < 0) return 0;
    out[n++] = (uint8_t)(hi << 4 | lo);
    s += 2;
  }
  return (s && *s) ? 0 : n;  // remainder (odd/too long) = error
}

// "1.2.3" -> 0x1203
static bool parseIA(const char* s, uint16_t& ia) {
  unsigned a, b, c;
  char end;
  if (!s || sscanf(s, "%u.%u.%u%c", &a, &b, &c, &end) != 3 || a > 15 || b > 15 || c > 255) return false;
  ia = (uint16_t)(a << 12 | b << 8 | c);
  return true;
}

static void iaStr(uint16_t ia, char* out) { sprintf(out, "%u.%u.%u", ia >> 12, (ia >> 8) & 15, ia & 255); }

static void emit(JsonDocument& d) {
  serializeJson(d, Serial);
  Serial.write('\n');
}

static void replyOk(uint32_t id) {
  if (!id) return;
  JsonDocument d;
  d["re"] = id;
  d["ok"] = true;
  emit(d);
}

static void replyErr(uint32_t id, const char* err, const char* msg) {
  JsonDocument d;
  if (id) d["re"] = id;
  else d["ev"] = "error";
  if (id) d["ok"] = false;
  d["err"] = err;
  d["msg"] = msg;
  emit(d);
}

static uint8_t nextLfn() {
  lfn = (lfn + 1) & 7;
  return lfn;
}

// ---------------------------------------------------------------------------
// Clock (set by the host, for timestamps when programming)
// ---------------------------------------------------------------------------
static bool clockSet = false;
static int64_t clockBase = 0;  // seconds since 2000-01-01 at time clockMs
static uint32_t clockMs = 0;

static int64_t daysFromCivil(int y, unsigned m, unsigned d) {  // H. Hinnant, days since 1970-01-01
  y -= m <= 2;
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = (unsigned)(y - era * 400);
  const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return (int64_t)era * 146097 + (int64_t)doe - 719468;
}

static bool setClock(const char* iso) {
  int y, mo, d, h, mi, s;
  if (!iso || sscanf(iso, "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &s) != 6 || y < 2000) return false;
  clockBase = (daysFromCivil(y, mo, d) - daysFromCivil(2000, 1, 1)) * 86400 + h * 3600 + mi * 60 + s;
  clockMs = millis();
  clockSet = true;
  return true;
}

// ---------------------------------------------------------------------------
// Transmit
// Radio LEDs (XIAO PIN_LED active low, CUL32 PIN_LED2): briefly on for every transmitted and every valid received
// telegram. Both are driven on every C6 board – the pin of the other board is unused there.
static uint32_t ledOffAt = 0;
static bool ledOn = false;
static void ledSet(bool on) {
  if (PIN_LED >= 0) digitalWrite(PIN_LED, on ? LOW : HIGH);
  if (PIN_LED2 >= 0) digitalWrite(PIN_LED2, on ? LED2_ON : !LED2_ON);
}
static void ledFlash() {
  if (PIN_LED < 0 && PIN_LED2 < 0) return;
  ledSet(true);
  ledOn = true;
  ledOffAt = millis() + 40;
}
static void ledCheck(uint32_t now) {
  if (ledOn && (int32_t)(now - ledOffAt) >= 0) {
    ledSet(false);
    ledOn = false;
  }
}

// ---------------------------------------------------------------------------
// false = not sent (channel busy, error)
static bool sendTelegram(const uint8_t* data, size_t len) {
  static const char* kResult[] = {"ok", "busy", "underflow", "timeout", "too_long"};
  static uint8_t chips[255];
  if (!radioOk) return false;
  size_t n = knxrf::buildTxChips(data, len, chips, sizeof(chips), KNXRF_INVERT_CHIPS);
  if (!n) return false;
  ledFlash();
  CC1101::TxResult r = radio.transmit(chips, n);
  if (r == CC1101::TX_OK) ++statTx;
  if (radio.lbtOverrun()) ++statTxBusy;  // sent after the wait time despite a busy channel
  if (!monitor) return r == CC1101::TX_OK;
  char hx[2 * 64 + 1];
  hexStr(data, len < 64 ? len : 64, hx);
  JsonDocument d;
  d["ev"] = "telegram";
  d["dir"] = "tx";
  d["data"] = hx;
  d["result"] = kResult[r];
  d["ms"] = millis();
  emit(d);
  return r == CC1101::TX_OK;
}

// ---------------------------------------------------------------------------
// Server functions: radio link, events as JSON, configuration in flash
// ---------------------------------------------------------------------------
class BridgeIo : public enet::Io {
 public:
  enet::Server* server = nullptr;

  bool transmit(const uint8_t* frame, size_t len) override {
    uint8_t f[64];
    if (len > sizeof(f) || len < 16) return false;
    memcpy(f, frame, len);
    f[15] = (uint8_t)((f[15] & 0xF1) | nextLfn() << 1);
    return sendTelegram(f, len);
  }

  void onKey(const enet::KeyEvent& e) override {
    static const char* kKind[] = {"server", "direkt", "wert", "wert1", "wert4"};
    char sn[13], cmd[5], rsn[13];
    hexStr(e.senderSn, 6, sn);
    sprintf(cmd, "%04x", e.cmd);
    JsonDocument d;
    d["ev"] = "key";
    d["sn"] = sn;
    d["ga"] = e.ga;
    d["kind"] = kKind[(int)e.kind];
    d["cmd"] = cmd;
    d["press"] = e.press;
    if (e.kind == enet::KeyKind::SERVER) {
      d["code"] = (e.cmd >> 8) & 0x0F;
      d["seq"] = e.sg.seq;
      d["reply_ga"] = e.sg.replyGA;
      d["answered"] = e.answered;
      int scene = -1;
      if (e.sceneStarted && e.keyIndex >= 0) scene = server->config().keys[e.keyIndex].scene;
      d["scene"] = scene;
      d["key"] = e.keyIndex;
    } else if (e.kind == enet::KeyKind::VALUE1) {
      sprintf(cmd, "%02x", e.cmd);
      d["cmd"] = cmd;
    } else if (e.kind == enet::KeyKind::VALUE4) {
      char c8[9];
      sprintf(c8, "%08lx", (unsigned long)e.value32);
      d["cmd"] = c8;
    } else {
      d["richtung"] = (e.cmd & 0xC000) == 0x8000 ? "rechts" : "links";  // rocker keys are next to each other
      d["hold_ms"] = (uint32_t)(e.cmd & 0x3FFF) * 10;
      if (e.kind == enet::KeyKind::DIRECT) {
        hexStr(e.sg.replySN, 6, rsn);
        d["seq"] = e.sg.seq;
        d["reply_sn"] = rsn;
        d["reply_ga"] = e.sg.replyGA;
      }
    }
    // Battery state from the RF info byte (KNX RF: bit 1 = 1 battery OK, 0 weak); the eNet Server evaluates
    // it the same way (kdrive DeviceMonitor, BATTERY_OK/BATTERY_WEAK)
    d["battery"] = (e.rfInfo & 0x02) ? "ok" : "weak";
    d["rssi"] = lastRssi;
    emit(d);
  }

  void onState(const enet::Channel& ch, const enet::ChannelState& st, enet::Source src) override {
    static const char* kSrc[] = {"status", "poll", "cache"};
    char ia[12];
    iaStr(ch.ia, ia);
    JsonDocument d;
    d["ev"] = "state";
    d["ia"] = ia;
    d["ch"] = ch.ch;
    d["on"] = st.on;
    d["percent"] = st.on && !ch.dimmer && !ch.blind ? 100 : st.percent;
    if (ch.blind) {
      d["kind"] = "blind";
      d["slat"] = st.slat;
      if (st.move != 0xFF) d["move"] = st.move;
    }
    d["source"] = kSrc[(int)src];
    if (src != enet::Source::CACHE) d["rssi"] = lastRssi;  // level of the status message or response
    emit(d);
  }

  void onResult(uint32_t id, uint16_t ia, uint16_t handle, bool isRead, enet::Origin, bool ok, int rc,
                uint8_t tries, const uint8_t* val, size_t len) override {
    if (!id) return;  // internal jobs (status polling) only report state
    char iab[12], hx[2 * 8 + 1];
    iaStr(ia, iab);
    JsonDocument d;
    d["ev"] = "result";
    d["id"] = id;
    d["ok"] = ok;
    d["ia"] = iab;
    int ci = server->channelIndex(ia, handle);
    if (ci >= 0) d["ch"] = server->config().channels[ci].ch;
    d["handle"] = handle;
    d["type"] = isRead ? "read" : "set";
    d["tries"] = tries;
    if (ok) d["rssi"] = lastRssi;  // level of the acknowledgement
    if (ok && val && len) {
      hexStr(val, len < 8 ? len : 8, hx);
      d["value"] = hx;
    }
    if (!ok) {
      d["err"] = rc < 0 ? "timeout" : "nack";
      if (rc >= 0) d["rc"] = rc;
    }
    emit(d);
  }

  void onPropResult(uint32_t id, uint16_t ia, enet::JobKind kind, uint8_t obj, uint8_t pid, uint16_t start,
                    bool ok, int rc, uint8_t tries, const uint8_t* val, size_t len) override {
    if (!id) return;
    char iab[12], hx[2 * enet::kMaxJobData + 1];
    iaStr(ia, iab);
    JsonDocument d;
    d["ev"] = "result";
    d["id"] = id;
    d["ok"] = ok;
    d["ia"] = iab;
    static const char* const kTypes[] = {"set",      "read",   "prop_read", "prop_write",
                                         "np_write", "fp_cmd", "fp_read",   "scene_call"};
    d["type"] = kTypes[(int)kind];
    if (kind == enet::JobKind::GROUP_WRITE) {
      d["ga"] = start;
    } else if (kind != enet::JobKind::NP_WRITE) {
      d["obj"] = obj;
      d["pid"] = pid;
      if (kind == enet::JobKind::PROP_READ || kind == enet::JobKind::PROP_WRITE) d["start"] = start;
      if (ok) d["rssi"] = lastRssi;
    }
    d["tries"] = tries;
    if (ok && val && len) {
      hexStr(val, len < enet::kMaxJobData ? len : enet::kMaxJobData, hx);
      d["value"] = hx;
    }
    if (!ok) {
      d["err"] = rc < 0 ? "timeout" : "nack";
      if (rc > 0 && kind != enet::JobKind::PROP_READ && kind != enet::JobKind::PROP_WRITE) d["rc"] = rc;
    }
    emit(d);
  }

  void onSceneDone(uint32_t id, uint8_t scene, uint8_t failed) override {
    JsonDocument d;
    d["ev"] = "scene_done";
    d["nr"] = scene;
    d["failed"] = failed;
    if (id) d["id"] = id;
    emit(d);
    if (!id) return;
    JsonDocument r;
    r["ev"] = "result";
    r["id"] = id;
    r["ok"] = failed == 0;
    if (failed) r["err"] = "timeout";
    emit(r);
  }

  void onRealServer() override {
    JsonDocument d;
    d["ev"] = "real_server";
    emit(d);
  }
};

static BridgeIo bridgeIo;
static enet::Server server(bridgeIo);
static enet::Config cfgTmp;  // buffer for loading/uploading
static uint8_t cfgBlob[3072];  // format 4: 23 bytes per channel
static size_t cfgLen = 0;     // length of the blob currently being uploaded
static uint16_t cfgCrc = 0;   // CRC of the active configuration (0 = none)
static bool cfgValid = false;

static void serverOnFrame(const uint8_t* d, size_t n) {
  knxrf::Telegram t;
  if (knxrf::parseTelegram(d, n, t)) server.onFrame(t, millis());
}

static void loadServerConfig() {
  size_t n = prefs.getBytesLength("cfg");
  if (n >= 2 && n <= sizeof(cfgBlob) && prefs.getBytes("cfg", cfgBlob, n) == n &&
      enet::parseConfig(cfgBlob, n, cfgTmp)) {
    server.setConfig(cfgTmp);
    cfgCrc = (uint16_t)(cfgBlob[n - 2] << 8 | cfgBlob[n - 1]);
    cfgValid = true;
  } else {  // without configuration: domain and server SN from config.h (transmit only with a valid configuration)
    const uint8_t dom[6] = ENET_DOMAIN, sn[6] = ENET_SERVER_SN;
    memcpy(cfgTmp.domain, dom, 6);
    memcpy(cfgTmp.serverSn, sn, 6);
    server.setConfig(cfgTmp);
  }
  // on by default (new bridge = server right away); a real server is only reported (ev real_server)
  server.setEnabled(prefs.getBool("srv", true));
}

// ---------------------------------------------------------------------------
// Receive
// ---------------------------------------------------------------------------
static void rxError(const char* kind, float rssi, const uint8_t* d, size_t n) {
  if (!verbose) return;
  char hx[2 * 128 + 1];
  hexStr(d, n < 128 ? n : 128, hx);
  JsonDocument doc;
  doc["ev"] = "rx_error";
  doc["kind"] = kind;
  doc["rssi"] = rssi;
  doc["data"] = hx;
  emit(doc);
}

// Reads a telegram after a detected sync.
static void receiveFrame() {
  static uint8_t chips[2 * knxrf::kMaxFrame];
  static uint8_t raw[knxrf::kMaxFrame];
  const float rssi = radio.rssiDbm();
  const uint32_t t0 = millis();
  size_t have = 0;
  size_t needChips = 0;

  while (millis() - t0 < 60) {
    uint8_t n = radio.rxBytes();
    if (radio.rxOverflow()) {
      ++statOverflow;
      break;
    }
    if (n) {
      // leave the last byte in the FIFO while reception is still in progress (errata)
      if (n > 1 && (needChips == 0 || have + n < needChips)) --n;
      if (have + n > sizeof(chips)) n = sizeof(chips) - have;
      radio.readFifo(chips + have, n);
      have += n;
    }
    if (!needChips && have >= 2) {
      uint8_t L;
      if (knxrf::manchesterDecode(chips, 2, &L, KNXRF_INVERT_CHIPS) != 1) break;
      size_t onAir = knxrf::frameLenOnAir(L);
      if (!onAir) break;
      needChips = 2 * onAir;
    }
    if (needChips && have >= needChips) break;
    if (!n) delayMicroseconds(200);
  }
  const int8_t est = radio.freqEst();
  const float foff = est * 26000.0f / 16384.0f;
  radio.startRx();

  if (!needChips || have < needChips) {
    ++statBad;
    return rxError("invalid", rssi, chips, have);
  }
  knxrf::Frame f;
  const int fixed = knxrf::decodeFrame(chips, needChips, KNXRF_INVERT_CHIPS, raw, f);
  if (fixed == -1) {
    ++statBad;
    return rxError("manchester", rssi, chips, have);
  }
  if (fixed == -2) {
    ++statCrc;
    return rxError("crc", rssi, raw, needChips / 2);
  }
  if (fixed > 0) ++statFixed;

  ++statOk;
  ledFlash();
  lastRssi = rssi;
  if (estN >= 1000) {  // moving: halve old values
    estSum /= 2;
    estN /= 2;
  }
  estSum += est;
  ++estN;
  serverOnFrame(f.data, f.len);  // first: answer server keys without delay
  if (!monitor) return;
  char hx[2 * knxrf::kMaxFrame + 1];
  hexStr(f.data, f.len, hx);
  JsonDocument d;
  d["ev"] = "telegram";
  d["dir"] = "rx";
  d["data"] = hx;
  d["rssi"] = rssi;
  d["foff_khz"] = foff;
  d["ms"] = millis();
  emit(d);
}

// ---------------------------------------------------------------------------
// Diagnostics (text command d or {"cmd":"diag"})
// ---------------------------------------------------------------------------
static void diagnose() {
  radio.writeReg(CC1101::IOCFG0, 0x2F);  // constant 0
  radio.writeReg(CC1101::IOCFG2, 0x2F);
  delay(2);
  int g0lo = digitalRead(PIN_GDO0), g2lo = PIN_GDO2 >= 0 ? digitalRead(PIN_GDO2) : -1;
  radio.writeReg(CC1101::IOCFG0, 0x6F);  // constant 1
  radio.writeReg(CC1101::IOCFG2, 0x6F);
  delay(2);
  int g0hi = digitalRead(PIN_GDO0), g2hi = PIN_GDO2 >= 0 ? digitalRead(PIN_GDO2) : -1;
  radio.writeReg(CC1101::IOCFG0, 0x06);
  radio.writeReg(CC1101::IOCFG2, 0x0E);
  radio.startRx();
  delay(5);
  uint8_t marc = radio.marcState();
  float rssiMin = 0, rssiMax = -200;
  uint32_t cs = 0, n = 0;
  for (uint32_t t0 = millis(); millis() - t0 < 6000; ++n) {
    if (radio.syncDetected() || radio.marcState() != 0x0D) {  // telegram: AGC frozen (AGCCTRL0) → restart,
      radio.startRx();                                        // otherwise RSSI stays at its level
      delay(2);
      --n;
      continue;
    }
    float r = radio.rssiDbm();
    if (r > rssiMax) rssiMax = r;
    if (r < rssiMin) rssiMin = r;
    cs += radio.carrierSense();
    delay(2);
  }
  JsonDocument d;
  d["ev"] = "diag";
  d["gdo0"] = String(g0lo) + "/" + String(g0hi);
  d["gdo2"] = String(g2lo) + "/" + String(g2hi);
  d["marcstate"] = marc;
  d["rssi_min"] = rssiMin;
  d["rssi_max"] = rssiMax;
  d["cs_anteil"] = n ? (float)cs / n : 0.0f;
  d["grundrauschen"] = radio.noise().floorDb();
  d["schwelle"] = radio.noise().threshold();
  emit(d);
}

static void printStats(uint32_t id) {
  JsonDocument d;
  if (id) {
    d["re"] = id;
    d["ok"] = true;
  } else {
    d["ev"] = "stats";
  }
  d["rx_ok"] = statOk;
  d["crc_error"] = statCrc;
  d["corrected"] = statFixed;
  d["invalid"] = statBad;
  d["overflow"] = statOverflow;
  d["tx"] = statTx;
  d["tx_busy"] = statTxBusy;
  if (radio.noise().ready()) {
    d["noise_dbm"] = roundf(radio.noise().floorDb() * 10) / 10;
    d["busy_pct"] = roundf(radio.noise().busyShare() * 1000) / 10;
  }
  d["uptime_s"] = millis() / 1000;
  emit(d);
}

// Board identifier (MAC of the ESP32), e.g. "a0b1c2d3e4f5"
static const char* bridgeId() {
  static char id[13];
  uint64_t mac = ESP.getEfuseMac();
  for (int i = 0; i < 6; ++i) sprintf(id + 2 * i, "%02x", (uint8_t)(mac >> (8 * i)));
  return id;
}

static void fillRadio(JsonObject r) {
  r["ok"] = radioOk;
  r["version"] = radioOk ? radio.version() : radioVersion;
  r["freq_offset"] = freqOffset;
  r["calibrated"] = prefs.isKey("foff");  // offset stored for this board
  if (estN) r["freqest"] = (float)estSum / estN;  // mean deviation (steps), FSCTRL0 += freqest
  r["freqest_n"] = estN;
}

static void emitRadio() {
  JsonDocument d;
  d["ev"] = "radio";
  fillRadio(d["radio"].to<JsonObject>());
  emit(d);
}

// Check the CC1101 continuously: if it drops out (loose contact, ground lost), report radio.ok = false and
// reinitialize every 5 s. The version is read even in the middle of reception (status register).
static void checkRadio(uint32_t now) {
  static uint32_t last = 0;
  static uint8_t fails = 0;
  if (now - last < (radioOk ? 1000u : 5000u)) return;
  last = now;
  if (radioOk) {
    if (radio.version() == radioVersion) {
      fails = 0;
      return;
    }
    if (++fails < 2) return;
    fails = 0;
    radioOk = false;
    emitRadio();
    replyErr(0, "no_radio", "CC1101 antwortet nicht mehr - Verkabelung/3,3 V pruefen");
  } else if (radio.begin(freqOffset)) {
    radioVersion = radio.version();
    radioOk = true;
    radio.startRx();
    emitRadio();
  }
}

static void fillStatus(JsonDocument& d) {
  char crc[5];
  sprintf(crc, "%04x", cfgCrc);
  d["product"] = "enet-bridge";  // identifier for the host: a bridge is answering here (automatic update)
  d["fw"] = kFwVersion;
  d["proto"] = kProtoVersion;
  d["chip"] = CONFIG_IDF_TARGET;  // "esp32", "esp32c6": which firmware package the host flashes
  d["id"] = bridgeId();
  JsonObject r = d["radio"].to<JsonObject>();
  fillRadio(r);
  JsonObject s = d["server"].to<JsonObject>();
  s["on"] = server.enabled();
  JsonObject c = d["config"].to<JsonObject>();
  c["valid"] = cfgValid;
  c["crc"] = crc;
  c["channels"] = server.config().nChannels;
  c["scenes"] = server.config().nScenes;
  c["keys"] = server.config().nKeys;
  JsonObject q = d["queue"].to<JsonObject>();
  q["len"] = server.queueLength();
  q["max"] = enet::kQueueLen;
  d["clock"] = clockSet;
  d["uptime_s"] = millis() / 1000;
}

// ---------------------------------------------------------------------------
// JSON commands
// ---------------------------------------------------------------------------
static bool channelArg(JsonDocument& in, uint32_t id, uint16_t& ia, int& ci) {
  if (!parseIA(in["ia"] | (const char*)nullptr, ia) || !in["ch"].is<int>()) {
    replyErr(id, "bad_args", "ia und ch erforderlich");
    return false;
  }
  ci = server.channelIndexByCh(ia, (uint8_t)(in["ch"].as<int>()));
  if (ci < 0) {
    replyErr(id, "unknown_channel", "Kanal nicht in der Konfiguration");
    return false;
  }
  return true;
}

static bool radioReady(uint32_t id) {
  if (!radioOk) {
    replyErr(id, "no_radio", "CC1101 nicht erkannt");
    return false;
  }
  if (!cfgValid) {
    replyErr(id, "no_config", "keine gueltige Konfiguration");
    return false;
  }
  return true;
}

static void queued(uint32_t id, bool ok) {
  if (ok) replyOk(id);
  else replyErr(id, "queue_full", "Warteschlange voll");
}

static void handleJson(const char* line) {
  JsonDocument in;
  if (deserializeJson(in, line)) return replyErr(0, "bad_json", "JSON ungueltig");
  const char* cmd = in["cmd"] | "";
  uint32_t id = in["id"] | 0;
  uint16_t ia;
  int ci;
  uint8_t val[8];

  if (!strcmp(cmd, "hello")) {
    if (in["time"].is<const char*>()) setClock(in["time"]);
    JsonDocument d;
    d["re"] = id;
    d["ok"] = true;
    fillStatus(d);
    emit(d);
  } else if (!strcmp(cmd, "time")) {
    if (!setClock(in["time"] | (const char*)nullptr)) return replyErr(id, "bad_args", "time: JJJJ-MM-TTThh:mm:ss");
    replyOk(id);
  } else if (!strcmp(cmd, "stats")) {
    printStats(id);
  } else if (!strcmp(cmd, "server")) {
    if (in["on"].is<bool>()) {
      server.setEnabled(in["on"].as<bool>());
      prefs.putBool("srv", server.enabled());
    }
    JsonDocument d;
    d["re"] = id;
    d["ok"] = true;
    d["on"] = server.enabled();
    emit(d);
  } else if (!strcmp(cmd, "config")) {
    int part = in["part"] | -1;
    const char* data = in["data"] | "";
    if (part < 0) return replyErr(id, "bad_args", "part fehlt");
    if (part == 0) cfgLen = 0;
    size_t n = parseHex(data, cfgBlob + cfgLen, sizeof(cfgBlob) - cfgLen);
    if (*data && !n) return replyErr(id, "bad_args", "data: Hex ungueltig oder zu lang");
    cfgLen += n;
    replyOk(id);
    if (!(in["last"] | false)) return;
    JsonDocument d;
    d["ev"] = "config";
    if (!enet::parseConfig(cfgBlob, cfgLen, cfgTmp)) {
      d["ok"] = false;
      d["err"] = "bad_args";
      d["msg"] = "Konfiguration ungueltig (Version/CRC/Format)";
      return emit(d);
    }
    server.setConfig(cfgTmp);
    prefs.putBytes("cfg", cfgBlob, cfgLen);
    cfgCrc = (uint16_t)(cfgBlob[cfgLen - 2] << 8 | cfgBlob[cfgLen - 1]);
    cfgValid = true;
    char crc[5];
    sprintf(crc, "%04x", cfgCrc);
    d["ok"] = true;
    d["crc"] = crc;
    d["channels"] = cfgTmp.nChannels;
    d["scenes"] = cfgTmp.nScenes;
    d["keys"] = cfgTmp.nKeys;
    emit(d);
  } else if (!strcmp(cmd, "radio")) {
    // {"freq_offset"?, "save"?} – set frequency correction (effective immediately) and/or save it in NVS
    if (!in["freq_offset"].isNull()) {
      int f = in["freq_offset"] | 1000;
      if (f < -128 || f > 127) return replyErr(id, "bad_args", "freq_offset -128..127");
      if (!radioOk) return replyErr(id, "no_radio", "CC1101 nicht erkannt");
      freqOffset = (int8_t)f;
      radio.setFreqOffset(freqOffset);
      estSum = 0;
      estN = 0;
    }
    if (in["save"] | false) prefs.putChar("foff", freqOffset);
    JsonDocument d;
    d["re"] = id;
    d["ok"] = true;
    fillRadio(d["radio"].to<JsonObject>());
    emit(d);
  } else if (!strcmp(cmd, "monitor")) {
    monitor = in["on"] | false;
    replyOk(id);
  } else if (!strcmp(cmd, "switch")) {
    if (!radioReady(id) || !channelArg(in, id, ia, ci)) return;
    if (!in["on"].is<bool>()) return replyErr(id, "bad_args", "on fehlt");
    val[0] = in["on"].as<bool>() ? 1 : 0;
    const enet::Channel& ch = server.config().channels[ci];
    queued(id, server.queueSet(ia, ch.offset + enet::goOf(ch, enet::kSlotOn), val, 1, enet::Origin::HOST, id));
  } else if (!strcmp(cmd, "dim")) {
    if (!radioReady(id) || !channelArg(in, id, ia, ci)) return;
    int p = in["percent"] | -1;
    if (p < 0 || p > 100) return replyErr(id, "bad_args", "percent 0..100");
    const enet::Channel& ch = server.config().channels[ci];
    if (p == 0) {
      val[0] = 0;
      return queued(id, server.queueSet(ia, ch.offset + enet::goOf(ch, enet::kSlotOn), val, 1, enet::Origin::HOST, id));
    }
    val[0] = (uint8_t)(p * 255 / 100);  // like the server: floor
    queued(id, server.queueSet(ia, ch.offset + enet::goOf(ch, enet::kSlotValue), val, 1, enet::Origin::HOST, id));
  } else if (!strcmp(cmd, "cover")) {
    // {"ia","ch","action": up|down|stop|step_up|step_down|position|slat [, "percent" 0..100 (0 = top)]}
    if (!radioReady(id) || !channelArg(in, id, ia, ci)) return;
    const enet::Channel& ch = server.config().channels[ci];
    if (!ch.blind) return replyErr(id, "bad_args", "kein Jalousiekanal");
    const char* a = in["action"] | "";
    uint16_t go;
    if (!strcmp(a, "up") || !strcmp(a, "down")) {
      go = enet::goOf(ch, enet::kSlotMove);
      val[0] = !strcmp(a, "down") ? 1 : 0;  // DPT 1.008: 0 up, 1 down
    } else if (!strcmp(a, "step_up") || !strcmp(a, "step_down")) {
      go = enet::goOf(ch, enet::kSlotStep);
      val[0] = !strcmp(a, "step_down") ? 1 : 0;  // DPT 1.007
    } else if (!strcmp(a, "stop")) {
      go = enet::goOf(ch, enet::kSlotStop);
      val[0] = 1;  // DPT 1.017 trigger
    } else if (!strcmp(a, "position") || !strcmp(a, "slat")) {
      int p = in["percent"] | -1;
      if (p < 0 || p > 100) return replyErr(id, "bad_args", "percent 0..100");
      go = enet::goOf(ch, !strcmp(a, "slat") ? enet::kSlotSlat : enet::kSlotPos);
      val[0] = (uint8_t)((p * 255 + 50) / 100);  // DPT 5.001
    } else {
      return replyErr(id, "bad_args", "action up|down|stop|step_up|step_down|position|slat");
    }
    queued(id, server.queueSet(ia, ch.offset + go, val, 1, enet::Origin::HOST, id));
  } else if (!strcmp(cmd, "set")) {
    if (!radioReady(id)) return;
    size_t vl = parseHex(in["value"] | "", val, 4);
    if (!parseIA(in["ia"] | (const char*)nullptr, ia) || !in["handle"].is<int>() || !vl)
      return replyErr(id, "bad_args", "ia, handle, value (hex 1-4 Byte)");
    queued(id, server.queueSet(ia, (uint16_t)in["handle"].as<int>(), val, (uint8_t)vl, enet::Origin::HOST, id));
  } else if (!strcmp(cmd, "read")) {
    if (!radioReady(id)) return;
    if (!parseIA(in["ia"] | (const char*)nullptr, ia) || !in["handle"].is<int>())
      return replyErr(id, "bad_args", "ia, handle");
    queued(id, server.queueRead(ia, (uint16_t)in["handle"].as<int>(), enet::Origin::HOST, id));
  } else if (!strcmp(cmd, "prop_read") || !strcmp(cmd, "prop_write")) {
    // {"ia","obj","pid","start","count"[, "data" hex]} – A_PropertyValue_Read/Write (parameter memory: PID 79)
    if (!radioReady(id)) return;
    bool write = !strcmp(cmd, "prop_write");
    uint8_t data[enet::kMaxJobData];
    size_t dl = write ? parseHex(in["data"] | "", data, sizeof(data)) : 0;
    if (!parseIA(in["ia"] | (const char*)nullptr, ia) || !in["obj"].is<int>() || !in["pid"].is<int>() ||
        !in["start"].is<int>() || (write && !dl))
      return replyErr(id, "bad_args", write ? "ia, obj, pid, start, data" : "ia, obj, pid, start");
    int count = in["count"] | (write ? (int)dl : 1);
    queued(id, server.queueProp(write ? enet::JobKind::PROP_WRITE : enet::JobKind::PROP_READ, ia,
                                (uint8_t)in["obj"].as<int>(), (uint8_t)in["pid"].as<int>(),
                                (uint16_t)in["start"].as<int>(), (uint8_t)count, data, (uint8_t)dl, id));
  } else if (!strcmp(cmd, "np_write")) {
    // {"ia","data" hex} – A_NetworkParameter_Write P2P, without a response from the device
    if (!radioReady(id)) return;
    uint8_t data[enet::kMaxJobData];
    size_t dl = parseHex(in["data"] | "", data, sizeof(data));
    if (!parseIA(in["ia"] | (const char*)nullptr, ia) || !dl) return replyErr(id, "bad_args", "ia, data");
    queued(id, server.queueNpWrite(ia, data, (uint8_t)dl, id));
  } else if (!strcmp(cmd, "fp_cmd") || !strcmp(cmd, "fp_read")) {
    // {"ia","obj","pid","data" hex} – FuncProp on object/PID, e.g. link table (PID 63)
    if (!radioReady(id)) return;
    uint8_t data[enet::kMaxJobData];
    size_t dl = parseHex(in["data"] | "", data, sizeof(data));
    if (!parseIA(in["ia"] | (const char*)nullptr, ia) || !in["obj"].is<int>() || !in["pid"].is<int>())
      return replyErr(id, "bad_args", "ia, obj, pid, data");
    queued(id, server.queueFuncProp(!strcmp(cmd, "fp_cmd") ? enet::JobKind::FP_CMD : enet::JobKind::FP_READ, ia,
                                    (uint8_t)in["obj"].as<int>(), (uint8_t)in["pid"].as<int>(), data, (uint8_t)dl,
                                    id));
  } else if (!strcmp(cmd, "scene_call")) {
    // {"ga","nr"[,"learn"]} – recall a KNX scene in the actuators or learn the current state
    if (!radioReady(id)) return;
    int ga = in["ga"] | -1, nr = in["nr"] | -1;
    if (ga < 1 || ga > 0xFFFF || nr < 0 || nr > 63) return replyErr(id, "bad_args", "ga, nr 0..63");
    val[0] = (uint8_t)(nr | ((in["learn"] | false) ? 0x80 : 0));
    queued(id, server.queueGroupWrite((uint16_t)ga, val, 1, id));
  } else if (!strcmp(cmd, "poll")) {
    if (!radioReady(id) || !channelArg(in, id, ia, ci)) return;
    const enet::Channel& ch = server.config().channels[ci];
    bool ok = true;
    if (ch.blind) {
      ok = server.queueRead(ia, ch.offset + enet::goOf(ch, enet::kSlotSlatNow), enet::Origin::POLL, 0) &&
           server.queueRead(ia, ch.offset + enet::goOf(ch, enet::kSlotPosNow), enet::Origin::POLL, id);
      return queued(id, ok);
    }
    if (ch.dimmer) ok = server.queueRead(ia, ch.offset + enet::goOf(ch, enet::kSlotStatus), enet::Origin::POLL, 0);
    ok = ok && server.queueRead(ia, ch.offset + enet::goOf(ch, ch.dimmer ? enet::kSlotStatusValue : enet::kSlotStatus),
                                enet::Origin::POLL, id);
    queued(id, ok);
  } else if (!strcmp(cmd, "state")) {
    const enet::Config& c = server.config();
    bool filterIa = parseIA(in["ia"] | (const char*)nullptr, ia);
    int ch = in["ch"] | -1;
    int count = 0;
    for (uint8_t i = 0; i < c.nChannels; ++i) {
      if (filterIa && c.channels[i].ia != ia) continue;
      if (ch >= 0 && c.channels[i].ch != ch) continue;
      if (!server.state(i).known) continue;
      bridgeIo.onState(c.channels[i], server.state(i), enet::Source::CACHE);
      ++count;
    }
    JsonDocument d;
    d["re"] = id;
    d["ok"] = true;
    d["count"] = count;
    emit(d);
  } else if (!strcmp(cmd, "scene")) {
    if (!radioReady(id)) return;
    int nr = in["nr"] | -1;
    if (nr < 0 || nr >= server.config().nScenes) return replyErr(id, "bad_args", "Szene unbekannt");
    queued(id, server.runScene((uint8_t)nr, id));
  } else if (!strcmp(cmd, "send")) {
    uint8_t frame[64];
    size_t n = parseHex(in["frame"] | "", frame, sizeof(frame));
    if (!n || n < 16) return replyErr(id, "bad_args", "frame (hex, ohne CRC)");
    if (!radioOk) return replyErr(id, "no_radio", "CC1101 nicht erkannt");
    frame[15] = (uint8_t)((frame[15] & 0xF1) | nextLfn() << 1);
    sendTelegram(frame, n);
    replyOk(id);
  } else if (!strcmp(cmd, "diag")) {
    replyOk(id);
    diagnose();
  } else if (!strcmp(cmd, "reboot")) {
    replyOk(id);
    Serial.flush();
    delay(50);
    ESP.restart();
  } else {
    replyErr(id, "unknown_cmd", cmd);
  }
}

// ---------------------------------------------------------------------------
// Text commands for debugging
// ---------------------------------------------------------------------------
static void handleText(char* line) {
  char* cmd = strtok(line, " ");
  if (!cmd) return;
  char* a1 = strtok(nullptr, " ");
  char* a2 = strtok(nullptr, " ");
  if (!strcmp(cmd, "v")) {
    verbose = !verbose;
    JsonDocument d;
    d["ev"] = "verbose";
    d["on"] = verbose;
    emit(d);
  } else if (!strcmp(cmd, "r")) {
    radio.dumpRegisters(Serial);
  } else if (!strcmp(cmd, "s")) {
    printStats(0);
  } else if (!strcmp(cmd, "d")) {
    diagnose();
  } else if (!strcmp(cmd, "w")) {  // write register (hex), e.g. "w 04 89"
    if (!a1 || !a2) return replyErr(0, "bad_args", "w <Reg> <Wert>");
    uint8_t reg = (uint8_t)strtoul(a1, nullptr, 16), v = (uint8_t)strtoul(a2, nullptr, 16);
    radio.strobe(CC1101::SIDLE);
    radio.writeReg(reg, v);
    radio.startRx();
    JsonDocument d;
    d["ev"] = "reg";
    d["addr"] = reg;
    d["value"] = radio.readReg(reg);
    emit(d);
  } else if (!strcmp(cmd, "spi")) {  // line test without SPI: CSn by hand, read MISO with pull-up/-down
    SPI.end();
    pinMode(PIN_CS, OUTPUT);
    JsonDocument d;
    d["ev"] = "spi_test";
    const char* namen[] = {"cs_hoch_pullup", "cs_hoch_pulldown", "cs_tief_pullup", "cs_tief_pulldown"};
    for (int i = 0; i < 4; ++i) {
      digitalWrite(PIN_CS, i < 2 ? HIGH : LOW);
      pinMode(PIN_MISO, (i % 2) ? INPUT_PULLDOWN : INPUT_PULLUP);
      delay(5);
      d[namen[i]] = digitalRead(PIN_MISO);  // CC1101: CS high = high impedance (follows the pull), CS low = 0 (ready)
    }
    digitalWrite(PIN_CS, HIGH);
    pinMode(PIN_SCK, INPUT_PULLDOWN);
    pinMode(PIN_MOSI, INPUT_PULLDOWN);
    pinMode(PIN_GDO0, INPUT_PULLDOWN);
    delay(2);
    d["sck"] = digitalRead(PIN_SCK);
    d["mosi"] = digitalRead(PIN_MOSI);
    d["gdo0"] = digitalRead(PIN_GDO0);
    d["gdo2"] = PIN_GDO2 >= 0 ? digitalRead(PIN_GDO2) : -1;
    radioOk = radio.begin(freqOffset);  // resume SPI
    radioVersion = radioOk ? radio.version() : 0;
    d["radio_ok"] = radioOk;
    d["version"] = radio.version();
    emit(d);
    if (radioOk) radio.startRx();
  } else if (!strcmp(cmd, "send")) {
    uint8_t frame[64];
    size_t n = parseHex(a1, frame, sizeof(frame));
    if (!n || n < 16) return replyErr(0, "bad_args", "send <Hex>");
    frame[15] = (uint8_t)((frame[15] & 0xF1) | nextLfn() << 1);
    sendTelegram(frame, n);
  } else {
    replyErr(0, "unknown_cmd", cmd);
  }
}

static void handleSerial() {
  static char line[512];
  static size_t pos = 0;
  static bool overflow = false;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (pos < sizeof(line) - 1) line[pos++] = c;
      else overflow = true;
      continue;
    }
    line[pos] = 0;
    pos = 0;
    if (overflow) {
      overflow = false;
      replyErr(0, "line_too_long", "Zeile laenger als 512 Byte");
      continue;
    }
    if (line[0] == '{') handleJson(line);
    else handleText(line);
  }
}

void setup() {
  // Several commands at once (e.g. "all lights on") must fit even while transmitting/receiving (blocked up to
  // ~50 ms); the default of 256 bytes overflowed
  Serial.setRxBufferSize(4096);
#if ARDUINO_USB_CDC_ON_BOOT  // native USB (e.g. XIAO ESP32-C6): buffer events, do not block without a host
  Serial.setTxBufferSize(4096);
  Serial.setTxTimeoutMs(0);
#endif
  Serial.begin(SERIAL_BAUD);
  delay(300);
  if (PIN_LED >= 0) pinMode(PIN_LED, OUTPUT);
  if (PIN_LED2 >= 0) pinMode(PIN_LED2, OUTPUT);
  ledSet(false);
  bridgeIo.server = &server;
  prefs.begin("enet", false);
  freqOffset = prefs.getChar("foff", CC1101_FREQ_OFFSET);
  radioOk = radio.begin(freqOffset);
  radioVersion = radioOk ? radio.version() : 0;
  loadServerConfig();
  JsonDocument d;
  d["ev"] = "start";
  fillStatus(d);
  d["reset_reason"] = (int)esp_reset_reason();
  emit(d);
  if (!radioOk) replyErr(0, "no_radio", "CC1101 nicht gefunden - Verkabelung/3,3 V pruefen");
  radio.startRx();
}

void loop() {
  const uint32_t now = millis();
  ledCheck(now);
  checkRadio(now);
  if (radioOk) {
    if (radio.syncDetected()) {
      receiveFrame();
    } else {
      static uint32_t tNoise = 0;
      const uint8_t marc = radio.marcState();
      if (marc == 0x11) {  // RX_OVERFLOW
        ++statOverflow;
        radio.startRx();
      } else if (marc == 0x0D && now - tNoise >= 5) {  // RX: noise floor for listen-before-talk
        tNoise = now;
        radio.noiseSample();
      }
    }
  }
  if (radioOk && cfgValid) server.tick(now);
  handleSerial();
}

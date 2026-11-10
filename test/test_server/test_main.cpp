// Host tests of the server functions: pio test -e native
#include <string.h>
#include <unity.h>

#include <string>
#include <vector>

#include "../../src/knxrf.cpp"
#include "../../src/server.cpp"

void setUp() {}
void tearDown() {}

static size_t fromHex(const char* s, uint8_t* out) {
  size_t n = 0;
  auto nib = [](char c) -> uint8_t { return c <= '9' ? c - '0' : (c | 0x20) - 'a' + 10; };
  while (s[0] && s[1]) {
    out[n++] = (uint8_t)(nib(s[0]) << 4 | nib(s[1]));
    s += 2;
  }
  return n;
}

static std::string toHex(const uint8_t* d, size_t n) {
  static const char* hx = "0123456789abcdef";
  std::string s;
  for (size_t i = 0; i < n; ++i) {
    s += hx[d[i] >> 4];
    s += hx[d[i] & 15];
  }
  return s;
}

struct FakeIo : enet::Io {
  std::vector<std::string> tx;
  std::vector<enet::KeyEvent> keys;
  std::vector<std::string> states, results;
  int sceneDone = -1, sceneFailed = -1, realServer = 0;
  uint32_t sceneId = 0;
  int busy = 0;  // this many transmissions fail because of a busy channel
  std::vector<std::string> props;
  void onPropResult(uint32_t id, uint16_t ia, enet::JobKind, uint8_t obj, uint8_t pid, uint16_t start, bool ok,
                    int rc, uint8_t tries, const uint8_t* val, size_t len) override {
    char b[96];
    int n = snprintf(b, sizeof(b), "#%u %04x %u/%u@%u %s rc%d t%u ", id, ia, obj, pid, start, ok ? "ok" : "fehler",
                     rc, tries);
    for (size_t i = 0; i < len && n < 90; ++i) n += snprintf(b + n, sizeof(b) - n, "%02x", val[i]);
    props.push_back(b);
  }
  bool transmit(const uint8_t* f, size_t n) override {
    if (busy > 0) {
      --busy;
      return false;
    }
    tx.push_back(toHex(f, n));
    return true;
  }
  void onKey(const enet::KeyEvent& e) override { keys.push_back(e); }
  void onState(const enet::Channel& ch, const enet::ChannelState& st, enet::Source) override {
    char b[64];
    snprintf(b, sizeof(b), "%04x/%u %s %u", ch.ia, ch.offset, st.on ? "ein" : "aus", st.percent);
    states.push_back(b);
  }
  void onResult(uint32_t id, uint16_t ia, uint16_t h, bool rd, enet::Origin, bool ok, int rc, uint8_t tries,
                const uint8_t*, size_t) override {
    char b[80];
    snprintf(b, sizeof(b), "#%u %04x h%u %s %s rc%d t%u", (unsigned)id, ia, h, rd ? "read" : "set",
             ok ? "ok" : "fehler", rc, tries);
    results.push_back(b);
  }
  void onSceneDone(uint32_t id, uint8_t s, uint8_t f) override {
    sceneDone = s;
    sceneFailed = f;
    sceneId = id;
  }
  void onRealServer() override { ++realServer; }
};

static const uint8_t kDomain[6] = {0x00, 0x0A, 0x12, 0x34, 0x56, 0x00};
static const uint8_t kServerSN[6] = {0x00, 0x0A, 0x65, 0x43, 0x21, 0x15};
static const uint8_t kHandsender[6] = {0x00, 0x0A, 0x23, 0x45, 0x67, 0x16};
static const uint8_t kStylos[6] = {0x00, 0x0A, 0x34, 0x56, 0x78, 0x12};
static const uint8_t kDecke[6] = {0x00, 0x0A, 0x0C, 0x54, 0x33, 0x15};
static const uint8_t kMini[6] = {0x00, 0x0A, 0x45, 0x67, 0x89, 0x17};

// Excerpt of the test installation: Stylos K1/K2, ceiling light by the door; scene 0 = Stylos K1 on + ceiling value 0
static enet::Config testanlageConfig() {
  enet::Config c;
  memcpy(c.domain, kDomain, 6);
  memcpy(c.serverSn, kServerSN, 6);
  c.channels[0] = {0x0004, {}, 0, false, 0, 1, 3};
  memcpy(c.channels[0].sn, kStylos, 6);
  c.channels[1] = {0x0004, {}, 26, false, 0, 2, 4};
  memcpy(c.channels[1].sn, kStylos, 6);
  c.channels[2] = {0x0005, {}, 0, true, 0, 1, 3};
  memcpy(c.channels[2].sn, kDecke, 6);
  c.channels[3] = {0x001E, {}, 0, true, 3, 1, 3};  // mini dimmer after factory reset: GA base 3
  memcpy(c.channels[3].sn, kMini, 6);
  c.nChannels = 4;
  c.actions[0] = {0x0004, 2, 1, {0x01}};
  c.actions[1] = {0x0005, 4, 1, {0x00}};
  c.nActions = 2;
  c.scenes[0] = {0, 2};
  c.nScenes = 1;
  memcpy(c.keys[0].sn, kHandsender, 6);
  c.keys[0].ga = 6;
  c.keys[0].scene = 0;
  memcpy(c.keys[1].sn, kHandsender, 6);
  c.keys[1].ga = 12;
  c.keys[1].scene = -1;  // report only
  c.nKeys = 2;
  return c;
}

static void feed(enet::Server& s, const char* hex, uint32_t now) {
  uint8_t b[64];
  size_t n = fromHex(hex, b);
  knxrf::Telegram t;
  TEST_ASSERT_TRUE(knxrf::parseTelegram(b, n, t));
  s.onFrame(t, now);
}

// real telegrams (captures 2026-10-23/26)
static const char* kPressW1Oben = "1f44ff02000a234567160000030006ac008001000a6543211500324000031208";
static const char* kReleaseW1Oben = "1f44ff02000a234567160000030006ae008001000a6543211500320000031308";
static const char* kRepeatRelease = "1f44ff02000a234567160000030006a0008081000a6543211500320000031308";
static const char* kPressW1Unten = "1f44ff02000a23456716000003000ca2008001000a6543211500334f00022a08";
static const char* kAckStylos = "1444ff02000a123456000000047fff6902c9003e00";
static const char* kAckDecke = "1444ff02000a123456000000057fff6902c9003e00";
static const char* kStylosInfoEin = "1444ff02000a345678120000040016a20080e4ffff";
static const char* kServerInfo = "1444ff02000a65432115007fff0032e40080ff02fe";

void test_config_roundtrip() {
  enet::Config c = testanlageConfig(), d;
  uint8_t blob[512];
  size_t n = enet::serializeConfig(c, blob, sizeof(blob));
  TEST_ASSERT_TRUE(n > 0);
  TEST_ASSERT_TRUE(enet::parseConfig(blob, n, d));
  TEST_ASSERT_EQUAL(4, d.nChannels);
  TEST_ASSERT_EQUAL(3, d.channels[3].gaBase);
  TEST_ASSERT_EQUAL(2, d.channels[1].ch);
  TEST_ASSERT_EQUAL(4, d.channels[1].obj);
  TEST_ASSERT_EQUAL(26, d.channels[1].offset);
  TEST_ASSERT_TRUE(d.channels[2].dimmer);
  TEST_ASSERT_EQUAL(-1, d.keys[1].scene);
  TEST_ASSERT_EQUAL_HEX8_ARRAY(kServerSN, d.serverSn, 6);
  blob[10] ^= 1;  // CRC wrong
  TEST_ASSERT_FALSE(enet::parseConfig(blob, n, d));
}

void test_config_empty() {  // new installation without devices: header + CRC only (22 bytes)
  enet::Config c = testanlageConfig(), d;
  c.nChannels = c.nActions = c.nScenes = c.nKeys = 0;
  uint8_t blob[64];
  size_t n = enet::serializeConfig(c, blob, sizeof(blob));
  TEST_ASSERT_EQUAL(22, n);
  TEST_ASSERT_TRUE(enet::parseConfig(blob, n, d));
  TEST_ASSERT_EQUAL(0, d.nChannels);
  TEST_ASSERT_EQUAL_HEX8_ARRAY(kServerSN, d.serverSn, 6);
}

void test_disabled_does_not_answer() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  feed(s, kPressW1Oben, 0);
  s.tick(0);
  TEST_ASSERT_EQUAL(0, io.tx.size());
  TEST_ASSERT_EQUAL(1, io.keys.size());
  TEST_ASSERT_FALSE(io.keys[0].answered);
  TEST_ASSERT_EQUAL(0, io.keys[0].keyIndex);
}

void test_key_answer_and_scene() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  s.setEnabled(true);
  feed(s, kPressW1Oben, 1000);
  // Response immediately, byte-identical to the server (LFN 0, the bridge sets it when transmitting)
  TEST_ASSERT_EQUAL(1, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1444ff02000a65432115007fff0032e00080ff0312", io.tx[0].c_str());
  TEST_ASSERT_TRUE(io.keys[0].press);
  TEST_ASSERT_TRUE(io.keys[0].sceneStarted);
  s.tick(1001);  // first scene command
  TEST_ASSERT_EQUAL(2, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1644ff02000a12345600007fff00046102c7003e000201", io.tx[1].c_str());
  feed(s, kAckStylos, 1045);
  s.tick(1046);  // second command only after acknowledgement
  TEST_ASSERT_EQUAL(3, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1644ff02000a12345600007fff00056102c7003e000400", io.tx[2].c_str());
  feed(s, kAckDecke, 1090);
  TEST_ASSERT_EQUAL(0, io.sceneDone);
  TEST_ASSERT_EQUAL(0, io.sceneFailed);
  // Release and repetition: answer, but no new scene
  feed(s, kReleaseW1Oben, 1200);
  feed(s, kRepeatRelease, 1300);
  s.tick(1301);
  TEST_ASSERT_EQUAL(5, io.tx.size());
  TEST_ASSERT_FALSE(io.keys[1].sceneStarted);
  TEST_ASSERT_FALSE(io.keys[2].sceneStarted);
  // same seq on press again (repetition) -> no second scene
  feed(s, kPressW1Oben, 1400);
  TEST_ASSERT_FALSE(io.keys[3].sceneStarted);
}

// Press lost (bridge was transmitting): release with seq +2 relative to the last seen one recalls the scene
void test_key_missed_press() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  s.setEnabled(true);
  // after startup: single release without history -> no scene
  feed(s, kReleaseW1Oben, 1000);  // seq 0313
  TEST_ASSERT_FALSE(io.keys[0].sceneStarted);
  TEST_ASSERT_TRUE(io.keys[0].answered);
  // Press 0314 lost, release 0315 arrives -> scene
  std::string rel = kReleaseW1Oben;
  rel.replace(rel.find("00320000031308"), 14, "00320000031508");
  feed(s, rel.c_str(), 2000);
  TEST_ASSERT_TRUE(io.keys[1].sceneStarted);
  // repetition of this release -> no second scene
  feed(s, rel.c_str(), 2100);
  TEST_ASSERT_FALSE(io.keys[2].sceneStarted);
  // next pair normal: press 0316 -> scene, release 0317 -> none
  std::string pr = kPressW1Oben;
  pr.replace(pr.find("00324000031208"), 14, "00324000031608");
  std::string rel2 = kReleaseW1Oben;
  rel2.replace(rel2.find("00320000031308"), 14, "00320000031708");
  feed(s, pr.c_str(), 3000);
  feed(s, rel2.c_str(), 3200);
  TEST_ASSERT_TRUE(io.keys[3].sceneStarted);
  TEST_ASSERT_FALSE(io.keys[4].sceneStarted);
}

void test_key_report_only() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  s.setEnabled(true);
  feed(s, kPressW1Unten, 0);
  s.tick(1);
  TEST_ASSERT_EQUAL(1, io.tx.size());  // only the response
  TEST_ASSERT_EQUAL(1, io.keys[0].keyIndex);
  TEST_ASSERT_FALSE(io.keys[0].sceneStarted);
}

// Transmitter after factory reset: GA base 3 instead of 0 -> same key (only the lower byte counts)
void test_key_other_ga_base() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  s.setEnabled(true);
  uint8_t b[64];
  size_t n = fromHex(kPressW1Oben, b);
  knxrf::Telegram t;
  TEST_ASSERT_TRUE(knxrf::parseTelegram(b, n, t));
  t.dst = 0x0306;
  s.onFrame(t, 0);
  TEST_ASSERT_EQUAL(0, io.keys[0].keyIndex);
  TEST_ASSERT_TRUE(io.keys[0].sceneStarted);
}

void test_retry_and_fail() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  const uint8_t on = 1;
  s.queueSet(0x0004, 2, &on, 1, enet::Origin::HOST, 42);
  s.tick(0);
  s.tick(399);
  TEST_ASSERT_EQUAL(1, io.tx.size());
  s.tick(400);   // attempt 2, next timeout 550 ms
  s.tick(949);
  TEST_ASSERT_EQUAL(2, io.tx.size());
  s.tick(950);   // 3 (700 ms)
  s.tick(1650);  // 4 (850 ms)
  s.tick(2500);  // 5 (1000 ms)
  TEST_ASSERT_EQUAL(5, io.tx.size());
  s.tick(3499);
  TEST_ASSERT_EQUAL(0, io.results.size());
  s.tick(3500);
  TEST_ASSERT_EQUAL(1, io.results.size());
  TEST_ASSERT_EQUAL_STRING("#42 0004 h2 set fehler rc-1 t5", io.results[0].c_str());
  TEST_ASSERT_FALSE(s.busy());
}

void test_busy_channel_keeps_try() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  const uint8_t on = 1;
  io.busy = 2;
  s.queueSet(0x0004, 2, &on, 1, enet::Origin::HOST, 7);
  s.tick(0);    // busy
  s.tick(20);   // busy
  TEST_ASSERT_EQUAL(0, io.tx.size());
  s.tick(40);   // sent, still attempt 1
  TEST_ASSERT_EQUAL(1, io.tx.size());
  s.tick(439);
  TEST_ASSERT_EQUAL(1, io.tx.size());
  // acknowledgement of the actuator
  feed(s, kAckStylos, 480);
  TEST_ASSERT_EQUAL(1, io.results.size());
  TEST_ASSERT_EQUAL_STRING("#7 0004 h2 set ok rc0 t1", io.results[0].c_str());
}

// Response of an actuator 0.0.5 (under domain) to A_PropertyValue_Read
static void feedPropResp(enet::Server& s, uint16_t src, uint8_t obj, uint8_t pid, uint16_t start, uint8_t count,
                         const char* valHex, uint32_t now) {
  knxrf::Telegram t;
  static const uint8_t dom[6] = {0x00, 0x0A, 0x12, 0x34, 0x56, 0x00};
  memcpy(t.addr, dom, 6);
  t.doa = true;
  t.src = src;
  t.dst = knxrf::kServerIA;
  t.apci = knxrf::APCI_PROP_VALUE_RESP;
  t.data[0] = obj;
  t.data[1] = pid;
  t.data[2] = (uint8_t)(count << 4 | start >> 8);
  t.data[3] = (uint8_t)start;
  t.dataLen = 4 + fromHex(valHex, t.data + 4);
  uint8_t raw[64];
  size_t n = knxrf::buildTelegram(t, raw);
  knxrf::Telegram back;
  TEST_ASSERT_TRUE(knxrf::parseTelegram(raw, n, back));
  s.onFrame(back, now);
}

void test_prop_read_write() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  // Read minimum brightness: obj 3 PID 0x4F start 0x10F, 1 byte
  TEST_ASSERT_TRUE(s.queueProp(enet::JobKind::PROP_READ, 0x0005, 3, 0x4F, 0x10F, 1, nullptr, 0, 11));
  s.tick(0);
  TEST_ASSERT_EQUAL(1, io.tx.size());
  // ... 7fff 0005 | NPCI | 03d5 | 03 4f 11 0f
  TEST_ASSERT_EQUAL_STRING("034f110f", io.tx[0].substr(io.tx[0].size() - 8).c_str());
  TEST_ASSERT_EQUAL_STRING("d5", io.tx[0].substr(io.tx[0].size() - 10, 2).c_str());
  feedPropResp(s, 0x0006, 3, 0x4F, 0x10F, 1, "19", 40);  // wrong sender: ignore
  feedPropResp(s, 0x0005, 3, 0x4F, 0x110, 1, "19", 45);  // wrong start: ignore
  TEST_ASSERT_EQUAL(0, io.props.size());
  feedPropResp(s, 0x0005, 3, 0x4F, 0x10F, 1, "19", 50);
  TEST_ASSERT_EQUAL(1, io.props.size());
  TEST_ASSERT_EQUAL_STRING("#11 0005 3/79@271 ok rc0 t1 19", io.props[0].c_str());
  // Write, device rejects (count 0)
  const uint8_t v[] = {0x0c};
  TEST_ASSERT_TRUE(s.queueProp(enet::JobKind::PROP_WRITE, 0x0005, 3, 0x4F, 0x10F, 1, v, 1, 12));
  s.tick(100);
  TEST_ASSERT_EQUAL_STRING("034f110f0c", io.tx[1].substr(io.tx[1].size() - 10).c_str());
  feedPropResp(s, 0x0005, 3, 0x4F, 0x10F, 0, "", 150);
  TEST_ASSERT_EQUAL_STRING("#12 0005 3/79@271 fehler rc1 t1 ", io.props[1].c_str());
  // NetworkParameter without response: done after sending
  const uint8_t np[] = {0x00, 0x12, 0x3c, 0x00};
  TEST_ASSERT_TRUE(s.queueNpWrite(0x0005, np, 4, 13));
  s.tick(200);
  TEST_ASSERT_EQUAL(3, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("#13 0005 0/0@0 ok rc0 t1 ", io.props[2].c_str());
  TEST_ASSERT_FALSE(s.busy());
}

// FuncProp response (0x2C9) of an actuator to the server, payload = obj pid rc ...
static void feedFp(enet::Server& s, uint16_t ia, const char* payload, uint32_t now) {
  char body[160];
  snprintf(body, sizeof(body), "44ff02000a1234560000%04x7fff6902c9%s", ia, payload);
  char hex[164];
  snprintf(hex, sizeof(hex), "%02x%s", (unsigned)(strlen(body) / 2), body);
  feed(s, hex, now);
}

void test_link_table() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  // Link table of the ceiling light by the door, channel object 3, GO 6, read entry 0
  const uint8_t rd[] = {0x00, 0x00, 0x06, 0x00};
  TEST_ASSERT_TRUE(s.queueFuncProp(enet::JobKind::FP_READ, 0x0005, 3, 63, rd, 4, 21));
  s.tick(0);
  TEST_ASSERT_EQUAL_STRING("1744ff02000a12345600007fff00056102c8033f00000600", io.tx[0].c_str());
  feedFp(s, 0x0005, "003e00", 20);  // acknowledgement of a switch command (obj 0, PID 62): ignore
  TEST_ASSERT_EQUAL(0, io.props.size());
  feedFp(s, 0x0005, "033f0000000a65432115002d0006", 40);  // Server-SN, GA 45 -> GO 6
  TEST_ASSERT_EQUAL(1, io.props.size());
  TEST_ASSERT_EQUAL_STRING("#21 0005 3/63@0 ok rc0 t1 00000a65432115002d0006", io.props[0].c_str());
  // End of the table: rc ff
  TEST_ASSERT_TRUE(s.queueFuncProp(enet::JobKind::FP_READ, 0x0005, 3, 63, rd, 4, 22));
  s.tick(100);
  feedFp(s, 0x0005, "033fff", 140);
  TEST_ASSERT_EQUAL(2, io.props.size());
  TEST_ASSERT_EQUAL_STRING("#22 0005 3/63@0 fehler rc255 t1 ", io.props[1].c_str());
  // Create entry: server SN, GA 100 -> GO 6
  const uint8_t wr[] = {0x00, 0x00, 0x00, 0x0a, 0x65, 0x43, 0x21, 0x15, 0x00, 0x64, 0x00, 0x06};
  TEST_ASSERT_TRUE(s.queueFuncProp(enet::JobKind::FP_CMD, 0x0005, 3, 63, wr, sizeof(wr), 23));
  s.tick(200);
  TEST_ASSERT_EQUAL_STRING("1f44ff02000a12345600007fff00056102c7033f0000000a6543211500640006", io.tx[2].c_str());
  feedFp(s, 0x0005, "033f00", 240);
  TEST_ASSERT_EQUAL(3, io.props.size());
  TEST_ASSERT_EQUAL_STRING("#23 0005 3/63@0 ok rc0 t1 ", io.props[2].c_str());
}

void test_scene_call_group() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  // Scene 7 to GA 48 (work light), byte-identical to the live test 2026-10-28 (LFN 0)
  const uint8_t nr[] = {0x07};
  TEST_ASSERT_TRUE(s.queueGroupWrite(48, nr, 1, 31));
  s.tick(0);
  TEST_ASSERT_EQUAL(1, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1244ff02000a65432115007fff0030e0008007", io.tx[0].c_str());
  s.tick(50);
  TEST_ASSERT_EQUAL(1, io.tx.size());  // no acknowledgement, gap kGroupGapMs
  s.tick(80);
  s.tick(160);
  TEST_ASSERT_EQUAL(3, io.tx.size());
  TEST_ASSERT_EQUAL(0, io.props.size());
  s.tick(240);
  TEST_ASSERT_EQUAL(3, io.tx.size());
  TEST_ASSERT_EQUAL(1, io.props.size());
  TEST_ASSERT_EQUAL_STRING("#31 0000 0/0@48 ok rc0 t3 ", io.props[0].c_str());
  TEST_ASSERT_FALSE(s.busy());
}

void test_scene_with_group_action() {
  FakeIo io;
  enet::Server s(io);
  enet::Config c = testanlageConfig();
  c.actions[2] = {0x0000, 45, 1, {0x06}};  // IA 0: scene recall "Alles an" (all on; GA 45, no. 6)
  c.nActions = 3;
  c.scenes[1] = {2, 1};
  c.nScenes = 2;
  s.setConfig(c);
  TEST_ASSERT_TRUE(s.runScene(1, 41));
  for (uint32_t t = 0; t <= 300; t += 10) s.tick(t);
  TEST_ASSERT_EQUAL(3, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1244ff02000a65432115007fff002de0008006", io.tx[2].c_str());
  TEST_ASSERT_EQUAL(1, io.sceneDone);
  TEST_ASSERT_EQUAL(0, io.sceneFailed);
  TEST_ASSERT_EQUAL(0, io.props.size());
}

void test_poll_after_command() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  const uint8_t zero = 0;
  s.queueSet(0x0005, 4, &zero, 1);  // ceiling value 0
  s.tick(0);
  feed(s, kAckDecke, 40);
  s.tick(7999);
  TEST_ASSERT_EQUAL(1, io.tx.size());
  s.tick(8040);  // no status message -> read GO 20
  TEST_ASSERT_EQUAL(2, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1544ff02000a12345600007fff00056102c8003e0014", io.tx[1].c_str());
  feed(s, "1544ff02000a123456000000057fff6d02c9003e0000", 8080);
  s.tick(8081);  // dimmer: read GO 21
  TEST_ASSERT_EQUAL_STRING("1544ff02000a12345600007fff00056102c8003e0015", io.tx[2].c_str());
  feed(s, "1544ff02000a123456000000057fff6f02c9003e0000", 8120);
  TEST_ASSERT_EQUAL(2, io.states.size());  // every poll is reported (GO 20 and GO 21), even if unchanged
  TEST_ASSERT_EQUAL_STRING("0005/0 aus 0", io.states[0].c_str());
  TEST_ASSERT_EQUAL_STRING("0005/0 aus 0", io.states[1].c_str());
  TEST_ASSERT_TRUE(s.state(2).known);
}

void test_status_message_cancels_poll() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  const uint8_t on = 1;
  s.queueSet(0x0004, 2, &on, 1);
  s.tick(0);
  feed(s, kAckStylos, 40);
  feed(s, kStylosInfoEin, 1500);  // Stylos reports by itself
  TEST_ASSERT_EQUAL_STRING("0004/0 ein 100", io.states[0].c_str());
  s.tick(9000);
  TEST_ASSERT_EQUAL(1, io.tx.size());  // no poll
}

void test_status_ga_base() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  feed(s, "1444ff02000a4567891700001e0316a20080e4ffff", 0);  // 0.0.30 to GA 790 = 3*256 + 22
  TEST_ASSERT_EQUAL(1, io.states.size());
  TEST_ASSERT_EQUAL_STRING("001e/0 ein 100", io.states[0].c_str());
  feed(s, "1444ff02000a4567891700001e0016a2008000ffff", 0);  // different base (0): still valid (lower byte)
  TEST_ASSERT_EQUAL(2, io.states.size());
  TEST_ASSERT_EQUAL_STRING("001e/0 aus 0", io.states[1].c_str());
  feed(s, "1444ff02000a4567891700001e0315a20080e4ffff", 0);  // different GO (GA 789) -> ignored
  TEST_ASSERT_EQUAL(2, io.states.size());
}

void test_key_kinds_and_lookup() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  s.setEnabled(true);
  // linked rocker (hand-held transmitter W2 -> Stylos): report, do not answer
  feed(s, "1f44ff02000a234567160000030039a6008001000a34567812001640000724f0", 0);
  TEST_ASSERT_EQUAL(0, io.tx.size());
  TEST_ASSERT_EQUAL(1, io.keys.size());
  TEST_ASSERT_TRUE(io.keys[0].kind == enet::KeyKind::DIRECT);
  TEST_ASSERT_TRUE(io.keys[0].press);
  TEST_ASSERT_FALSE(io.keys[0].answered);
  // Release after hold duration
  feed(s, "1f44ff02000a234567160000030039a8008001000a34567812001640220725f0", 300);
  TEST_ASSERT_FALSE(io.keys[1].press);
  // unassigned rocker: bare key value
  feed(s, "1344ff02000a23456716000003003eaa00804000", 500);
  TEST_ASSERT_TRUE(io.keys[2].kind == enet::KeyKind::VALUE);
  TEST_ASSERT_EQUAL_HEX16(0x4000, io.keys[2].cmd);
  TEST_ASSERT_TRUE(io.keys[2].press);
  TEST_ASSERT_EQUAL_HEX8(0x02, io.keys[2].rfInfo);  // battery OK
  // RF info 0x00: battery weak, passed on with the key press
  feed(s, "1344ff00000a23456716000003003eac00804000", 520);
  TEST_ASSERT_EQUAL(4, io.keys.size());
  TEST_ASSERT_EQUAL_HEX8(0x00, io.keys[3].rfInfo);
  // Sun sensor (capture 2026-11-06): brightness 2 bytes to GA 0x204, scene 50 as 1 byte to GA 0x207
  feed(s, "1344ff00000a03a417110000090204a600803436", 530);
  TEST_ASSERT_EQUAL(5, io.keys.size());
  TEST_ASSERT_EQUAL((int)enet::KeyKind::VALUE, (int)io.keys[4].kind);
  TEST_ASSERT_EQUAL_HEX16(0x3436, io.keys[4].cmd);
  feed(s, "1244ff00000a03a417110000090207ae008072", 540);
  TEST_ASSERT_EQUAL(6, io.keys.size());
  TEST_ASSERT_EQUAL((int)enet::KeyKind::VALUE1, (int)io.keys[5].kind);
  TEST_ASSERT_EQUAL_HEX16(0x0207, io.keys[5].ga);
  TEST_ASSERT_EQUAL_HEX16(0x72, io.keys[5].cmd);
  // channel lookup by channel number
  TEST_ASSERT_EQUAL(1, s.channelIndexByCh(0x0004, 2));
  TEST_ASSERT_EQUAL(-1, s.channelIndexByCh(0x0004, 3));
  // scene with job number
  TEST_ASSERT_TRUE(s.runScene(0, 77));
  s.tick(600);
  feed(s, kAckStylos, 640);
  s.tick(641);
  feed(s, kAckDecke, 690);
  TEST_ASSERT_EQUAL(77, io.sceneId);
  TEST_ASSERT_EQUAL(0, io.results.size());  // scenes do not report individual results
}

void test_real_server_reported() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  s.setEnabled(true);
  feed(s, kServerInfo, 0);
  TEST_ASSERT_TRUE(s.enabled());  // only report, never switch off by itself
  TEST_ASSERT_EQUAL(1, io.realServer);
  feed(s, kServerInfo, 30000);
  TEST_ASSERT_EQUAL(1, io.realServer);  // at most once per minute
  feed(s, kServerInfo, 60000);
  TEST_ASSERT_EQUAL(2, io.realServer);
  feed(s, kPressW1Oben, 60010);
  TEST_ASSERT_EQUAL(1, io.tx.size());  // key is still answered
}

// Blind (catalog, untested on the device): messages GO10/11/12/13, staged polls after a command
static const uint8_t kJal[6] = {0x00, 0x0A, 0x54, 0x27, 0x00, 0x18};
static enet::Config jalousieConfig() {
  enet::Config c = testanlageConfig();
  c.channels[4] = {0x0020, {}, 0, false, 2, 1, 3, true};
  memcpy(c.channels[4].sn, kJal, 6);
  c.nChannels = 5;
  return c;
}

void test_blind_config_and_states() {
  uint8_t blob[512];
  enet::Config c = jalousieConfig(), r;
  size_t n = enet::serializeConfig(c, blob, sizeof(blob));
  TEST_ASSERT_TRUE(enet::parseConfig(blob, n, r));
  TEST_ASSERT_TRUE(r.channels[4].blind);
  TEST_ASSERT_FALSE(r.channels[4].dimmer);
  TEST_ASSERT_FALSE(r.channels[2].blind);
  FakeIo io;
  enet::Server s(io);
  s.setConfig(c);
  feed(s, "1244ff02000a54270018000020020ca2008080", 0);  // GO12 position 0x80 → 50 %
  TEST_ASSERT_EQUAL(50, s.state(4).percent);
  TEST_ASSERT_TRUE(s.state(4).on);
  feed(s, "1144ff02000a54270018000020020aa20083", 0);  // GO10 short value 3: moving down
  TEST_ASSERT_EQUAL(3, s.state(4).move);
  feed(s, "1244ff02000a54270018000020020da20080ff", 0);  // GO13 slat 100 %
  TEST_ASSERT_EQUAL(100, s.state(4).slat);
  feed(s, "1444ff02000a54270018000020020ba20080e4ffff", 0);  // GO11 sum status: 100 %
  TEST_ASSERT_EQUAL(100, s.state(4).percent);
  TEST_ASSERT_EQUAL(4, io.states.size());
  TEST_ASSERT_EQUAL(0, io.keys.size());  // no key events from the 1-byte messages
  feed(s, "1244ff02000a54270018000020020ca2008000", 0);  // fully up
  TEST_ASSERT_FALSE(s.state(4).on);
  // same GA, different sender: sun sensor scene (VALUE1), no blind state
  feed(s, "1244ff02000a4567891700001e020ca2008005", 0);
  TEST_ASSERT_EQUAL(1, io.keys.size());
}

void test_blind_poll_stages() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(jalousieConfig());
  const uint8_t down = 1;
  s.queueSet(0x0020, enet::kGoBlindMove, &down, 1);
  s.tick(0);
  feed(s, "1444ff02000a123456000000207fff6902c9003e00", 40);  // acknowledgement
  TEST_ASSERT_TRUE(s.state(4).pollPending);
  TEST_ASSERT_EQUAL_UINT32(8040, s.state(4).pollAt);
  s.tick(8040);  // first poll (GO 12 and 13), next stage scheduled
  TEST_ASSERT_TRUE(s.state(4).pollPending);
  TEST_ASSERT_EQUAL_UINT32(25040, s.state(4).pollAt);
  TEST_ASSERT_EQUAL_STRING("1544ff02000a12345600007fff00206102c8003e000c", io.tx[1].c_str());
  feed(s, kStylosInfoEin, 9000);  // message of another channel does not change the blind poll
  TEST_ASSERT_TRUE(s.state(4).pollPending);
}

// Format 3 (firmware before 0.8.23) is still accepted: no GO numbers → defaults of the channel kind
void test_config_v3_defaults() {
  uint8_t b[64] = {'E', 0x03};
  size_t n = 2;
  memcpy(b + n, kDomain, 6), n += 6;
  memcpy(b + n, kServerSN, 6), n += 6;
  b[n++] = 0x7F, b[n++] = 0xFF;                      // server IA
  b[n++] = 1, b[n++] = 0, b[n++] = 0, b[n++] = 0;    // 1 channel
  b[n++] = 0x00, b[n++] = 0x20;                      // IA
  memcpy(b + n, kJal, 6), n += 6;
  b[n++] = 1, b[n++] = 0, b[n++] = 3, b[n++] = 2, b[n++] = 0;  // ch 1, offset 0, obj 3, blind, base 0
  uint16_t crc = knxrf::crc16(b, n);
  b[n++] = crc >> 8, b[n++] = crc & 0xFF;
  enet::Config c;
  TEST_ASSERT_TRUE(enet::parseConfig(b, n, c));
  TEST_ASSERT_TRUE(c.channels[0].blind);
  TEST_ASSERT_EQUAL(enet::kGoBlindPosNow, enet::goOf(c.channels[0], enet::kSlotPosNow));
  TEST_ASSERT_EQUAL(enet::kSpanBlind, enet::goOf(c.channels[0], enet::kSlotSpan));
  c.channels[0].blind = false;
  TEST_ASSERT_EQUAL(enet::kGoSumstatusInfo, enet::goOf(c.channels[0], enet::kSlotSumInfo));
}

// Channel type variants: GO numbers from the configuration (format 4)
void test_variant_layouts() {
  enet::Config c = jalousieConfig(), r;
  // mini dimmer as CT_1F0C (Lighting Actuator v2): status 24, value status 25, sum status info 26, span 29
  const uint8_t dim[enet::kGoSlots] = {2, 4, 24, 25, 26, 0, 0, 0, 0, 29};
  memcpy(c.channels[3].go, dim, sizeof(dim));
  // blind as CT_1F10 (Blinds Actuator v3): movement info 11, sum status 12, position 13, slat 14, span 18
  const uint8_t bl[enet::kGoSlots] = {2, 3, 4, 5, 6, 11, 12, 13, 14, 18};
  memcpy(c.channels[4].go, bl, sizeof(bl));
  uint8_t blob[512];
  size_t n = enet::serializeConfig(c, blob, sizeof(blob));
  TEST_ASSERT_TRUE(enet::parseConfig(blob, n, r));
  TEST_ASSERT_EQUAL_HEX8_ARRAY(dim, r.channels[3].go, enet::kGoSlots);
  TEST_ASSERT_EQUAL(0, r.channels[0].go[enet::kSlotStatus]);  // default channel: 0 = default
  TEST_ASSERT_EQUAL(enet::kGoStatus, enet::goOf(r.channels[0], enet::kSlotStatus));
  FakeIo io;
  enet::Server s(io);
  s.setConfig(r);
  feed(s, "1444ff02000a4567891700001e0316a20080e4ffff", 0);  // GA 3*256 + 22: not the status GO of CT_1F0C
  TEST_ASSERT_EQUAL(0, io.states.size());
  feed(s, "1444ff02000a4567891700001e031aa20080e4ffff", 0);  // GA 3*256 + 26
  TEST_ASSERT_EQUAL(1, io.states.size());
  TEST_ASSERT_EQUAL_STRING("001e/0 ein 100", io.states[0].c_str());
  feed(s, "1244ff02000a54270018000020020da2008080", 0);  // blind GO13 = position in CT_1F10 → 50 %
  TEST_ASSERT_EQUAL(50, s.state(4).percent);
  feed(s, "1244ff02000a54270018000020020ca20080ff", 0);  // GO12 is the sum status there (3 bytes): ignored
  TEST_ASSERT_EQUAL(50, s.state(4).percent);
  const uint8_t down = 1;
  s.queueSet(0x0020, 2, &down, 1);
  s.tick(0);
  feed(s, "1444ff02000a123456000000207fff6902c9003e00", 40);  // acknowledgement
  s.tick(8040);                                                // poll position GO 13 (handle 0x0d)
  TEST_ASSERT_EQUAL_STRING("1544ff02000a12345600007fff00206102c8003e000d", io.tx[1].c_str());
}

// Energy sensor: 4-byte value (e.g. active power DPT 14) as VALUE4 with GA and value
void test_value4() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  feed(s, "1544ff02000a547100000000210105a2008042c80000", 0);
  TEST_ASSERT_EQUAL(1, io.keys.size());
  TEST_ASSERT_EQUAL((int)enet::KeyKind::VALUE4, (int)io.keys[0].kind);
  TEST_ASSERT_EQUAL_HEX32(0x42C80000, io.keys[0].value32);  // 100.0 W
  TEST_ASSERT_EQUAL_HEX16(0x0105, io.keys[0].ga);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_config_roundtrip);
  RUN_TEST(test_config_empty);
  RUN_TEST(test_disabled_does_not_answer);
  RUN_TEST(test_key_answer_and_scene);
  RUN_TEST(test_key_missed_press);
  RUN_TEST(test_key_report_only);
  RUN_TEST(test_key_other_ga_base);
  RUN_TEST(test_retry_and_fail);
  RUN_TEST(test_poll_after_command);
  RUN_TEST(test_status_message_cancels_poll);
  RUN_TEST(test_status_ga_base);
  RUN_TEST(test_key_kinds_and_lookup);
  RUN_TEST(test_real_server_reported);
  RUN_TEST(test_busy_channel_keeps_try);
  RUN_TEST(test_prop_read_write);
  RUN_TEST(test_link_table);
  RUN_TEST(test_scene_call_group);
  RUN_TEST(test_scene_with_group_action);
  RUN_TEST(test_blind_config_and_states);
  RUN_TEST(test_blind_poll_stages);
  RUN_TEST(test_config_v3_defaults);
  RUN_TEST(test_variant_layouts);
  RUN_TEST(test_value4);
  return UNITY_END();
}

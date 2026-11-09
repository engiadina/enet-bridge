// Host-Tests der Server-Funktionen: pio test -e native
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
  int busy = 0;  // so viele Sendungen scheitern an belegtem Kanal
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

// Ausschnitt der Testanlage: Stylos K1/K2, Decke Tuer; Szene 0 = Stylos K1 ein + Decke Wert 0
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
  c.channels[3] = {0x001E, {}, 0, true, 3, 1, 3};  // Mini-Dimmer nach Werksreset: GA-Basis 3
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
  c.keys[1].scene = -1;  // nur melden
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

// echte Telegramme (Mitschnitte 2026-10-23/26)
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
  blob[10] ^= 1;  // CRC falsch
  TEST_ASSERT_FALSE(enet::parseConfig(blob, n, d));
}

void test_config_empty() {  // neue Anlage ohne Geräte: nur Kopf + CRC (22 Byte)
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
  // Antwort sofort, byte-gleich zum Server (LFN 0, die Bridge setzt sie beim Senden)
  TEST_ASSERT_EQUAL(1, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1444ff02000a65432115007fff0032e00080ff0312", io.tx[0].c_str());
  TEST_ASSERT_TRUE(io.keys[0].press);
  TEST_ASSERT_TRUE(io.keys[0].sceneStarted);
  s.tick(1001);  // erster Szenenbefehl
  TEST_ASSERT_EQUAL(2, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1644ff02000a12345600007fff00046102c7003e000201", io.tx[1].c_str());
  feed(s, kAckStylos, 1045);
  s.tick(1046);  // zweiter Befehl erst nach Quittung
  TEST_ASSERT_EQUAL(3, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1644ff02000a12345600007fff00056102c7003e000400", io.tx[2].c_str());
  feed(s, kAckDecke, 1090);
  TEST_ASSERT_EQUAL(0, io.sceneDone);
  TEST_ASSERT_EQUAL(0, io.sceneFailed);
  // Loslassen und Wiederholung: beantworten, aber keine neue Szene
  feed(s, kReleaseW1Oben, 1200);
  feed(s, kRepeatRelease, 1300);
  s.tick(1301);
  TEST_ASSERT_EQUAL(5, io.tx.size());
  TEST_ASSERT_FALSE(io.keys[1].sceneStarted);
  TEST_ASSERT_FALSE(io.keys[2].sceneStarted);
  // gleiche seq beim Druecken nochmal (Wiederholung) -> keine zweite Szene
  feed(s, kPressW1Oben, 1400);
  TEST_ASSERT_FALSE(io.keys[3].sceneStarted);
}

// Druecken verloren (Bridge sendete gerade): Loslassen mit seq +2 zur letzten gesehenen ruft die Szene auf
void test_key_missed_press() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  s.setEnabled(true);
  // nach dem Start: einzelnes Loslassen ohne Vorgeschichte -> keine Szene
  feed(s, kReleaseW1Oben, 1000);  // seq 0313
  TEST_ASSERT_FALSE(io.keys[0].sceneStarted);
  TEST_ASSERT_TRUE(io.keys[0].answered);
  // Druecken 0314 verloren, Loslassen 0315 kommt an -> Szene
  std::string rel = kReleaseW1Oben;
  rel.replace(rel.find("00320000031308"), 14, "00320000031508");
  feed(s, rel.c_str(), 2000);
  TEST_ASSERT_TRUE(io.keys[1].sceneStarted);
  // Wiederholung dieses Loslassens -> keine zweite Szene
  feed(s, rel.c_str(), 2100);
  TEST_ASSERT_FALSE(io.keys[2].sceneStarted);
  // naechstes Paar normal: Druecken 0316 -> Szene, Loslassen 0317 -> keine
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
  TEST_ASSERT_EQUAL(1, io.tx.size());  // nur die Antwort
  TEST_ASSERT_EQUAL(1, io.keys[0].keyIndex);
  TEST_ASSERT_FALSE(io.keys[0].sceneStarted);
}

// Sender nach Werksreset: GA-Basis 3 statt 0 -> dieselbe Taste (nur unteres Byte zaehlt)
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
  s.tick(400);   // Versuch 2, nächster Timeout 550 ms
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
  s.tick(0);    // belegt
  s.tick(20);   // belegt
  TEST_ASSERT_EQUAL(0, io.tx.size());
  s.tick(40);   // gesendet, weiterhin Versuch 1
  TEST_ASSERT_EQUAL(1, io.tx.size());
  s.tick(439);
  TEST_ASSERT_EQUAL(1, io.tx.size());
  // Quittung des Aktors
  feed(s, kAckStylos, 480);
  TEST_ASSERT_EQUAL(1, io.results.size());
  TEST_ASSERT_EQUAL_STRING("#7 0004 h2 set ok rc0 t1", io.results[0].c_str());
}

// Antwort eines Aktors 0.0.5 (unter Domaene) auf A_PropertyValue_Read
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
  // Minimalhelligkeit lesen: Obj 3 PID 0x4F Start 0x10F, 1 Byte
  TEST_ASSERT_TRUE(s.queueProp(enet::JobKind::PROP_READ, 0x0005, 3, 0x4F, 0x10F, 1, nullptr, 0, 11));
  s.tick(0);
  TEST_ASSERT_EQUAL(1, io.tx.size());
  // ... 7fff 0005 | NPCI | 03d5 | 03 4f 11 0f
  TEST_ASSERT_EQUAL_STRING("034f110f", io.tx[0].substr(io.tx[0].size() - 8).c_str());
  TEST_ASSERT_EQUAL_STRING("d5", io.tx[0].substr(io.tx[0].size() - 10, 2).c_str());
  feedPropResp(s, 0x0006, 3, 0x4F, 0x10F, 1, "19", 40);  // falscher Absender: ignorieren
  feedPropResp(s, 0x0005, 3, 0x4F, 0x110, 1, "19", 45);  // falscher Start: ignorieren
  TEST_ASSERT_EQUAL(0, io.props.size());
  feedPropResp(s, 0x0005, 3, 0x4F, 0x10F, 1, "19", 50);
  TEST_ASSERT_EQUAL(1, io.props.size());
  TEST_ASSERT_EQUAL_STRING("#11 0005 3/79@271 ok rc0 t1 19", io.props[0].c_str());
  // Schreiben, Gerät lehnt ab (Anzahl 0)
  const uint8_t v[] = {0x0c};
  TEST_ASSERT_TRUE(s.queueProp(enet::JobKind::PROP_WRITE, 0x0005, 3, 0x4F, 0x10F, 1, v, 1, 12));
  s.tick(100);
  TEST_ASSERT_EQUAL_STRING("034f110f0c", io.tx[1].substr(io.tx[1].size() - 10).c_str());
  feedPropResp(s, 0x0005, 3, 0x4F, 0x10F, 0, "", 150);
  TEST_ASSERT_EQUAL_STRING("#12 0005 3/79@271 fehler rc1 t1 ", io.props[1].c_str());
  // NetworkParameter ohne Antwort: erledigt nach dem Senden
  const uint8_t np[] = {0x00, 0x12, 0x3c, 0x00};
  TEST_ASSERT_TRUE(s.queueNpWrite(0x0005, np, 4, 13));
  s.tick(200);
  TEST_ASSERT_EQUAL(3, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("#13 0005 0/0@0 ok rc0 t1 ", io.props[2].c_str());
  TEST_ASSERT_FALSE(s.busy());
}

// FuncProp-Antwort (0x2C9) eines Aktors an den Server, payload = obj pid rc ...
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
  // Link-Tabelle Decke Tuer, Kanal-Objekt 3, GO 6, Eintrag 0 lesen
  const uint8_t rd[] = {0x00, 0x00, 0x06, 0x00};
  TEST_ASSERT_TRUE(s.queueFuncProp(enet::JobKind::FP_READ, 0x0005, 3, 63, rd, 4, 21));
  s.tick(0);
  TEST_ASSERT_EQUAL_STRING("1744ff02000a12345600007fff00056102c8033f00000600", io.tx[0].c_str());
  feedFp(s, 0x0005, "003e00", 20);  // Quittung eines Schaltbefehls (Obj 0, PID 62): ignorieren
  TEST_ASSERT_EQUAL(0, io.props.size());
  feedFp(s, 0x0005, "033f0000000a65432115002d0006", 40);  // Server-SN, GA 45 -> GO 6
  TEST_ASSERT_EQUAL(1, io.props.size());
  TEST_ASSERT_EQUAL_STRING("#21 0005 3/63@0 ok rc0 t1 00000a65432115002d0006", io.props[0].c_str());
  // Ende der Tabelle: rc ff
  TEST_ASSERT_TRUE(s.queueFuncProp(enet::JobKind::FP_READ, 0x0005, 3, 63, rd, 4, 22));
  s.tick(100);
  feedFp(s, 0x0005, "033fff", 140);
  TEST_ASSERT_EQUAL(2, io.props.size());
  TEST_ASSERT_EQUAL_STRING("#22 0005 3/63@0 fehler rc255 t1 ", io.props[1].c_str());
  // Eintrag anlegen: Server-SN, GA 100 -> GO 6
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
  // Szene 7 an GA 48 (Arbeitslicht), byte-gleich zum Live-Test 2026-10-28 (LFN 0)
  const uint8_t nr[] = {0x07};
  TEST_ASSERT_TRUE(s.queueGroupWrite(48, nr, 1, 31));
  s.tick(0);
  TEST_ASSERT_EQUAL(1, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1244ff02000a65432115007fff0030e0008007", io.tx[0].c_str());
  s.tick(50);
  TEST_ASSERT_EQUAL(1, io.tx.size());  // keine Quittung, Abstand kGroupGapMs
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
  c.actions[2] = {0x0000, 45, 1, {0x06}};  // IA 0: Szenenaufruf "Alles an" (GA 45, Nr. 6)
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
  s.queueSet(0x0005, 4, &zero, 1);  // Decke Wert 0
  s.tick(0);
  feed(s, kAckDecke, 40);
  s.tick(7999);
  TEST_ASSERT_EQUAL(1, io.tx.size());
  s.tick(8040);  // keine Statusmeldung -> GO 20 lesen
  TEST_ASSERT_EQUAL(2, io.tx.size());
  TEST_ASSERT_EQUAL_STRING("1544ff02000a12345600007fff00056102c8003e0014", io.tx[1].c_str());
  feed(s, "1544ff02000a123456000000057fff6d02c9003e0000", 8080);
  s.tick(8081);  // Dimmer: GO 21 lesen
  TEST_ASSERT_EQUAL_STRING("1544ff02000a12345600007fff00056102c8003e0015", io.tx[2].c_str());
  feed(s, "1544ff02000a123456000000057fff6f02c9003e0000", 8120);
  TEST_ASSERT_EQUAL(2, io.states.size());  // jede Abfrage wird gemeldet (GO 20 und GO 21), auch unverändert
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
  feed(s, kStylosInfoEin, 1500);  // Stylos meldet sich selbst
  TEST_ASSERT_EQUAL_STRING("0004/0 ein 100", io.states[0].c_str());
  s.tick(9000);
  TEST_ASSERT_EQUAL(1, io.tx.size());  // keine Abfrage
}

void test_status_ga_base() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  feed(s, "1444ff02000a4567891700001e0316a20080e4ffff", 0);  // 0.0.30 an GA 790 = 3*256 + 22
  TEST_ASSERT_EQUAL(1, io.states.size());
  TEST_ASSERT_EQUAL_STRING("001e/0 ein 100", io.states[0].c_str());
  feed(s, "1444ff02000a4567891700001e0016a2008000ffff", 0);  // andere Basis (0): gilt trotzdem (unteres Byte)
  TEST_ASSERT_EQUAL(2, io.states.size());
  TEST_ASSERT_EQUAL_STRING("001e/0 aus 0", io.states[1].c_str());
  feed(s, "1444ff02000a4567891700001e0315a20080e4ffff", 0);  // anderes GO (GA 789) -> ignoriert
  TEST_ASSERT_EQUAL(2, io.states.size());
}

void test_key_kinds_and_lookup() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  s.setEnabled(true);
  // verknuepfte Wippe (Handsender W2 -> Stylos): melden, nicht beantworten
  feed(s, "1f44ff02000a234567160000030039a6008001000a34567812001640000724f0", 0);
  TEST_ASSERT_EQUAL(0, io.tx.size());
  TEST_ASSERT_EQUAL(1, io.keys.size());
  TEST_ASSERT_TRUE(io.keys[0].kind == enet::KeyKind::DIRECT);
  TEST_ASSERT_TRUE(io.keys[0].press);
  TEST_ASSERT_FALSE(io.keys[0].answered);
  // Loslassen nach Haltedauer
  feed(s, "1f44ff02000a234567160000030039a8008001000a34567812001640220725f0", 300);
  TEST_ASSERT_FALSE(io.keys[1].press);
  // unbelegte Wippe: nackter Tastenwert
  feed(s, "1344ff02000a23456716000003003eaa00804000", 500);
  TEST_ASSERT_TRUE(io.keys[2].kind == enet::KeyKind::VALUE);
  TEST_ASSERT_EQUAL_HEX16(0x4000, io.keys[2].cmd);
  TEST_ASSERT_TRUE(io.keys[2].press);
  TEST_ASSERT_EQUAL_HEX8(0x02, io.keys[2].rfInfo);  // Batterie in Ordnung
  // RF-Info 0x00: Batterie schwach, wird mit dem Tastendruck weitergegeben
  feed(s, "1344ff00000a23456716000003003eac00804000", 520);
  TEST_ASSERT_EQUAL(4, io.keys.size());
  TEST_ASSERT_EQUAL_HEX8(0x00, io.keys[3].rfInfo);
  // Sonnensensor (Mitschnitt 2026-11-06): Helligkeit 2 Byte an GA 0x204, Szene 50 als 1 Byte an GA 0x207
  feed(s, "1344ff00000a03a417110000090204a600803436", 530);
  TEST_ASSERT_EQUAL(5, io.keys.size());
  TEST_ASSERT_EQUAL((int)enet::KeyKind::VALUE, (int)io.keys[4].kind);
  TEST_ASSERT_EQUAL_HEX16(0x3436, io.keys[4].cmd);
  feed(s, "1244ff00000a03a417110000090207ae008072", 540);
  TEST_ASSERT_EQUAL(6, io.keys.size());
  TEST_ASSERT_EQUAL((int)enet::KeyKind::VALUE1, (int)io.keys[5].kind);
  TEST_ASSERT_EQUAL_HEX16(0x0207, io.keys[5].ga);
  TEST_ASSERT_EQUAL_HEX16(0x72, io.keys[5].cmd);
  // Kanalsuche ueber Kanalnummer
  TEST_ASSERT_EQUAL(1, s.channelIndexByCh(0x0004, 2));
  TEST_ASSERT_EQUAL(-1, s.channelIndexByCh(0x0004, 3));
  // Szene mit Auftragsnummer
  TEST_ASSERT_TRUE(s.runScene(0, 77));
  s.tick(600);
  feed(s, kAckStylos, 640);
  s.tick(641);
  feed(s, kAckDecke, 690);
  TEST_ASSERT_EQUAL(77, io.sceneId);
  TEST_ASSERT_EQUAL(0, io.results.size());  // Szenen melden keine Einzelergebnisse
}

void test_real_server_reported() {
  FakeIo io;
  enet::Server s(io);
  s.setConfig(testanlageConfig());
  s.setEnabled(true);
  feed(s, kServerInfo, 0);
  TEST_ASSERT_TRUE(s.enabled());  // nur melden, nie selbst abschalten
  TEST_ASSERT_EQUAL(1, io.realServer);
  feed(s, kServerInfo, 30000);
  TEST_ASSERT_EQUAL(1, io.realServer);  // höchstens einmal je Minute
  feed(s, kServerInfo, 60000);
  TEST_ASSERT_EQUAL(2, io.realServer);
  feed(s, kPressW1Oben, 60010);
  TEST_ASSERT_EQUAL(1, io.tx.size());  // Taste wird weiter beantwortet
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
  return UNITY_END();
}

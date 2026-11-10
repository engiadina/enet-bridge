// Host-Tests: pio test -e native
#include <string.h>
#include <unity.h>

#include "../../src/knxrf.cpp"
#include "../../src/knxrf.h"
#include "../../src/rauschen.h"

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

// Telegramme aus einem Mitschnitt (rtl_433, CRCs entfernt); Domäne und Seriennummern durch erfundene ersetzt
static const char* kFrames[] = {
    "1944ff02000a12345600007fff0000e103e4c351480405000000",              // Server-Broadcast
    "1f44ff02000a234567160000030006ae008001000a654321150032400002fe08",  // Handsender SumstatusGet
    "1444ff02000a65432115007fff0032e40080ff02fe",                        // Server SumstatusInfo
    "1644ff02000a12345600007fff00046702c7003e000201",                    // FuncPropCmd Stylos ein
    "1444ff02000a123456000000047fff6902c9003e00",                        // Antwort OK
};

void test_frame_len() {
  TEST_ASSERT_EQUAL(0, knxrf::frameLenOnAir(9));
  TEST_ASSERT_EQUAL(25, knxrf::frameLenOnAir(0x14));  // 21 Datenbytes + 2 CRCs
  TEST_ASSERT_EQUAL(27, knxrf::frameLenOnAir(0x16));  // 23 Datenbytes + 2 CRCs
  TEST_ASSERT_EQUAL(38, knxrf::frameLenOnAir(0x1F));  // 32 Datenbytes, 3 Bloecke -> 3 CRCs
  TEST_ASSERT_EQUAL(12 + 17 + 4, knxrf::frameLenOnAir(9 + 17));  // 2 Folgebloecke
}

void test_manchester_roundtrip() {
  uint8_t d[4] = {0x00, 0xFF, 0x5A, 0x14}, c[8], o[4];
  knxrf::manchesterEncode(d, 4, c);
  // 0x00 -> 1010101010101010, 0xFF -> 0101010101010101
  TEST_ASSERT_EQUAL_HEX8(0xAA, c[0]);
  TEST_ASSERT_EQUAL_HEX8(0x55, c[2]);
  TEST_ASSERT_EQUAL(4, knxrf::manchesterDecode(c, 8, o, false));
  TEST_ASSERT_EQUAL_MEMORY(d, o, 4);
  c[5] = 0xFF;  // ungueltiges Chip-Paar
  TEST_ASSERT_EQUAL(2, knxrf::manchesterDecode(c, 8, o, false));
}

void test_real_frames_roundtrip() {
  for (auto hex : kFrames) {
    uint8_t data[64], raw[128], chips[256], dec[128];
    size_t n = fromHex(hex, data);
    TEST_ASSERT_EQUAL(n, (size_t)data[0] + 1);
    size_t rawLen = knxrf::addCrc(data, n, raw);
    TEST_ASSERT_EQUAL(knxrf::frameLenOnAir(data[0]), rawLen);
    size_t cl = knxrf::manchesterEncode(raw, rawLen, chips);
    size_t dl = knxrf::manchesterDecode(chips, cl, dec, false);
    TEST_ASSERT_EQUAL(rawLen, dl);
    knxrf::Frame f;
    TEST_ASSERT_TRUE(knxrf::stripCrc(dec, dl, f));
    TEST_ASSERT_TRUE(f.crcOk);
    TEST_ASSERT_EQUAL(n, f.len);
    TEST_ASSERT_EQUAL_MEMORY(data, f.data, n);
    dec[dl - 1] ^= 0x01;  // CRC kaputt
    TEST_ASSERT_TRUE(knxrf::stripCrc(dec, dl, f));
    TEST_ASSERT_FALSE(f.crcOk);
  }
}

static const uint8_t kDomain[6] = {0x00, 0x0A, 0x12, 0x34, 0x56, 0x00};
static const uint8_t kServerSN[6] = {0x00, 0x0A, 0x65, 0x43, 0x21, 0x15};

static void assertFrame(const char* hex, const uint8_t* got, size_t gotLen) {
  uint8_t want[64];
  size_t n = fromHex(hex, want);
  TEST_ASSERT_EQUAL(n, gotLen);
  TEST_ASSERT_EQUAL_HEX8_ARRAY(want, got, n);
}

void test_parse_build_roundtrip() {
  for (auto hex : kFrames) {
    uint8_t data[64], out[64];
    size_t n = fromHex(hex, data);
    knxrf::Telegram t;
    TEST_ASSERT_TRUE(knxrf::parseTelegram(data, n, t));
    TEST_ASSERT_EQUAL(n, knxrf::buildTelegram(t, out));
    TEST_ASSERT_EQUAL_HEX8_ARRAY(data, out, n);
  }
}

void test_parse_fields() {
  uint8_t d[64];
  knxrf::Telegram t;
  // FuncPropCmd Server -> Stylos
  TEST_ASSERT_TRUE(knxrf::parseTelegram(d, fromHex(kFrames[3], d), t));
  TEST_ASSERT_TRUE(t.doa);
  TEST_ASSERT_FALSE(t.group);
  TEST_ASSERT_EQUAL_HEX16(0x7FFF, t.src);
  TEST_ASSERT_EQUAL_HEX16(0x0004, t.dst);
  TEST_ASSERT_EQUAL(6, t.hops);
  TEST_ASSERT_EQUAL(3, t.lfn);
  TEST_ASSERT_EQUAL_HEX16(knxrf::APCI_FUNC_PROP_CMD, t.apciBase());
  TEST_ASSERT_EQUAL_HEX8_ARRAY(kDomain, t.addr, 6);

  // Handsender SumstatusGet
  TEST_ASSERT_TRUE(knxrf::parseTelegram(d, fromHex(kFrames[1], d), t));
  TEST_ASSERT_TRUE(t.group);
  TEST_ASSERT_FALSE(t.doa);
  TEST_ASSERT_EQUAL(2, t.hops);
  TEST_ASSERT_EQUAL(7, t.lfn);
  TEST_ASSERT_EQUAL_HEX16(0x0006, t.dst);
  knxrf::SumstatusGet s;
  TEST_ASSERT_TRUE(knxrf::parseSumstatusGet(t, s));
  TEST_ASSERT_EQUAL_HEX8_ARRAY(kServerSN, s.replySN, 6);
  TEST_ASSERT_EQUAL(50, s.replyGA);
  TEST_ASSERT_EQUAL_HEX16(0x4000, s.cmd);
  TEST_ASSERT_EQUAL_HEX16(0x02FE, s.seq);
  TEST_ASSERT_EQUAL_HEX8(0x08, s.cc);

  // Antwort des Aktors
  TEST_ASSERT_TRUE(knxrf::parseTelegram(d, fromHex(kFrames[4], d), t));
  knxrf::FuncPropResp r;
  TEST_ASSERT_TRUE(knxrf::parseFuncPropResp(t, r));
  TEST_ASSERT_EQUAL(62, r.pid);
  TEST_ASSERT_EQUAL(0, r.rc);
  TEST_ASSERT_EQUAL(0, r.valueLen);
  TEST_ASSERT_FALSE(knxrf::parseSumstatusGet(t, s));

  d[0] = 0x30;  // L passt nicht zur Laenge
  TEST_ASSERT_FALSE(knxrf::parseTelegram(d, 21, t));
}

void test_build_server_frames() {
  uint8_t out[64];
  const uint8_t on = 0x01;
  assertFrame(kFrames[3], out, knxrf::buildFuncPropCmd(kDomain, 0x0004, 2, &on, 1, 3, out));
  assertFrame(kFrames[2], out, knxrf::buildSumstatusInfo(kServerSN, 50, 0xFF, 0x02FE, 2, out));
  assertFrame(kFrames[0], out, knxrf::buildServerBroadcast(kDomain, 0, out));

  size_t n = knxrf::buildFuncPropStateRead(kDomain, 0x0004, 28, 5, out);
  knxrf::Telegram t;
  TEST_ASSERT_TRUE(knxrf::parseTelegram(out, n, t));
  TEST_ASSERT_EQUAL_HEX16(knxrf::APCI_FUNC_PROP_STATE_READ, t.apci);
  TEST_ASSERT_EQUAL(4, t.dataLen);
  TEST_ASSERT_EQUAL(28, t.data[3]);
  TEST_ASSERT_EQUAL(5, t.lfn);

  TEST_ASSERT_EQUAL_HEX8(0x80 | 100, knxrf::sumstatus(true, 150));
  TEST_ASSERT_EQUAL_HEX8(0x32, knxrf::sumstatus(false, 50));
}

void test_tx_chips() {
  uint8_t data[64], chips[256], dec[128];
  size_t n = fromHex(kFrames[1], data);  // 3 Bloecke
  size_t c = knxrf::buildTxChips(data, n, chips, sizeof(chips), false);
  TEST_ASSERT_EQUAL(knxrf::kTxPreambleBytes + 3 + 2 * knxrf::frameLenOnAir(data[0]), c);
  for (size_t i = 0; i < knxrf::kTxPreambleBytes; ++i) TEST_ASSERT_EQUAL_HEX8(0x55, chips[i]);
  TEST_ASSERT_EQUAL_HEX8(0x54, chips[knxrf::kTxPreambleBytes]);
  TEST_ASSERT_EQUAL_HEX8(0x76, chips[knxrf::kTxPreambleBytes + 1]);
  TEST_ASSERT_EQUAL_HEX8(0x96, chips[knxrf::kTxPreambleBytes + 2]);

  // Ab dem Sync so dekodieren wie der Empfangspfad
  const uint8_t* body = chips + knxrf::kTxPreambleBytes + 3;
  size_t bodyLen = c - knxrf::kTxPreambleBytes - 3;
  size_t dl = knxrf::manchesterDecode(body, bodyLen, dec, false);
  TEST_ASSERT_EQUAL(bodyLen / 2, dl);
  knxrf::Frame f;
  TEST_ASSERT_TRUE(knxrf::stripCrc(dec, dl, f));
  TEST_ASSERT_TRUE(f.crcOk);
  TEST_ASSERT_EQUAL_HEX8_ARRAY(data, f.data, n);

  // invertiert: nur der Datenteil
  c = knxrf::buildTxChips(data, n, chips, sizeof(chips), true);
  TEST_ASSERT_EQUAL_HEX8(0x96, chips[knxrf::kTxPreambleBytes + 2]);
  TEST_ASSERT_EQUAL(bodyLen / 2, knxrf::manchesterDecode(body, bodyLen, dec, true));

  TEST_ASSERT_EQUAL(0, knxrf::buildTxChips(data, n, chips, 40, false));      // Puffer zu klein
  TEST_ASSERT_EQUAL(0, knxrf::buildTxChips(data, n - 1, chips, 256, false));  // L passt nicht
}

// Gestoerte Quittungen von 0.0.7 (Mitschnitt 2026-10-27, Domäne ersetzt, Fehlerbild unverändert): 1–2 ungueltige Chip-Paare
void test_decode_corrects_erasures() {
  static const char* kGestoert[] = {
      "a99a9a9a5555aaa6baaaaa66a9a6a59a9996aaaa99965956aaaaaaaaaa95955555559669aaa65a69aaaaa556aaaa599a4955",
      "a99a9a9a5555aaa6aaaaaa66ab26a59a9996aaaa99965956aaaaaaaaaa959555555596a9aaa65a69aaaaa556aaaa99956aa9",
  };
  static const int kStellen[] = {2, 2};
  for (int i = 0; i < 2; ++i) {
    uint8_t chips[64], raw[64];
    size_t n = fromHex(kGestoert[i], chips);
    knxrf::Frame f;
    TEST_ASSERT_EQUAL(kStellen[i], knxrf::decodeFrame(chips, n, false, raw, f));
    TEST_ASSERT_TRUE(f.crcOk);
    TEST_ASSERT_EQUAL(21, f.len);
    TEST_ASSERT_EQUAL_HEX8(0x07, f.data[12]);  // Absender 0.0.7
    TEST_ASSERT_EQUAL_HEX8(0xC9, f.data[17]);  // FuncProp-Antwort
  }
  // fehlerfrei: 0 Korrekturen; zu viele Ausloeschungen: -1
  uint8_t chips[64], raw[64];
  size_t n = fromHex(kGestoert[0], chips);
  knxrf::Frame f;
  TEST_ASSERT_EQUAL(2, knxrf::decodeFrame(chips, n, false, raw, f));
  uint8_t sauber[64];
  knxrf::manchesterEncode(raw, n / 2, sauber);  // raw ist jetzt korrigiert (mit CRCs)
  TEST_ASSERT_EQUAL(0, knxrf::decodeFrame(sauber, n, false, raw, f));
  chips[5] = chips[7] = chips[9] = chips[11] = 0xff;
  TEST_ASSERT_EQUAL(-1, knxrf::decodeFrame(chips, n, false, raw, f));
}

// Rauschen: Grundrauschen folgt dem unteren Pegel, Telegramme heben es kaum, belegt erst 10 dB darüber
static void test_rauschen() {
  Rauschen r;
  TEST_ASSERT_FALSE(r.belegt(-50));                      // ohne Proben nie belegt
  uint32_t z = 1;
  auto zufall = [&z]() { z = z * 1103515245u + 12345u; return (int)((z >> 16) % 9) - 4; };  // ±4 dB
  for (int i = 0; i < 20000; ++i) r.probe(-104.0f + zufall());
  TEST_ASSERT_FLOAT_WITHIN(3.0f, -105.5f, r.boden());    // unteres Quantil der Streuung
  TEST_ASSERT_FALSE(r.belegt(-100));
  TEST_ASSERT_TRUE(r.belegt(-90));
  TEST_ASSERT_TRUE(r.anteil() < 0.01f);
  const float vorher = r.boden();
  for (int i = 0; i < 20; ++i) TEST_ASSERT_TRUE(r.probe(-55));  // ein Telegramm (~100 ms bei 5 ms je Probe)
  TEST_ASSERT_FLOAT_WITHIN(0.5f, vorher, r.boden());
  for (int i = 0; i < 20000; ++i) r.probe(-85.0f + zufall());   // dauerhafter Störer
  TEST_ASSERT_FALSE(r.belegt(-85));                     // sperrt nach einiger Zeit nicht mehr
  TEST_ASSERT_TRUE(r.belegt(-60));
  for (int i = 0; i < 2000; ++i) r.probe(-104.0f + zufall());   // Störer weg: schnell wieder unten
  TEST_ASSERT_TRUE(r.boden() < -100.0f);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_frame_len);
  RUN_TEST(test_manchester_roundtrip);
  RUN_TEST(test_decode_corrects_erasures);
  RUN_TEST(test_real_frames_roundtrip);
  RUN_TEST(test_parse_build_roundtrip);
  RUN_TEST(test_parse_fields);
  RUN_TEST(test_build_server_frames);
  RUN_TEST(test_tx_chips);
  RUN_TEST(test_rauschen);
  return UNITY_END();
}

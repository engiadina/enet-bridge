#pragma once
// eNet-Server-Funktionen im ESP (plattformunabhaengig, auf dem Host testbar):
//   - Server-Tasten beantworten (SumstatusInfo, sofort) und als Ereignis melden
//   - Befehle/Abfragen nacheinander mit Quittung und Wiederholung (wie der Server)
//   - Status-Polling 8 s nach jedem Befehl, wenn sich der Aktor nicht selbst meldet
//   - Zustandstabelle aller Kanaele
//   - lokale Szenen (pro Taste: Szene ausfuehren oder nur melden)
//   - schaltet sich ab, sobald ein Telegramm des echten Servers empfangen wird
// Zeit kommt als Parameter (ms), Senden und Ereignisse laufen ueber die Schnittstelle Io.

#include <stddef.h>
#include <stdint.h>

#include "knxrf.h"

namespace enet {

constexpr size_t kMaxChannels = 48, kMaxActions = 128, kMaxScenes = 32, kMaxKeys = 32, kQueueLen = 64;
// Quittungs-Timeout wächst je Versuch um kAckStepMs (400, 550, 700 … ms), damit kurze Störungen nicht alle
// Versuche treffen (der eNet Server versucht nur 3× im 400-ms-Raster)
constexpr uint32_t kAckTimeoutMs = 400, kAckStepMs = 150, kPollDelayMs = 8000;
constexpr uint8_t kTries = 5;
constexpr uint32_t kRealServerReportMs = 60000;  // echten Server höchstens einmal je Minute melden
// Kanal belegt (LBT): Senden nach kBusyRetryMs erneut, ohne einen Versuch zu verbrauchen, höchstens kBusyRetries×
constexpr uint32_t kBusyRetryMs = 20;
constexpr uint8_t kBusyRetries = 20;
// Gruppentelegramme (Szenenaufruf) werden nicht quittiert: kGroupRepeats× im Abstand kGroupGapMs senden
constexpr uint8_t kGroupRepeats = 3;
constexpr uint32_t kGroupGapMs = 80;
constexpr uint16_t kGoStatus = 20, kGoStatusValue = 21, kGoSumstatusInfo = 22;
// Jalousie CT_1F03 (Katalog, ungetestet): GO2 Auf/Ab (1.008), GO3 Stopp/Schritt (1.007), GO4 Stopp (1.017),
// GO5 Position (5.001), GO6 Lamelle (5.001), GO10 Fahrinfo (2 Bit: 0 steht oben, 1 steht unten, 2 fährt auf,
// 3 fährt ab), GO11 SumstatusInfo, GO12 Position aktuell, GO13 Lamelle aktuell. Position 0 % = oben.
constexpr uint16_t kGoBlindMove = 2, kGoBlindStep = 3, kGoBlindStop = 4, kGoBlindPos = 5, kGoBlindSlat = 6,
                   kGoBlindInfo = 10, kGoBlindSumstatus = 11, kGoBlindPosNow = 12, kGoBlindSlatNow = 13;
constexpr uint8_t kSpanLight = 26, kSpanBlind = 19;  // Handles je Kanal (Offset .. Offset + Span - 1)
// Jalousie: Fahrten dauern länger als ein Licht – nach einem Befehl mehrfach abfragen (ms nach dem Befehl)
constexpr uint32_t kBlindPollMs[] = {8000, 25000, 60000, 120000};
constexpr uint8_t kBlindPolls = sizeof(kBlindPollMs) / sizeof(kBlindPollMs[0]);

struct Channel {
  uint16_t ia;
  uint8_t sn[6];
  uint8_t offset;  // Kanaloffset (Handle = Offset + GO)
  bool dimmer;
  uint8_t gaBase;  // eigene GAs = gaBase * 256 + GO (wechselt bei jedem Werksreset)
  uint8_t ch;      // Kanalnummer wie im Export (1, 2, ...) – Adressierung durch den Host
  uint8_t obj;     // Kanal-Objekt (Link-Tabelle, Parameter)
  bool blind;      // Jalousie (Flags Bit 1) – am Ende, damit bestehende Initialisierungen gelten
};
struct Action {
  uint16_t ia, handle;  // ia = 0: Gruppentelegramm unter Server-SN an GA = handle (Szenenaufruf)
  uint8_t len;
  uint8_t value[4];
};
struct Scene {
  uint8_t first, count;  // Bereich in Config::actions
};
struct Key {
  uint8_t sn[6];  // Sender
  uint16_t ga;    // GA der Server-Taste (verglichen wird nur das untere Byte = Offset + 6)
  int8_t scene;   // < 0: nur melden
};

struct Config {
  uint8_t domain[6] = {};
  uint8_t serverSn[6] = {};
  uint16_t serverIa = knxrf::kServerIA;
  Channel channels[kMaxChannels];
  Action actions[kMaxActions];
  Scene scenes[kMaxScenes];
  Key keys[kMaxKeys];
  uint8_t nChannels = 0, nActions = 0, nScenes = 0, nKeys = 0;
};

// Binaerformat Version 3 (erzeugt von tools/server_cfg.py), alle Zahlen Big Endian:
//   'E' 0x03 | Domaene(6) | Server-SN(6) | Server-IA(2) | nCh nAct nScenes nKeys (je 1)
//   Kanaele: IA(2) SN(6) Kanalnr(1) Offset(1) Objekt(1) Flags(1, Bit0 Dimmer, Bit1 Jalousie) GA-Basis(1)
//   Aktionen: IA(2) Handle(2) Laenge(1) Wert(Laenge, max 4); IA 0 = Gruppentelegramm an GA = Handle
//   Szenen: erste Aktion(1) Anzahl(1)
//   Tasten: SN(6) GA(2) Szene(1, signed)
//   CRC16 (knxrf::crc16 ueber alles davor)
bool parseConfig(const uint8_t* blob, size_t len, Config& c);
size_t serializeConfig(const Config& c, uint8_t* out, size_t cap);

struct ChannelState {
  bool known = false, on = false;
  uint8_t percent = 0;  // Jalousie: Position (0 % = oben)
  uint8_t slat = 0;     // Jalousie: Lamelle
  uint8_t move = 0xFF;  // Jalousie: Fahrinfo GO10 (0xFF unbekannt)
  bool pollPending = false;
  uint32_t pollAt = 0, cmdAt = 0;
  uint8_t pollStage = 0;  // Jalousie: nächste Abfrage aus kBlindPollMs
};

enum class Source : uint8_t { STATUS_MSG, POLL, CACHE };
// GO_*: Gruppenobjekt per FuncProp (Schalten, Status); PROP_*: A_PropertyValue (Parameter, Lade-Status …);
// NP_WRITE: A_NetworkParameter_Write P2P ohne Antwort (gilt nach dem Senden als erledigt);
// FP_CMD/FP_READ: FuncProp auf beliebiges Objekt/PID (Link-Tabelle); GROUP_WRITE: unter Server-SN an eine GA
// (Szenenaufruf), ohne Quittung, kGroupRepeats× gesendet
enum class JobKind : uint8_t { GO_SET, GO_READ, PROP_READ, PROP_WRITE, NP_WRITE, FP_CMD, FP_READ, GROUP_WRITE };
constexpr size_t kMaxJobData = 16;
enum class Origin : uint8_t { HOST, SCENE, POLL };

// SERVER: SumstatusGet an die Server-SN (Server-Taste); DIRECT: SumstatusGet an einen Aktor (verknuepfte
// Taste/Wippe); VALUE: nackter Tastenwert einer unbelegten Wippe (2 Byte), auch Helligkeit des Sonnensensors
// (DPT 9); VALUE1: 1-Byte-Gruppenwert unter Sender-SN (Sonnensensor: Szenennummer in Bit 5-0, ab 0.8.9).
// VALUE4: 4-Byte-Gruppenwert unter Sender-SN (Energiesensor: Leistung DPT 14, Energie DPT 13; ab 0.8.21).
enum class KeyKind : uint8_t { SERVER, DIRECT, VALUE, VALUE1, VALUE4 };

struct KeyEvent {
  const uint8_t* senderSn;
  uint16_t ga;
  KeyKind kind;
  uint16_t cmd;            // Tastenwert (bei VALUE die 2 Datenbytes, bei VALUE1 das Datenbyte)
  knxrf::SumstatusGet sg;  // nur SERVER/DIRECT
  int keyIndex;            // -1: nicht konfiguriert (nur SERVER)
  bool press, answered, sceneStarted;
  uint8_t rfInfo;          // RF-Info des Telegramms (Bit 1: Batterie in Ordnung)
  uint32_t value32;        // nur VALUE4
};

class Io {
 public:
  virtual ~Io() {}
  // Rahmen ohne CRCs; LFN setzt der Aufrufer der Funkstrecke.
  virtual bool transmit(const uint8_t* frame, size_t len) = 0;
  virtual void onKey(const KeyEvent& e) = 0;
  virtual void onState(const Channel& ch, const ChannelState& st, Source src) = 0;
  // id: Auftragsnummer des Hosts (0 = intern), rc: Rueckgabecode des Geraets (0 OK, -1 keine Quittung)
  virtual void onResult(uint32_t id, uint16_t ia, uint16_t handle, bool isRead, Origin o, bool ok, int rc,
                        uint8_t tries, const uint8_t* val, size_t len) = 0;
  // Ergebnis von PROP_*/NP_WRITE/FP_*/GROUP_WRITE (rc: 0 OK, -1 keine Antwort, sonst Code des Geräts;
  // PROP: 1 = lehnt ab). GROUP_WRITE: ia = 0, start = GA.
  virtual void onPropResult(uint32_t id, uint16_t ia, JobKind kind, uint8_t obj, uint8_t pid, uint16_t start,
                            bool ok, int rc, uint8_t tries, const uint8_t* val, size_t len) {}
  virtual void onSceneDone(uint32_t id, uint8_t scene, uint8_t failed) = 0;
  virtual void onRealServer() = 0;
};

class Server {
 public:
  explicit Server(Io& io) : io_(io) {}

  void setConfig(const Config& c);
  const Config& config() const { return cfg_; }
  void setEnabled(bool on) { enabled_ = on; }
  bool enabled() const { return enabled_; }

  void onFrame(const knxrf::Telegram& t, uint32_t now);
  void tick(uint32_t now);

  bool queueSet(uint16_t ia, uint16_t handle, const uint8_t* v, uint8_t len, Origin o = Origin::HOST,
                uint32_t id = 0);
  bool queueRead(uint16_t ia, uint16_t handle, Origin o = Origin::HOST, uint32_t id = 0);
  // Property lesen (data = nullptr) oder schreiben; count = Anzahl Elemente (1..15)
  bool queueProp(JobKind kind, uint16_t ia, uint8_t obj, uint8_t pid, uint16_t start, uint8_t count,
                 const uint8_t* data, uint8_t len, uint32_t id);
  bool queueNpWrite(uint16_t ia, const uint8_t* data, uint8_t len, uint32_t id);
  // FuncProp an Objekt/PID mit Rohdaten (kind = FP_CMD oder FP_READ)
  bool queueFuncProp(JobKind kind, uint16_t ia, uint8_t obj, uint8_t pid, const uint8_t* data, uint8_t len,
                     uint32_t id);
  bool queueGroupWrite(uint16_t ga, const uint8_t* data, uint8_t len, uint32_t id);
  bool runScene(uint8_t idx, uint32_t id = 0);
  size_t queueLength() const { return qCount_ + (busy_ ? 1 : 0); }

  const ChannelState& state(size_t i) const { return st_[i]; }
  int channelIndex(uint16_t ia, uint16_t handle) const;  // -1: unbekannt
  int channelIndexByCh(uint16_t ia, uint8_t ch) const;   // -1: unbekannt
  bool busy() const { return busy_; }

 private:
  struct Job {
    JobKind kind;
    uint16_t ia, handle;
    uint8_t obj, pid, count;  // PROP_*
    uint16_t start;
    uint8_t len;
    uint8_t value[kMaxJobData];
    bool read;  // GO_READ
    Origin origin;
    uint32_t id;
    int8_t scene;  // >= 0: gehoert zu dieser Szene
    bool lastOfScene;
  };
  bool push(const Job& j);
  void startJob(uint32_t now);
  void sendJob(uint32_t now);
  void finishJob(bool ok, int rc, const uint8_t* val, size_t len, uint32_t now);
  void sceneStep(bool ok);
  void updateState(int ci, bool setOn, bool on, bool setPercent, uint8_t percent, Source src);
  void updateBlind(int ci, int percent, int slat, int move, Source src);  // -1: unverändert
  void schedulePoll(int ci, uint32_t now);

  Io& io_;
  Config cfg_;
  ChannelState st_[kMaxChannels];
  uint16_t lastSeq_[kMaxKeys] = {};
  bool lastSeqValid_[kMaxKeys] = {};
  uint16_t lastAnySeq_[kMaxKeys] = {};  // letzte seq der Taste ueberhaupt (Druecken oder Loslassen)
  bool lastAnyValid_[kMaxKeys] = {};
  uint8_t sceneFailed_[kMaxScenes] = {};
  bool enabled_ = false;
  bool realServerSeen_ = false;
  uint32_t realServerAt_ = 0;  // letzte Meldung „echter Server“

  Job q_[kQueueLen];
  size_t qHead_ = 0, qCount_ = 0;
  bool busy_ = false;
  Job cur_{};
  uint8_t tries_ = 0, busyRetries_ = 0;
  bool txPending_ = false;  // letzter Sendeversuch wegen belegtem Kanal nicht gesendet
  uint32_t deadline_ = 0;
};

}  // namespace enet

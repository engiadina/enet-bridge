#pragma once
// KNX RF 1.1 Rahmen-Dekodierung (plattformunabhaengig, auf dem Host testbar).
//
// Funkschicht (= Wireless M-Bus Mode S):
//   868,3 MHz, 2-FSK, 32,768 kchip/s, Manchester (Chip-Paar 01 = 1, 10 = 0),
//   Sync-Wort auf Chip-Ebene 0x7696.
// Rahmen (nach Manchester-Dekodierung):
//   Block 1: L, C(0x44), 0xFF, RF-Info, 6 Byte SN/Domaene, CRC16
//   Block 2..n: je bis zu 16 Datenbytes + CRC16
//   CRC: Polynom 0x3D65, Init 0, Ergebnis invertiert, Big Endian.

#include <stddef.h>
#include <stdint.h>

namespace knxrf {

constexpr size_t kMaxFrame = 128;  // dekodierte Bytes inkl. CRC

// Anzahl der Bytes auf der Luft (inkl. aller CRCs) fuer ein gegebenes L-Feld.
// Liefert 0, wenn L ungueltig ist.
size_t frameLenOnAir(uint8_t L);

uint16_t crc16(const uint8_t* data, size_t len);

// Dekodiert Manchester-Chips (je Byte 2 Chip-Bytes) in Datenbytes.
// Liefert die Anzahl dekodierter Bytes; bricht bei ungueltigem Chip-Paar ab.
size_t manchesterDecode(const uint8_t* chips, size_t chipBytes, uint8_t* out, bool invert);

// Kodiert Datenbytes in Manchester-Chips (fuer Tests und spaeter zum Senden).
size_t manchesterEncode(const uint8_t* data, size_t len, uint8_t* chips);

struct Frame {
  uint8_t data[kMaxFrame];  // ohne CRCs (wie rtl_433 "data")
  size_t len = 0;
  bool crcOk = false;
  int badBlock = -1;        // erster Block mit CRC-Fehler
};

// Prueft CRCs aller Bloecke und entfernt sie. raw = Bytes auf der Luft.
bool stripCrc(const uint8_t* raw, size_t rawLen, Frame& out);

// Telegramm aus Chips dekodieren mit Korrektur: Ungueltige Chip-Paare (00/11) sind Ausloeschungen an bekannter
// Stelle. Bis kMaxErasures davon werden durchprobiert (2^k Varianten), angenommen wird nur eine Variante, bei der
// alle Block-CRCs stimmen. Rueckgabe: Anzahl korrigierter Stellen (>= 0), -1 = nicht dekodierbar, -2 = CRC falsch.
constexpr int kMaxErasures = 3;
int decodeFrame(const uint8_t* chips, size_t chipBytes, bool invert, uint8_t* raw, Frame& out);

// Fuegt CRCs hinzu (fuer Tests und spaeter zum Senden).
size_t addCrc(const uint8_t* data, size_t len, uint8_t* raw);

// ---------------------------------------------------------------------------
// Senden: kompletter Chip-Strom fuer den CC1101 (ohne dessen Praeambel/Sync).
//   Praeambel n x (01), Sync 00 0111011010010110 (wM-Bus Mode S, 18 Chips),
//   danach Manchester(Rahmen mit CRCs). Als Bytes: preambleBytes x 0x55, 0x54,
//   0x76, 0x96 (= 4 * preambleBytes + 3 Chip-Paare Praeambel).
// data = Rahmen ohne CRCs. Liefert die Laenge in Bytes, 0 wenn cap zu klein.
// ---------------------------------------------------------------------------
constexpr uint8_t kTxPreambleBytes = 9;  // 39 Chip-Paare (Mode S verlangt >= 15)
size_t buildTxChips(const uint8_t* data, size_t len, uint8_t* chips, size_t cap, bool invert,
                    uint8_t preambleBytes = kTxPreambleBytes);

// ---------------------------------------------------------------------------
// Telegramm (Rahmen ohne CRCs):
//   [0] L  [1] C=0x44  [2] 0xFF  [3] RF-Info  [4..9] SN/Domaene
//   [10] Ctrl  [11,12] Src  [13,14] Dst  [15] L/NPCI  [16] TPCI|APCI-hi  [17] APCI-lo  [18..] Daten
// L/NPCI: Bit7 Gruppe, Bits6-4 Hops, Bits3-1 LFN, Bit0 Domaene (1) / SN (0).
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
  uint8_t addr[6] = {};  // Seriennummer des Absenders oder Domaenenadresse
  bool doa = false;      // addr ist Domaene
  bool group = false;    // dst ist Gruppenadresse
  uint8_t hops = 6;
  uint8_t lfn = 0;
  uint8_t ctrl = 0x00;
  uint16_t src = 0, dst = 0;
  uint8_t tpci = 0;      // obere 6 Bit von Byte 16
  uint16_t apci = 0;     // 10 Bit, bei Gruppen-APCIs inkl. Kurzwert in Bits 5-0
  uint8_t data[kMaxData];
  size_t dataLen = 0;

  // APCI ohne Kurzwert (Gruppen-APCIs), sonst unveraendert
  uint16_t apciBase() const {
    uint16_t b = apci & 0x3C0;
    return (b == APCI_GROUP_READ || b == APCI_GROUP_RESPONSE || b == APCI_GROUP_WRITE) ? b : apci;
  }
};

bool parseTelegram(const uint8_t* d, size_t len, Telegram& t);
// Liefert die Rahmenlaenge (ohne CRCs), 0 bei zu vielen Daten.
size_t buildTelegram(const Telegram& t, uint8_t* out);

// --- Server-Telegramme (Absender 7.15.255) --------------------------------
// FuncProp allgemein (apci = APCI_FUNC_PROP_CMD oder _STATE_READ): Objekt, PID, Daten. Link-Tabelle eines
// Kanals = Kanal-Objekt PID 63: lesen `00 00 GO idx`, Eintrag anlegen `00 00 SN GA GO`, löschen `02 00 SN GA GO`.
size_t buildFuncProp(const uint8_t domain[6], uint16_t dstIA, uint16_t apci, uint8_t obj, uint8_t pid,
                     const uint8_t* data, size_t len, uint8_t lfn, uint8_t* out);
// Gruppentelegramm unter Server-SN, z. B. Szenenaufruf: GA der Szene, 1 Byte Nr. (Bit 7 = aktuellen Zustand lernen)
size_t buildServerGroupWrite(const uint8_t serverSN[6], uint16_t ga, const uint8_t* data, size_t len, uint8_t lfn,
                             uint8_t* out);
// Aktor schalten: obj 0, PID 62, GO-Handle, Wert (DPT-kodiert).
size_t buildFuncPropCmd(const uint8_t domain[6], uint16_t dstIA, uint16_t handle, const uint8_t* value,
                        size_t valueLen, uint8_t lfn, uint8_t* out);
// Status abfragen: obj 0, PID 62, GO-Handle.
size_t buildFuncPropStateRead(const uint8_t domain[6], uint16_t dstIA, uint16_t handle, uint8_t lfn,
                              uint8_t* out);
// Property lesen/schreiben (A_PropertyValue_Read/Write, P2P unter Domaene): Objekt, PID, Anzahl (1..15) und
// Startelement (12 Bit). Parameterspeicher = Kanal-Objekt PID 0x4F, Element = Byte.
size_t buildPropRead(const uint8_t domain[6], uint16_t dstIA, uint8_t obj, uint8_t pid, uint16_t start,
                     uint8_t count, uint8_t lfn, uint8_t* out);
size_t buildPropWrite(const uint8_t domain[6], uint16_t dstIA, uint8_t obj, uint8_t pid, uint16_t start,
                      uint8_t count, const uint8_t* data, size_t len, uint8_t lfn, uint8_t* out);
// A_NetworkParameter_Write P2P unter Domaene, Daten roh (Objekttyp, PID, Wert), z. B. 00 12 3c 00 nach Parametern.
size_t buildNetParamWriteP2P(const uint8_t domain[6], uint16_t dstIA, const uint8_t* data, size_t len,
                             uint8_t lfn, uint8_t* out);
// Antwort auf SumstatusGet eines Tasters (unter Server-SN, an Antwort-GA).
size_t buildSumstatusInfo(const uint8_t serverSN[6], uint16_t ga, uint8_t status, uint16_t seq, uint8_t lfn,
                          uint8_t* out);
// Broadcast alle ~5 s an GA 0/0/0: Objekt 50001 PID 72 = 04 05 00 00 00 (Bedeutung offen).
size_t buildServerBroadcast(const uint8_t domain[6], uint8_t lfn, uint8_t* out);

inline uint8_t sumstatus(bool on, uint8_t percent) {
  return (uint8_t)((on ? 0x80 : 0x00) | (percent > 100 ? 100 : percent));
}

// --- Taster -----------------------------------------------------------------
// SumstatusGet (GroupWrite, 14 Byte): req, Antwort-SN, Antwort-GA, cmd, seq, CC.
struct SumstatusGet {
  uint8_t req;
  uint8_t replySN[6];
  uint16_t replyGA;
  uint16_t cmd;  // Tastenwert (GBR): 0x40xx oben/ein, 0x80xx unten/aus, untere 14 Bit Haltedauer
  uint16_t seq;
  uint8_t cc;    // 0x08 Server/App-Taste, 0xF0 direkt verknuepft
};
bool parseSumstatusGet(const Telegram& t, SumstatusGet& s);

// FunctionProperty-Antwort (0x2C9): obj, PID, RC, Wert.
struct FuncPropResp {
  uint8_t obj, pid, rc;
  const uint8_t* value;
  size_t valueLen;
};
bool parseFuncPropResp(const Telegram& t, FuncPropResp& r);

// A_PropertyValue_Response: Anzahl 0 = Fehler (Zugriff verweigert, ungueltig)
struct PropResp {
  uint8_t obj, pid, count;
  uint16_t start;
  const uint8_t* value;
  size_t valueLen;
};
bool parsePropResp(const Telegram& t, PropResp& r);

}  // namespace knxrf

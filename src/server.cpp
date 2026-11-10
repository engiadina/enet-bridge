#include "server.h"

#include <string.h>

namespace enet {

namespace {

// (int32_t) (a - b) >= 0 also across the overflow of millis()
bool reached(uint32_t now, uint32_t at) { return (int32_t)(now - at) >= 0; }

struct Reader {
  Reader(const uint8_t* p_, size_t len_, size_t pos_) : p(p_), len(len_), pos(pos_) {}
  const uint8_t* p;
  size_t len, pos;
  bool ok = true;
  uint8_t u8() {
    if (pos + 1 > len) { ok = false; return 0; }
    return p[pos++];
  }
  uint16_t u16() { uint16_t hi = u8(); return (uint16_t)(hi << 8 | u8()); }
  void bytes(uint8_t* out, size_t n) {
    if (pos + n > len) { ok = false; memset(out, 0, n); return; }
    memcpy(out, p + pos, n);
    pos += n;
  }
};

struct Writer {
  Writer(uint8_t* p_, size_t cap_) : p(p_), cap(cap_) {}
  uint8_t* p;
  size_t cap, pos = 0;
  bool ok = true;
  void u8(uint8_t v) {
    if (pos + 1 > cap) { ok = false; return; }
    p[pos++] = v;
  }
  void u16(uint16_t v) { u8((uint8_t)(v >> 8)); u8((uint8_t)v); }
  void bytes(const uint8_t* b, size_t n) { for (size_t i = 0; i < n; ++i) u8(b[i]); }
};

}  // namespace

// Minimum length 22: header (2) + domain/SN (12) + IA (2) + counts (4) + CRC (2) – empty installation is valid
bool parseConfig(const uint8_t* blob, size_t len, Config& c) {
  if (len < 22 || blob[0] != 'E' || blob[1] != 0x03) return false;
  uint16_t crc = (uint16_t)(blob[len - 2] << 8 | blob[len - 1]);
  if (knxrf::crc16(blob, len - 2) != crc) return false;
  Reader r(blob, len - 2, 2);
  Config n;
  r.bytes(n.domain, 6);
  r.bytes(n.serverSn, 6);
  n.serverIa = r.u16();
  n.nChannels = r.u8();
  n.nActions = r.u8();
  n.nScenes = r.u8();
  n.nKeys = r.u8();
  if (n.nChannels > kMaxChannels || n.nActions > kMaxActions || n.nScenes > kMaxScenes || n.nKeys > kMaxKeys)
    return false;
  for (uint8_t i = 0; i < n.nChannels; ++i) {
    Channel& ch = n.channels[i];
    ch.ia = r.u16();
    r.bytes(ch.sn, 6);
    ch.ch = r.u8();
    ch.offset = r.u8();
    ch.obj = r.u8();
    const uint8_t flags = r.u8();
    ch.dimmer = flags & 1;
    ch.blind = flags & 2;
    ch.gaBase = r.u8();
  }
  for (uint8_t i = 0; i < n.nActions; ++i) {
    Action& a = n.actions[i];
    a.ia = r.u16();
    a.handle = r.u16();
    a.len = r.u8();
    if (a.len == 0 || a.len > sizeof(a.value)) return false;
    r.bytes(a.value, a.len);
  }
  for (uint8_t i = 0; i < n.nScenes; ++i) {
    n.scenes[i].first = r.u8();
    n.scenes[i].count = r.u8();
    if ((size_t)n.scenes[i].first + n.scenes[i].count > n.nActions) return false;
  }
  for (uint8_t i = 0; i < n.nKeys; ++i) {
    Key& k = n.keys[i];
    r.bytes(k.sn, 6);
    k.ga = r.u16();
    k.scene = (int8_t)r.u8();
    if (k.scene >= (int)n.nScenes) return false;
  }
  if (!r.ok || r.pos != len - 2) return false;
  c = n;
  return true;
}

size_t serializeConfig(const Config& c, uint8_t* out, size_t cap) {
  Writer w(out, cap);
  w.u8('E');
  w.u8(0x03);
  w.bytes(c.domain, 6);
  w.bytes(c.serverSn, 6);
  w.u16(c.serverIa);
  w.u8(c.nChannels);
  w.u8(c.nActions);
  w.u8(c.nScenes);
  w.u8(c.nKeys);
  for (uint8_t i = 0; i < c.nChannels; ++i) {
    const Channel& ch = c.channels[i];
    w.u16(ch.ia);
    w.bytes(ch.sn, 6);
    w.u8(ch.ch);
    w.u8(ch.offset);
    w.u8(ch.obj);
    w.u8((ch.dimmer ? 1 : 0) | (ch.blind ? 2 : 0));
    w.u8(ch.gaBase);
  }
  for (uint8_t i = 0; i < c.nActions; ++i) {
    const Action& a = c.actions[i];
    w.u16(a.ia);
    w.u16(a.handle);
    w.u8(a.len);
    w.bytes(a.value, a.len);
  }
  for (uint8_t i = 0; i < c.nScenes; ++i) {
    w.u8(c.scenes[i].first);
    w.u8(c.scenes[i].count);
  }
  for (uint8_t i = 0; i < c.nKeys; ++i) {
    w.bytes(c.keys[i].sn, 6);
    w.u16(c.keys[i].ga);
    w.u8((uint8_t)c.keys[i].scene);
  }
  if (!w.ok) return 0;
  uint16_t crc = knxrf::crc16(out, w.pos);
  w.u16(crc);
  return w.ok ? w.pos : 0;
}

void Server::setConfig(const Config& c) {
  cfg_ = c;
  for (auto& s : st_) s = ChannelState();
  for (auto& v : lastSeqValid_) v = false;
  qHead_ = qCount_ = 0;
  busy_ = false;
}

int Server::channelIndex(uint16_t ia, uint16_t handle) const {
  for (uint8_t i = 0; i < cfg_.nChannels; ++i) {
    const Channel& ch = cfg_.channels[i];
    if (ch.ia == ia && handle >= ch.offset && handle < ch.offset + (ch.blind ? kSpanBlind : kSpanLight)) return i;
  }
  return -1;
}

int Server::channelIndexByCh(uint16_t ia, uint8_t ch) const {
  for (uint8_t i = 0; i < cfg_.nChannels; ++i)
    if (cfg_.channels[i].ia == ia && cfg_.channels[i].ch == ch) return i;
  return -1;
}

bool Server::push(const Job& j) {
  if (qCount_ >= kQueueLen) return false;
  q_[(qHead_ + qCount_) % kQueueLen] = j;
  ++qCount_;
  return true;
}

bool Server::queueSet(uint16_t ia, uint16_t handle, const uint8_t* v, uint8_t len, Origin o, uint32_t id) {
  if (len == 0 || len > 4) return false;
  Job j{};
  j.kind = JobKind::GO_SET;
  j.id = id;
  j.ia = ia;
  j.handle = handle;
  j.len = len;
  memcpy(j.value, v, len);
  j.origin = o;
  j.scene = -1;
  return push(j);
}

bool Server::queueRead(uint16_t ia, uint16_t handle, Origin o, uint32_t id) {
  Job j{};
  j.kind = JobKind::GO_READ;
  j.id = id;
  j.ia = ia;
  j.handle = handle;
  j.read = true;
  j.origin = o;
  j.scene = -1;
  return push(j);
}

bool Server::queueProp(JobKind kind, uint16_t ia, uint8_t obj, uint8_t pid, uint16_t start, uint8_t count,
                       const uint8_t* data, uint8_t len, uint32_t id) {
  if ((kind != JobKind::PROP_READ && kind != JobKind::PROP_WRITE) || count == 0 || count > 15 || start > 0xFFF ||
      len > kMaxJobData || (kind == JobKind::PROP_WRITE && len == 0))
    return false;
  Job j{};
  j.kind = kind;
  j.id = id;
  j.ia = ia;
  j.obj = obj;
  j.pid = pid;
  j.start = start;
  j.count = count;
  j.len = kind == JobKind::PROP_WRITE ? len : 0;
  if (j.len) memcpy(j.value, data, len);
  j.origin = Origin::HOST;
  j.scene = -1;
  return push(j);
}

bool Server::queueNpWrite(uint16_t ia, const uint8_t* data, uint8_t len, uint32_t id) {
  if (len == 0 || len > kMaxJobData) return false;
  Job j{};
  j.kind = JobKind::NP_WRITE;
  j.id = id;
  j.ia = ia;
  j.len = len;
  memcpy(j.value, data, len);
  j.origin = Origin::HOST;
  j.scene = -1;
  return push(j);
}

bool Server::queueFuncProp(JobKind kind, uint16_t ia, uint8_t obj, uint8_t pid, const uint8_t* data, uint8_t len,
                           uint32_t id) {
  if ((kind != JobKind::FP_CMD && kind != JobKind::FP_READ) || len > kMaxJobData) return false;
  Job j{};
  j.kind = kind;
  j.id = id;
  j.ia = ia;
  j.obj = obj;
  j.pid = pid;
  j.len = len;
  if (len) memcpy(j.value, data, len);
  j.origin = Origin::HOST;
  j.scene = -1;
  return push(j);
}

bool Server::queueGroupWrite(uint16_t ga, const uint8_t* data, uint8_t len, uint32_t id) {
  if (len == 0 || len > kMaxJobData) return false;
  Job j{};
  j.kind = JobKind::GROUP_WRITE;
  j.id = id;
  j.handle = ga;
  j.len = len;
  memcpy(j.value, data, len);
  j.origin = Origin::HOST;
  j.scene = -1;
  return push(j);
}

bool Server::runScene(uint8_t idx, uint32_t id) {
  if (idx >= cfg_.nScenes) return false;
  const Scene& s = cfg_.scenes[idx];
  if (qCount_ + s.count > kQueueLen) return false;
  sceneFailed_[idx] = 0;
  for (uint8_t k = 0; k < s.count; ++k) {
    const Action& a = cfg_.actions[s.first + k];
    Job j{};
    j.kind = a.ia ? JobKind::GO_SET : JobKind::GROUP_WRITE;
    j.ia = a.ia;
    j.handle = a.handle;
    j.len = a.len;
    memcpy(j.value, a.value, a.len);
    j.origin = Origin::SCENE;
    j.id = id;
    j.scene = (int8_t)idx;
    j.lastOfScene = (k + 1 == s.count);
    push(j);
  }
  if (s.count == 0) io_.onSceneDone(id, idx, 0);
  return true;
}

void Server::sendJob(uint32_t now) {
  uint8_t frame[64];
  size_t n = 0;
  switch (cur_.kind) {
    case JobKind::GO_SET:
      n = knxrf::buildFuncPropCmd(cfg_.domain, cur_.ia, cur_.handle, cur_.value, cur_.len, 0, frame);
      break;
    case JobKind::GO_READ:
      n = knxrf::buildFuncPropStateRead(cfg_.domain, cur_.ia, cur_.handle, 0, frame);
      break;
    case JobKind::PROP_READ:
      n = knxrf::buildPropRead(cfg_.domain, cur_.ia, cur_.obj, cur_.pid, cur_.start, cur_.count, 0, frame);
      break;
    case JobKind::PROP_WRITE:
      n = knxrf::buildPropWrite(cfg_.domain, cur_.ia, cur_.obj, cur_.pid, cur_.start, cur_.count, cur_.value,
                                cur_.len, 0, frame);
      break;
    case JobKind::NP_WRITE:
      n = knxrf::buildNetParamWriteP2P(cfg_.domain, cur_.ia, cur_.value, cur_.len, 0, frame);
      break;
    case JobKind::FP_CMD:
    case JobKind::FP_READ:
      n = knxrf::buildFuncProp(cfg_.domain, cur_.ia,
                               cur_.kind == JobKind::FP_CMD ? knxrf::APCI_FUNC_PROP_CMD : knxrf::APCI_FUNC_PROP_STATE_READ,
                               cur_.obj, cur_.pid, cur_.value, cur_.len, 0, frame);
      break;
    case JobKind::GROUP_WRITE:
      n = knxrf::buildServerGroupWrite(cfg_.serverSn, cur_.handle, cur_.value, cur_.len, 0, frame);
      break;
  }
  bool sent = n && io_.transmit(frame, n);
  txPending_ = !sent && busyRetries_ < kBusyRetries;
  if (txPending_) ++busyRetries_;
  deadline_ = now + (txPending_                           ? kBusyRetryMs
                     : cur_.kind == JobKind::GROUP_WRITE ? kGroupGapMs
                                                         : kAckTimeoutMs + (tries_ - 1) * kAckStepMs);
  if (cur_.kind == JobKind::NP_WRITE && !txPending_) finishJob(sent, sent ? 0 : -1, nullptr, 0, now);
}

void Server::startJob(uint32_t now) {
  cur_ = q_[qHead_];
  qHead_ = (qHead_ + 1) % kQueueLen;
  --qCount_;
  busy_ = true;
  tries_ = 1;
  busyRetries_ = 0;
  sendJob(now);
}

void Server::finishJob(bool ok, int rc, const uint8_t* val, size_t len, uint32_t now) {
  busy_ = false;
  if (cur_.kind != JobKind::GO_SET && cur_.kind != JobKind::GO_READ) {
    if (cur_.scene >= 0) return sceneStep(ok);
    uint16_t start = cur_.kind == JobKind::GROUP_WRITE ? cur_.handle : cur_.start;
    io_.onPropResult(cur_.id, cur_.ia, cur_.kind, cur_.obj, cur_.pid, start, ok, rc, tries_, val, len);
    return;
  }
  int ci = channelIndex(cur_.ia, cur_.handle);
  if (ci >= 0 && ok) {
    const Channel& ch = cfg_.channels[ci];
    if (!cur_.read) {
      st_[ci].cmdAt = now;
      st_[ci].pollStage = 0;
      schedulePoll(ci, now);  // query status in case the actuator does not report by itself
    } else if (ch.blind) {
      if (len >= 1 && cur_.handle == ch.offset + kGoBlindPosNow)
        updateBlind(ci, val[0] * 100 / 255, -1, -1, Source::POLL);
      else if (len >= 1 && cur_.handle == ch.offset + kGoBlindSlatNow)
        updateBlind(ci, -1, val[0] * 100 / 255, -1, Source::POLL);
    } else if (len >= 1 && cur_.handle == ch.offset + kGoStatus) {
      updateState(ci, true, val[0] != 0, false, 0, Source::POLL);
    } else if (len >= 1 && cur_.handle == ch.offset + kGoStatusValue) {
      updateState(ci, false, false, true, (uint8_t)(val[0] * 100 / 255), Source::POLL);
    }
  }
  if (cur_.scene < 0) {
    io_.onResult(cur_.id, cur_.ia, cur_.handle, cur_.read, cur_.origin, ok, rc, tries_, val, len);
  } else {
    sceneStep(ok);
  }
}

// Scenes only report their completion
void Server::sceneStep(bool ok) {
  if (!ok) ++sceneFailed_[cur_.scene];
  if (cur_.lastOfScene) io_.onSceneDone(cur_.id, (uint8_t)cur_.scene, sceneFailed_[cur_.scene]);
}

void Server::updateState(int ci, bool setOn, bool on, bool setPercent, uint8_t percent, Source src) {
  ChannelState& s = st_[ci];
  bool changed = !s.known;
  if (setOn && s.on != on) changed = true;
  if (setPercent && s.percent != percent) changed = true;
  if (setOn) s.on = on;
  if (setPercent) s.percent = percent;
  s.known = true;
  // Always report polls: after commands the host provisionally sets the target state, the table here does not –
  // an executed but differing state (e.g. scene, lost status message) would otherwise never be corrected
  if (changed || src == Source::POLL) io_.onState(cfg_.channels[ci], s, src);
}

void Server::updateBlind(int ci, int percent, int slat, int move, Source src) {
  ChannelState& s = st_[ci];
  bool changed = !s.known;
  if (percent >= 0 && s.percent != percent) changed = true;
  if (slat >= 0 && s.slat != slat) changed = true;
  if (move >= 0 && s.move != move) changed = true;
  if (percent >= 0) s.percent = (uint8_t)percent;
  if (slat >= 0) s.slat = (uint8_t)slat;
  if (move >= 0) s.move = (uint8_t)move;
  s.on = s.percent > 0;  // "not fully up"
  s.known = true;
  if (changed || src == Source::POLL) io_.onState(cfg_.channels[ci], s, src);
}

// Schedule a poll: light once after kPollDelayMs, blind in stages (kBlindPollMs from the command)
void Server::schedulePoll(int ci, uint32_t now) {
  ChannelState& s = st_[ci];
  if (!cfg_.channels[ci].blind) {
    s.pollPending = true;
    s.pollAt = now + kPollDelayMs;
    return;
  }
  if (s.pollStage >= kBlindPolls) {
    s.pollPending = false;
    return;
  }
  s.pollPending = true;
  s.pollAt = s.cmdAt + kBlindPollMs[s.pollStage++];
}

void Server::onFrame(const knxrf::Telegram& t, uint32_t now) {
  // Telegram from the real server? (The bridge does not receive its own transmissions.) Only report, do not
  // switch off: an error detection must never permanently disable the bridge (decision 2026-10-29).
  if ((t.doa && t.src == cfg_.serverIa) || (!t.doa && memcmp(t.addr, cfg_.serverSn, 6) == 0)) {
    if (!realServerSeen_ || now - realServerAt_ >= kRealServerReportMs) {
      realServerSeen_ = true;
      realServerAt_ = now;
      io_.onRealServer();
    }
    return;
  }

  // Acknowledgement/response to the current job
  knxrf::FuncPropResp resp;
  if (busy_ && t.doa && t.src == cur_.ia && (cur_.kind == JobKind::GO_SET || cur_.kind == JobKind::GO_READ) &&
      knxrf::parseFuncPropResp(t, resp)) {
    finishJob(resp.rc == 0, resp.rc, resp.value, resp.valueLen, now);
    return;
  }
  if (busy_ && t.doa && t.src == cur_.ia && (cur_.kind == JobKind::FP_CMD || cur_.kind == JobKind::FP_READ) &&
      knxrf::parseFuncPropResp(t, resp) && resp.obj == cur_.obj && resp.pid == cur_.pid) {
    finishJob(resp.rc == 0, resp.rc, resp.value, resp.valueLen, now);
    return;
  }
  knxrf::PropResp pr;
  if (busy_ && t.doa && t.src == cur_.ia && (cur_.kind == JobKind::PROP_READ || cur_.kind == JobKind::PROP_WRITE) &&
      knxrf::parsePropResp(t, pr) && pr.obj == cur_.obj && pr.pid == cur_.pid && pr.start == cur_.start) {
    finishJob(pr.count > 0, pr.count > 0 ? 0 : 1, pr.value, pr.valueLen, now);
    return;
  }

  // Key presses: server key (SumstatusGet to server SN), linked key (SumstatusGet to actuator)
  knxrf::SumstatusGet sg;
  if (knxrf::parseSumstatusGet(t, sg)) {
    bool toServer = memcmp(sg.replySN, cfg_.serverSn, 6) == 0;
    KeyEvent e{t.addr, t.dst, toServer ? KeyKind::SERVER : KeyKind::DIRECT, sg.cmd, sg, -1,
               (sg.cmd & 0x4000) != 0, false, false, t.rfInfo};
    if (toServer) {  // lower GA byte only: the transmitter's GA base increases with every factory reset
      for (uint8_t i = 0; i < cfg_.nKeys; ++i)
        if ((cfg_.keys[i].ga & 0xFF) == (t.dst & 0xFF) && memcmp(cfg_.keys[i].sn, t.addr, 6) == 0) e.keyIndex = i;
    } else {  // directly linked: 0x40xx/0x80xx, hold duration 0 = press
      e.press = (sg.cmd & 0x3FFF) == 0;
    }
    if (toServer && enabled_) {
      uint8_t frame[32];
      size_t n = knxrf::buildSumstatusInfo(cfg_.serverSn, sg.replyGA, 0xFF, sg.seq, 0, frame);
      if (n) io_.transmit(frame, n);
      e.answered = true;
      // Scene on press. 0.8.10 also recalled it on release (like the server) – reverted in 0.8.11: that made
      // lamps flicker, and a single release without a received press (second key touched while holding)
      // switched the wrong scene. The cause (lamp dimmed down on release) was old links in the actuator,
      // see NOTES "FW 0.8.10".
      // Exception (0.8.16): release with seq = last seen + 2 – exactly one telegram is missing in between, and
      // that was the press (press N, release N+1, consecutive per key). It got lost while the bridge was
      // transmitting (status polls after a scene, 2026-11-09). A release without history (+1 or after
      // startup) still recalls nothing.
      if (e.keyIndex >= 0) {
        const uint8_t i = (uint8_t)e.keyIndex;
        bool start = false;
        if (e.press) {
          start = !(lastSeqValid_[i] && lastSeq_[i] == sg.seq);
          lastSeq_[i] = sg.seq;
          lastSeqValid_[i] = true;
        } else {
          start = lastAnyValid_[i] && (uint16_t)(sg.seq - lastAnySeq_[i]) == 2;
          if (start) {  // make up for the lost press: a repetition of it is then not a new scene
            lastSeq_[i] = (uint16_t)(sg.seq - 1);
            lastSeqValid_[i] = true;
          }
        }
        if (!(lastAnyValid_[i] && lastAnySeq_[i] == sg.seq)) {
          lastAnySeq_[i] = sg.seq;
          lastAnyValid_[i] = true;
        }
        int8_t sc = cfg_.keys[i].scene;
        if (start && sc >= 0) e.sceneStarted = runScene((uint8_t)sc);
      }
    }
    io_.onKey(e);
    return;
  }

  // bare key value of an unassigned rocker (GroupWrite, 2 bytes, under transmitter SN)
  if (t.group && !t.doa && t.apciBase() == knxrf::APCI_GROUP_WRITE && t.dataLen == 2) {
    uint16_t v = (uint16_t)(t.data[0] << 8 | t.data[1]);
    KeyEvent e{t.addr, t.dst, KeyKind::VALUE, v, {}, -1, (v & 0x3FFF) == 0, false, false,
               t.rfInfo};
    io_.onKey(e);
    return;
  }

  // Blind: 1-byte message under its own SN to base * 256 + offset + 10/12/13 (movement info, position, slat)
  // (movement info 2 bits: as short value in the APCI or as a separate byte)
  if (t.group && !t.doa && t.apciBase() == knxrf::APCI_GROUP_WRITE) {
    for (uint8_t i = 0; i < cfg_.nChannels; ++i) {
      const Channel& ch = cfg_.channels[i];
      if (!ch.blind || memcmp(ch.sn, t.addr, 6) != 0) continue;
      const uint8_t go = (uint8_t)((t.dst & 0xFF) - ch.offset);
      const uint8_t v = t.dataLen ? t.data[0] : (uint8_t)(t.apci & 0x3F);
      if (go == kGoBlindInfo) return updateBlind(i, -1, -1, v & 3, Source::STATUS_MSG);
      if (go == kGoBlindPosNow && t.dataLen == 1) return updateBlind(i, v * 100 / 255, -1, -1, Source::STATUS_MSG);
      if (go == kGoBlindSlatNow && t.dataLen == 1) return updateBlind(i, -1, v * 100 / 255, -1, Source::STATUS_MSG);
      if (go == kGoBlindSumstatus && t.dataLen == 3)
        return updateBlind(i, t.data[0] & 0x7F, -1, -1, Source::STATUS_MSG);
    }
  }

  // 4-byte group value under transmitter SN (energy sensor: power, energy)
  if (t.group && !t.doa && t.apciBase() == knxrf::APCI_GROUP_WRITE && t.dataLen == 4) {
    KeyEvent e{t.addr, t.dst, KeyKind::VALUE4, 0, {}, -1, false, false, false, t.rfInfo,
               (uint32_t)t.data[0] << 24 | (uint32_t)t.data[1] << 16 | (uint32_t)t.data[2] << 8 | t.data[3]};
    io_.onKey(e);
    return;
  }

  // 1-byte group value under transmitter SN: scene recall of the sun sensor (DPT 18, e.g. GA base*256+3 or +7)
  if (t.group && !t.doa && t.apciBase() == knxrf::APCI_GROUP_WRITE && t.dataLen == 1) {
    KeyEvent e{t.addr, t.dst, KeyKind::VALUE1, t.data[0], {}, -1, false, false, false, t.rfInfo};
    io_.onKey(e);
    return;
  }

  // Spontaneous actuator status message: under its own SN to GA = base * 256 + offset + 22. Only the lower GA
  // byte counts (as with keys): the base increases with every factory reset, and a wrong base in the config
  // (e.g. import) would otherwise leave all messages of the actuator unrecognized. The SN makes it unique.
  if (t.group && !t.doa && t.apciBase() == knxrf::APCI_GROUP_WRITE && t.dataLen == 3) {
    for (uint8_t i = 0; i < cfg_.nChannels; ++i) {
      const Channel& ch = cfg_.channels[i];
      if (!ch.blind && (t.dst & 0xFF) == ((ch.offset + kGoSumstatusInfo) & 0xFF) && memcmp(ch.sn, t.addr, 6) == 0) {
        st_[i].pollPending = false;
        updateState(i, true, (t.data[0] & 0x80) != 0, true, t.data[0] & 0x7F, Source::STATUS_MSG);
      }
    }
  }
}

void Server::tick(uint32_t now) {
  for (uint8_t i = 0; i < cfg_.nChannels; ++i) {
    ChannelState& s = st_[i];
    if (s.pollPending && reached(now, s.pollAt)) {
      s.pollPending = false;
      const Channel& ch = cfg_.channels[i];
      if (ch.blind) {
        queueRead(ch.ia, ch.offset + kGoBlindPosNow, Origin::POLL);
        queueRead(ch.ia, ch.offset + kGoBlindSlatNow, Origin::POLL);
        schedulePoll(i, now);  // next stage
        continue;
      }
      queueRead(ch.ia, ch.offset + kGoStatus, Origin::POLL);
      if (ch.dimmer) queueRead(ch.ia, ch.offset + kGoStatusValue, Origin::POLL);
    }
  }
  if (busy_ && reached(now, deadline_)) {
    if (txPending_) {
      sendJob(now);  // channel was busy: the same attempt once more
    } else if (cur_.kind == JobKind::GROUP_WRITE && tries_ >= kGroupRepeats) {
      finishJob(true, 0, nullptr, 0, now);
    } else if (tries_ < kTries) {
      ++tries_;
      sendJob(now);
    } else {
      finishJob(false, -1, nullptr, 0, now);
    }
  }
  if (!busy_ && qCount_) startJob(now);
}

}  // namespace enet

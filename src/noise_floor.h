#pragma once
// Noise floor and channel-busy threshold for listen-before-talk (platform-independent, host tests).
//
// The carrier sense threshold of the CC1101 (AGCCTRL1) is fixed and depends on module, antenna and environment: with
// a 16 cm antenna it reported the channel busy up to 71 % of the time, with 8.6 cm wire 0–2 %. Instead, the
// noise floor is estimated from running RSSI samples (lower quantile: 0.05 dB down per sample below it, 0.01 dB
// up per sample above it → equilibrium at ~17 % of samples below). Short telegrams barely raise it, a persistent
// interferer does after a few seconds – it should not block transmitting permanently. Busy = RSSI > noise floor
// + margin.
#include <stdint.h>

class NoiseFloor {
 public:
  static constexpr float kMarginDb = 10.0f;  // busy from noise floor + 10 dB
  static constexpr float kStepDown = 0.05f, kStepUp = 0.01f;
  static constexpr float kMin = -120.0f, kMax = -70.0f;

  // One RSSI sample (dBm, while receiving); returns whether the channel is considered busy.
  bool sample(float rssi) {
    if (!init_) {
      floor_ = rssi;
      init_ = true;
    } else if (rssi < floor_) {
      floor_ -= kStepDown;
    } else if (rssi > floor_) {
      floor_ += kStepUp;
    }
    if (floor_ < kMin) floor_ = kMin;
    if (floor_ > kMax) floor_ = kMax;
    const bool b = busy(rssi);
    // Busy fraction, moving over ~1000 samples
    share_ += ((b ? 1.0f : 0.0f) - share_) * 0.001f;
    return b;
  }

  bool busy(float rssi) const { return init_ && rssi > floor_ + kMarginDb; }
  bool ready() const { return init_; }
  float floorDb() const { return floor_; }
  float threshold() const { return floor_ + kMarginDb; }
  float busyShare() const { return share_; }

 private:
  bool init_ = false;
  float floor_ = 0, share_ = 0;
};

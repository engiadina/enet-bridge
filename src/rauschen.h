#pragma once
// Grundrauschen und Kanal-belegt-Schwelle für Listen-before-talk (plattformunabhängig, Host-Tests).
//
// Die Carrier-Sense-Schwelle des CC1101 (AGCCTRL1) ist fest und hängt von Modul, Antenne und Umgebung ab: Mit
// 16-cm-Antenne meldete sie den Kanal bis 71 % der Zeit belegt, mit 8,6-cm-Draht 0–2 %. Stattdessen wird das
// Grundrauschen aus laufenden RSSI-Proben geschätzt (unteres Quantil: je Probe darunter 0,05 dB abwärts, darüber
// 0,01 dB aufwärts → Gleichgewicht bei ~17 % der Proben darunter). Kurze Telegramme heben es kaum, ein dauerhafter
// Störer nach einigen Sekunden schon – er soll das Senden nicht dauerhaft sperren. Belegt = RSSI > Grundrauschen
// + Abstand.
#include <stdint.h>

class Rauschen {
 public:
  static constexpr float kAbstandDb = 10.0f;  // belegt ab Grundrauschen + 10 dB
  static constexpr float kAb = 0.05f, kAuf = 0.01f;
  static constexpr float kMin = -120.0f, kMax = -70.0f;

  // Eine RSSI-Probe (dBm, im Empfang); liefert, ob der Kanal damit als belegt gilt.
  bool probe(float rssi) {
    if (!init_) {
      boden_ = rssi;
      init_ = true;
    } else if (rssi < boden_) {
      boden_ -= kAb;
    } else if (rssi > boden_) {
      boden_ += kAuf;
    }
    if (boden_ < kMin) boden_ = kMin;
    if (boden_ > kMax) boden_ = kMax;
    const bool b = belegt(rssi);
    // Belegt-Anteil, gleitend über ~1000 Proben
    anteil_ += ((b ? 1.0f : 0.0f) - anteil_) * 0.001f;
    return b;
  }

  bool belegt(float rssi) const { return init_ && rssi > boden_ + kAbstandDb; }
  bool bereit() const { return init_; }
  float boden() const { return boden_; }
  float schwelle() const { return boden_ + kAbstandDb; }
  float anteil() const { return anteil_; }

 private:
  bool init_ = false;
  float boden_ = 0, anteil_ = 0;
};

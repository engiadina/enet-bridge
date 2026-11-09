#!/bin/sh
# Firmware bauen und als Bündel in die HA-Integration legen (custom_components/enet_bridge/bridge_fw/<chip>/).
#
#   tools/fw_bundle.sh
#
# Die Integration flasht damit eine Bridge selbst (Update-Entität; automatisch, wenn die Bridge älter ist).
# Version = kFwVersion aus src/main.cpp. Je Chip ein Bündel:
#   esp32    ESP32-DevKit      (env esp32dev,     Arduino 2.x)
#   esp32c6  XIAO ESP32-C6     (env xiao_esp32c6, Arduino 3.x/pioarduino)
# Der C6 baut in einem eigenen PlatformIO-Verzeichnis: beide Plattformen heißen „espressif32“ und tauschen sonst
# bei jedem Wechsel tool-esptoolpy aus (der erste Lauf danach scheitert).
set -e
cd "$(dirname "$0")/.."
OUT=custom_components/enet_bridge/bridge_fw

PIO=~/.platformio/penv/bin/pio
[ -x "$PIO" ] || PIO=pio  # z. B. Homebrew-Installation
C6_CORE=~/.platformio-pioarduino

bundle() {  # <env> <chip> <Core-Verzeichnis>
  PLATFORMIO_CORE_DIR="$3" "$PIO" run -e "$1"
  mkdir -p "$OUT/$2"
  B=.pio/build/$1
  cp "$B/bootloader.bin" "$B/partitions.bin" "$B/firmware.bin" "$OUT/$2/"
  cp "$3/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin" "$OUT/$2/"
}

bundle esp32dev esp32 ~/.platformio
bundle xiao_esp32c6 esp32c6 "$C6_CORE"
sed -n 's/^static const char\* kFwVersion = "\(.*\)";/\1/p' src/main.cpp > "$OUT/version.txt"
echo "Bündel $(cat "$OUT/version.txt") in $OUT: $(cd "$OUT" && echo */)"

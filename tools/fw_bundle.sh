#!/bin/sh
# Build the firmware and place it as a bundle in the HA integration (custom_components/enet_bridge/bridge_fw/<chip>/).
#
#   tools/fw_bundle.sh
#
# The integration uses it to flash a bridge itself (update entity; automatically if the bridge is older).
# Version = kFwVersion from src/main.cpp. One bundle per chip:
#   esp32    ESP32-DevKit      (env esp32dev,     Arduino 2.x)
#   esp32c6  XIAO ESP32-C6     (env xiao_esp32c6, Arduino 3.x/pioarduino)
# The C6 builds in its own PlatformIO directory: both platforms are called "espressif32" and would otherwise swap
# tool-esptoolpy on every switch (the first run afterwards fails).
set -e
cd "$(dirname "$0")/.."
OUT=custom_components/enet_bridge/bridge_fw

PIO=~/.platformio/penv/bin/pio
[ -x "$PIO" ] || PIO=pio  # e.g. Homebrew installation
C6_CORE=~/.platformio-pioarduino

bundle() {  # <env> <chip> <core directory>
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

#!/bin/sh
# Firmware bauen und als Bündel in die HA-Integration legen (custom_components/enet_bridge/bridge_fw/).
#
#   tools/fw_bundle.sh
#
# Die Integration flasht damit eine Bridge selbst (Update-Entität; automatisch, wenn die Bridge älter ist).
# Version = kFwVersion aus src/main.cpp.
set -e
cd "$(dirname "$0")/.."
BUILD=.pio/build/esp32dev
OUT=custom_components/enet_bridge/bridge_fw

PIO=~/.platformio/penv/bin/pio
[ -x "$PIO" ] || PIO=pio  # z. B. Homebrew-Installation
"$PIO" run -e esp32dev
mkdir -p "$OUT"
cp "$BUILD/bootloader.bin" "$BUILD/partitions.bin" "$BUILD/firmware.bin" \
   ~/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin "$OUT/"
sed -n 's/^static const char\* kFwVersion = "\(.*\)";/\1/p' src/main.cpp > "$OUT/version.txt"
echo "Bündel $(cat "$OUT/version.txt") in $OUT"

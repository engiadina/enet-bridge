#!/bin/sh
# Firmware bauen und über den HA-Rechner (z. B. Raspberry Pi) per USB in die Bridge flashen.
#
#   tools/flash_raspi.sh [root@<ha-host>]   (Standard: $ENET_HA_HOST, sonst root@homeassistant.local)
#
# Ablauf (docs/INTERFACE.md 10.5): bauen → Bootloader/Partitionen/App per SFTP auf den Raspi →
# esptool auf dem Raspi (App ab 0x10000, beim ersten Mal alles) → Bridge startet neu.
# Hält die HA-Integration den Port, wird sie vorher angehalten und danach wieder geladen (sobald vorhanden).
set -e
HOST=${1:-${ENET_HA_HOST:-root@homeassistant.local}}
PORT=/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
BUILD=.pio/build/esp32dev
REMOTE=/tmp/enet-fw

cd "$(dirname "$0")/.."
~/.platformio/penv/bin/pio run -e esp32dev
ssh "$HOST" "mkdir -p $REMOTE"
scp -q "$BUILD/bootloader.bin" "$BUILD/partitions.bin" "$BUILD/firmware.bin" \
    ~/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin "$HOST:$REMOTE/"

# Integration (falls installiert) gibt den Port frei
ssh "$HOST" "ha core info >/dev/null 2>&1 && [ -d /config/custom_components/enet_bridge ] && \
  curl -s -X POST -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" http://supervisor/core/api/services/enet_bridge/release >/dev/null || true"

ssh "$HOST" "python3 -m esptool --chip esp32 --port $PORT --baud 460800 write_flash -z \
  0x1000 $REMOTE/bootloader.bin 0x8000 $REMOTE/partitions.bin 0xe000 $REMOTE/boot_app0.bin 0x10000 $REMOTE/firmware.bin"

ssh "$HOST" "[ -d /config/custom_components/enet_bridge ] && \
  curl -s -X POST -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" http://supervisor/core/api/services/enet_bridge/reconnect >/dev/null || true"
echo "Fertig."

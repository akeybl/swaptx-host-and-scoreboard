#!/bin/sh
# Compile + flash the sniffer firmware with arduino-cli (brew install arduino-cli; core esp32:esp32).
# Boards with a WCH CH340/CH9102 USB chip need the WCH driver on macOS: brew install --cask wch-ch34x-usb-serial-driver
# Usage: ./flash.sh [/dev/cu.usbserial-XXXX]
cd "$(dirname "$0")"
PORT="$1"
if [ -z "$PORT" ]; then PORT=$(ls /dev/cu.usbserial* /dev/cu.SLAB_USBtoUART* /dev/cu.wchusbserial* 2>/dev/null | head -1); fi
[ -z "$PORT" ] && { echo "no USB serial port found; plug the ESP32 in or pass the port"; exit 1; }
arduino-cli core list | grep -q esp32:esp32 || { arduino-cli config add board_manager.additional_urls https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json; arduino-cli core update-index; arduino-cli core install esp32:esp32; }
FQBN="esp32:esp32:esp32:PartitionScheme=huge_app"   # BLE + WiFi need the 3 MB app partition
arduino-cli compile --fqbn "$FQBN" firmware/swaptx_sniffer && arduino-cli upload --fqbn "$FQBN" -p "$PORT" firmware/swaptx_sniffer
echo "flashed. run ./run.sh (it auto-detects $PORT)"

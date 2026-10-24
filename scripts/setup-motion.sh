#!/usr/bin/env bash
# Configure a Tasmota ESP32 (or ESP8266) as a motion / presence sensor: PIR (e.g. AM312, HC-SR501) or the OUT pin of
# an mmWave radar (e.g. HLK-LD2410) on one GPIO, set up as Switch1. Already on Wi-Fi and MQTT (see README).
# Run on the server from /opt/archmon:   scripts/setup-motion.sh <full MQTT topic> <gpio>
#   e.g. scripts/setup-motion.sh bldga-f1-map-pir-01_A1B2C3 4
# Then give the sensor_id a row in registry/sensors.csv with kind=motion (otherwise Switch1 is read as a leak probe).
# A rule publishes every change at once plus the current state every minute (heartbeat for the offline alert).
# Untested on hardware so far: check `tele/<topic>/SENSOR` with mosquitto_sub after setup.
set -euo pipefail
cd "$(dirname "$0")/.."
T="${1:?full MQTT topic, e.g. bldga-f1-map-pir-01_A1B2C3}"
G="${2:?GPIO number the sensor output is wired to}"
case "$G" in ''|*[!0-9]*) echo "GPIO must be a number"; exit 1 ;; esac
grep -q '^TASMOTA_WEB_PASSWORD=' .env || echo "TASMOTA_WEB_PASSWORD=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 16)" >> .env
chmod 600 .env
set -a; . ./.env; set +a
pub() { docker exec archmon-mosquitto mosquitto_pub -u admin -P "$MQTT_ADMIN_PASSWORD" -t "cmnd/$T/$1" -m "$2"; }
pub WebPassword "$TASMOTA_WEB_PASSWORD"
sleep 1
# Switch1 = component 160; SwitchMode 1 = follow the input (high = motion). The device restarts after a GPIO change.
pub Backlog "GPIO$G 160; SwitchMode1 1; SetOption8 0; TelePeriod 60"
echo "Waiting for $T to restart..."; sleep 20
pub Rule1 "ON Switch1#State DO Backlog Var1 %value%; Publish tele/$T/SENSOR {\"Switch1\":\"%value%\"} ENDON ON Time#Minute DO Publish tele/$T/SENSOR {\"Switch1\":\"%var1%\"} ENDON"
pub Rule1 1
echo "Sent motion setup to $T. Add its sensor_id to registry/sensors.csv with kind=motion, then: docker restart archmon-telegraf"

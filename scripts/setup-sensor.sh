#!/usr/bin/env bash
# One-step setup for a new IoTorero ST01 (Tasmota, ESP32-C3, SHT40) that is already on Wi-Fi and MQTT.
# Run on the Pi from /opt/archmon:   scripts/setup-sensor.sh <full MQTT topic, e.g. bldga-f1-vault-01_A1B2C3>
# Applies: SHT40 pin template, 60 s reporting, Celsius, dim green LED, web UI password. The sensor restarts once.
# The web UI login is user "admin" with TASMOTA_WEB_PASSWORD from .env (generated here if missing).
set -euo pipefail
cd "$(dirname "$0")/.."
T="${1:?usage: setup-sensor.sh <topic>}"
grep -q '^TASMOTA_WEB_PASSWORD=' .env || echo "TASMOTA_WEB_PASSWORD=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 16)" >> .env
chmod 600 .env
set -a; . ./.env; set +a
pub() { docker exec archmon-mosquitto mosquitto_pub -u admin -P "$MQTT_ADMIN_PASSWORD" -t "cmnd/$T/$1" -m "$2"; }
pub WebPassword "$TASMOTA_WEB_PASSWORD"
sleep 1
pub Backlog 'Template {"NAME":"IoTorero SHT40","GPIO":[0,0,0,0,0,0,0,0,608,32,640,0,0,0,0,0,0,0,0,0,0,1376],"FLAG":0,"BASE":1}; TelePeriod 60; SetOption8 0; Pixels 1; Color 00FF00; Dimmer 20; PowerOnState 1; Power ON; Module 0'
echo "Sent setup to $T; it restarts in a few seconds. Add its sensor_id to registry/sensors.csv if not already there."

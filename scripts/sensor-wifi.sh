#!/usr/bin/env bash
# Push Wi-Fi credentials to Tasmota sensors over MQTT. Run on the Pi from /opt/archmon.
# Credentials are read from .env (WIFI_SSID, WIFI_PASSWORD) so they never appear in chat, git or shell history.
#
#   scripts/sensor-wifi.sh list                 # show sensors and their current Wi-Fi
#   scripts/sensor-wifi.sh add <topic|all>      # store network as Wi-Fi slot 2 (current network stays as fallback)
#   scripts/sensor-wifi.sh switch <topic|all>   # add, then tell the sensor to move to slot 2 now
#   scripts/sensor-wifi.sh move <topic|all> <ip> # add, then point MQTT at the Pi's new address and move to slot 2
#
# Tasmota restarts after a Wi-Fi change. If slot 2 fails it falls back to slot 1, so a typo can't strand a sensor.
# The new network must still route to the MQTT broker (<pi-address>:1883).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
MQ="docker exec archmon-mosquitto"
pub() { $MQ mosquitto_pub -u admin -P "$MQTT_ADMIN_PASSWORD" -t "cmnd/$1/$2" -m "$3"; }
topics() {
  if [ "$1" = all ]; then
    $MQ mosquitto_sub -u admin -P "$MQTT_ADMIN_PASSWORD" -t 'tele/+/LWT' -v -W 2 2>/dev/null | awk '$2=="Online"{split($1,a,"/");print a[2]}' | grep -v '^tasmota_\|^sim-' || true
  else echo "$1"; fi
}
case "${1:-}" in
  list)
    for t in $(topics all); do
      (timeout 4 $MQ mosquitto_sub -u admin -P "$MQTT_ADMIN_PASSWORD" -t "stat/$t/STATUS11" -C 1 -W 3 2>/dev/null \
        | grep -o '"SSId":"[^"]*"\|"Signal":[-0-9]*' | tr '\n' ' ' | sed "s/^/$t  /"; echo) &
      sleep 0.3; pub "$t" Status 11; wait
    done ;;
  add|switch|move)
    : "${WIFI_SSID:?Add WIFI_SSID=... to .env first}"; : "${WIFI_PASSWORD:?Add WIFI_PASSWORD=... to .env first}"
    for t in $(topics "${2:?topic or all}"); do
      pub "$t" Password2 "$WIFI_PASSWORD"
      sleep 1
      pub "$t" SSID2 "$WIFI_SSID"
      [ "$1" = switch ] && { sleep 30; pub "$t" Ap 2; }
      [ "$1" = move ] && { sleep 30; pub "$t" Backlog "MqttHost ${3:?new Pi address}; Ap 2"; }
      echo "$t: slot 2 set to '$WIFI_SSID'${1/add/}"
    done ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac

#!/usr/bin/env bash
# Phase 1: create MQTT users, start InfluxDB, mint least-privilege tokens, start everything.
# Run from /opt/archmon as the admin user (member of the docker group). Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
set -a; . ./.env; set +a

# --- Mosquitto password file (hashed) ---
MQ=eclipse-mosquitto:2.0.22
if [ ! -s mosquitto/config/passwd ]; then
  umask 077; : > mosquitto/config/passwd
  for u in telegraf sensor admin; do
    var="MQTT_$(echo $u | tr a-z A-Z)_PASSWORD"
    docker run --rm -v "$PWD/mosquitto/config:/c" $MQ mosquitto_passwd -b /c/passwd "$u" "${!var}"
  done
fi
# mosquitto runs as uid 1883 in the container
docker run --rm -v "$PWD/mosquitto/config:/c" $MQ sh -c 'chown 1883:1883 /c/passwd /c/acl && chmod 600 /c/passwd /c/acl'

# --- InfluxDB + tokens ---
$DC up -d influxdb
for i in $(seq 1 30); do docker exec archmon-influxdb influx ping >/dev/null 2>&1 && break; sleep 2; done
INF="docker exec archmon-influxdb influx"
BID=$($INF bucket list --org "$INFLUX_ORG" --name "$INFLUX_BUCKET_RAW" --hide-headers --token "$INFLUX_ADMIN_TOKEN" | awk '{print $1}')
$INF bucket list --org "$INFLUX_ORG" --name environment_rollup --token "$INFLUX_ADMIN_TOKEN" >/dev/null 2>&1 || \
  $INF bucket create --org "$INFLUX_ORG" --name environment_rollup --retention 0 --token "$INFLUX_ADMIN_TOKEN" >/dev/null
RID=$($INF bucket list --org "$INFLUX_ORG" --name environment_rollup --hide-headers --token "$INFLUX_ADMIN_TOKEN" | awk '{print $1}')

mint() { # description, permission flags...
  local d="$1"; shift
  $INF auth create --org "$INFLUX_ORG" --description "$d" "$@" --hide-headers --json --token "$INFLUX_ADMIN_TOKEN" \
    | grep -o '"token": *"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/'
}
if [ -z "${INFLUX_TELEGRAF_TOKEN}" ]; then
  T=$(mint "telegraf-write" --write-bucket "$BID")
  sed -i "s|^INFLUX_TELEGRAF_TOKEN=.*|INFLUX_TELEGRAF_TOKEN=$T|" .env
fi
if [ -z "${INFLUX_GRAFANA_TOKEN}" ]; then
  T=$(mint "grafana-read" --read-bucket "$BID" --read-bucket "$RID")
  sed -i "s|^INFLUX_GRAFANA_TOKEN=.*|INFLUX_GRAFANA_TOKEN=$T|" .env
fi
unset T

mkdir -p state   # shared with the config container (uid 1000) and scripts/update.sh
$DC up -d
$DC ps
echo "SETUP_OK"

#!/usr/bin/env bash
# Generates .env with random secrets ON THE PI. Never copy .env off the Pi or commit it.
# Will not overwrite an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { echo ".env exists, leaving it alone"; exit 0; }
r() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }
umask 077
cat > .env <<EOF
INFLUX_ADMIN_USER=archmon-admin
INFLUX_ADMIN_PASSWORD=$(r 32)
INFLUX_ADMIN_TOKEN=$(r 64)
INFLUX_ORG=archives
INFLUX_BUCKET_RAW=environment_raw
INFLUX_TELEGRAF_TOKEN=
INFLUX_GRAFANA_TOKEN=
MQTT_TELEGRAF_PASSWORD=$(r 32)
MQTT_SENSOR_PASSWORD=$(r 24)
MQTT_ADMIN_PASSWORD=$(r 32)
GRAFANA_ADMIN_PASSWORD=$(r 20)
GRAFANA_SECRET_KEY=$(r 40)
NTFY_BASE_URL=
EOF
chmod 600 .env
echo "Secrets generated in $(pwd)/.env (mode 600)"

#!/usr/bin/env bash
# Stops the sensor simulator and deletes all simulated data (sensor_id starting "sim-") from InfluxDB.
# Run on the Pi from /opt/archmon. The sim-* rows in registry/sensors.csv can stay (harmless) or be deleted.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
docker compose --profile sim stop simulator 2>/dev/null || true
docker compose --profile sim rm -f simulator 2>/dev/null || true
for id in $(cut -d, -f1 registry/sensors.csv | grep '^sim-'); do
  for b in "$INFLUX_BUCKET_RAW" environment_rollup; do
    docker exec archmon-influxdb influx delete --org "$INFLUX_ORG" --bucket "$b" --token "$INFLUX_ADMIN_TOKEN" \
      --start 1970-01-01T00:00:00Z --stop 2100-01-01T00:00:00Z --predicate "sensor_id=\"$id\""
  done
  echo "deleted $id"
done

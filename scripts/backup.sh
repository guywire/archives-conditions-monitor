#!/usr/bin/env bash
# Nightly backup: InfluxDB (full), Grafana DB, ntfy users, Mosquitto passwd, config and .env.
# Output: /opt/archmon/backups/archmon-YYYYmmdd-HHMM.tar.gz (mode 600, keeps 14).
# The archive contains secrets (.env) — copy it only to a protected location.
# Optional off-device copy: set BACKUP_DEST=/mnt/share/archmon in .env (e.g. a mounted SMB share).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
umask 077
STAMP=$(date +%Y%m%d-%H%M); WORK=$(mktemp -d); OUTDIR=/opt/archmon/backups; mkdir -p "$OUTDIR"
trap 'rm -rf "$WORK"; docker exec archmon-influxdb rm -rf /tmp/bk 2>/dev/null || true' EXIT

docker exec archmon-influxdb influx backup /tmp/bk --token "$INFLUX_ADMIN_TOKEN" >/dev/null
docker cp archmon-influxdb:/tmp/bk "$WORK/influxdb"
docker run --rm -v archmon_grafana-data:/g:ro -v "$WORK":/out eclipse-mosquitto:2.0.22 sh -c "cp /g/grafana.db /out/grafana.db && chown $(id -u):$(id -g) /out/grafana.db"
docker run --rm -v archmon_ntfy-lib:/n:ro -v "$WORK":/out eclipse-mosquitto:2.0.22 sh -c "cp /n/user.db /out/ntfy-user.db 2>/dev/null && chown $(id -u):$(id -g) /out/ntfy-user.db || true"
tar czf "$WORK/config.tar.gz" --exclude=mosquitto/config/passwd --exclude=mosquitto/config/acl docker-compose.yml .env mosquitto telegraf grafana registry ntfy scripts influxdb

tar czf "$OUTDIR/archmon-$STAMP.tar.gz" -C "$WORK" .
ls -1t "$OUTDIR"/archmon-*.tar.gz | tail -n +15 | xargs -r rm -f
if [ -n "${BACKUP_DEST:-}" ] && [ -d "$BACKUP_DEST" ]; then cp "$OUTDIR/archmon-$STAMP.tar.gz" "$BACKUP_DEST/"; fi
echo "backup ok: $OUTDIR/archmon-$STAMP.tar.gz ($(du -h "$OUTDIR/archmon-$STAMP.tar.gz" | cut -f1))"

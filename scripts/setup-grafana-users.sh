#!/usr/bin/env bash
# Creates the read-only 'staff' Grafana login (Viewer role). Password generated into .env. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
grep -q '^GRAFANA_STAFF_PASSWORD=' .env || echo "GRAFANA_STAFF_PASSWORD=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 16)" >> .env
chmod 600 .env
set -a; . ./.env; set +a
G="http://127.0.0.1:3000"; A="admin:$GRAFANA_ADMIN_PASSWORD"
for i in $(seq 1 30); do curl -sf "$G/api/health" >/dev/null && break; sleep 2; done
if curl -sf -u "$A" "$G/api/users/lookup?loginOrEmail=staff" >/dev/null; then
  echo "staff user exists"
else
  curl -sf -u "$A" -H 'Content-Type: application/json' -X POST "$G/api/admin/users" \
    -d "{\"name\":\"Archive staff\",\"login\":\"staff\",\"password\":\"$GRAFANA_STAFF_PASSWORD\"}" >/dev/null && echo "staff user created (Viewer)"
fi

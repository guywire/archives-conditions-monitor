#!/usr/bin/env bash
# Phase 4: ntfy users and topic permissions. Run from /opt/archmon. Idempotent.
#   admin   - full access (web UI / maintenance)
#   grafana - publish-only to archmon-alerts
#   staff   - subscribe-only to archmon-alerts (use this login in the phone app)
#   archmon-sim - simulator alerts; subscribe with the admin login when testing
set -euo pipefail
cd "$(dirname "$0")/.."
r() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-24}"; }
for v in NTFY_ADMIN_PASSWORD NTFY_GRAFANA_PASSWORD NTFY_STAFF_PASSWORD; do
  grep -q "^$v=" .env || echo "$v=$(r 24)" >> .env
done
chmod 600 .env
set -a; . ./.env; set +a
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
$DC up -d ntfy grafana
sleep 3
adduser() { # user role password
  if docker exec archmon-ntfy ntfy user list 2>&1 | grep -q "^user $1 "; then
    docker exec -e NTFY_PASSWORD="$3" archmon-ntfy ntfy user change-pass "$1" >/dev/null
  else
    docker exec -e NTFY_PASSWORD="$3" archmon-ntfy ntfy user add --role="$2" "$1" >/dev/null
  fi
}
adduser admin admin "$NTFY_ADMIN_PASSWORD"
adduser grafana user "$NTFY_GRAFANA_PASSWORD"
adduser staff user "$NTFY_STAFF_PASSWORD"
docker exec archmon-ntfy ntfy access grafana archmon-alerts write-only >/dev/null
docker exec archmon-ntfy ntfy access staff archmon-alerts read-only >/dev/null
# Alerts from simulated sensors (rooms named "... (sim)") go to their own topic so staff are not paged
docker exec archmon-ntfy ntfy access grafana archmon-sim write-only >/dev/null
docker exec archmon-ntfy ntfy user list 2>&1 | grep -v "^$"
echo "NTFY_OK"

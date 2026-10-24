#!/usr/bin/env bash
# Software updates from signed Git release tags (requirement F16). Runs on the host as the admin user, never inside a
# container. Install once with: sudo scripts/setup-updater.sh   (systemd units, signing key, daily check)
#   update.sh check           fetch tags; write state/update-status.json (newest release, its changelog, signature)
#   update.sh apply <tag>     backup, switch to the tag, regenerate dashboards, restart, health check; roll back on failure
#   update.sh run-request     started by archmon-update.path when Grafana (config API) drops state/update-request.json
# Only vX.Y.Z tags signed by a key in state/allowed_signers (git SSH signing) are installed. Needs git, python3,
# curl and Docker Compose v2. Site files (.env, registry, state) are git-ignored, so updates never touch them.
set -euo pipefail

main() {
  cd "$(dirname "$0")/.."
  ROOT=$PWD; STATE=$ROOT/state; mkdir -p "$STATE"
  STATUS=$STATE/update-status.json; LOG=$STATE/update.log; HIST=$STATE/update-history.log; SIGNERS=$STATE/allowed_signers
  exec 9>"$STATE/update.lock"
  flock -n 9 || { echo "another update is running"; exit 1; }
  set -a; . ./.env; set +a
  DC="docker compose"; BY="${BY:-console}"; LATEST=""; SIGNED=no; NOTES=""
  case "${1:-}" in
    check) cmd_check ;;
    apply) cmd_apply "${2:?usage: update.sh apply vX.Y.Z}" ;;
    run-request) cmd_request ;;
    *) sed -n '2,9p' "$0"; exit 1 ;;
  esac
}

log() { echo "$(date '+%F %T')  $*" >>"$LOG"; }
installed() { git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD; }
verify() { [ -s "$SIGNERS" ] && git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$SIGNERS" verify-tag "$1" >/dev/null 2>&1; }
newer() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | tail -1)" = "$1" ]; }   # is $1 newer than $2

scan() {  # newest local release tag, whether it is signed by a trusted key, its CHANGELOG section
  LATEST=$(git tag -l 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true)
  SIGNED=no; NOTES=""
  [ -n "$LATEST" ] || return 0
  verify "$LATEST" && SIGNED=yes
  NOTES=$(git show "$LATEST:CHANGELOG.md" 2>/dev/null | awk -v v="${LATEST#v}" 'index($0, "## " v " ") == 1 || $0 == "## " v {f = 1; next} /^## / {if (f) exit} f' || true)
}

status() {  # state, message -> state/update-status.json (read by the config API for the Settings dashboard)
  U_STATE="$1" U_MSG="$2" U_INSTALLED="$(installed)" U_LATEST="$LATEST" U_SIGNED="$SIGNED" U_NOTES="$NOTES" \
  python3 - "$STATUS" "$HIST" "$LOG" <<'EOF'
import datetime, json, os, sys
status, hist, log = sys.argv[1:4]
def tail(path, n):
    return open(path, encoding="utf-8", errors="replace").read().splitlines()[-n:] if os.path.exists(path) else []
e = os.environ
d = {"state": e["U_STATE"], "message": e["U_MSG"], "installed": e["U_INSTALLED"], "latest": e["U_LATEST"],
     "latest_signed": e["U_SIGNED"] == "yes", "notes": e["U_NOTES"].strip() or "(no changelog entry)",
     "updated_at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
     "history": "\n".join(reversed(tail(hist, 10))) or "No updates yet.", "log": "\n".join(tail(log, 30))}
with open(status + ".tmp", "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False)
os.replace(status + ".tmp", status)
EOF
}

notify() {  # message, priority -> ntfy staff topic (grafana user is publish-only there)
  curl -sf -o /dev/null -u "grafana:$NTFY_GRAFANA_PASSWORD" -H "Title: Archmon software" -H "Priority: ${2:-default}" \
       -d "$1" "http://127.0.0.1:8090/archmon-alerts" || log "ntfy notification failed"
}

die() { log "ERROR: $1"; scan; status failed "$1"; exit 1; }

cmd_check() {
  scan; status checking "Checking for updates..."
  if ! git fetch --quiet --tags --force origin 2>>"$LOG"; then
    scan; status idle "Could not reach the repository (no internet?). Installed: $(installed)."; return 0
  fi
  scan
  local cur msg; cur=$(installed)
  if [ -z "$LATEST" ]; then msg="No releases found."
  elif ! newer "$LATEST" "$cur"; then msg="Up to date ($cur)."   # an untagged commit counts as older than any release
  elif [ "$SIGNED" != yes ]; then msg="$LATEST is available but is not signed by a trusted key, so it will not be installed."
  else
    msg="$LATEST is available. An Admin can install it from Settings."
    if [ "$(cat "$STATE/notified-tag" 2>/dev/null)" != "$LATEST" ]; then
      notify "Version $LATEST is available (installed: $cur). Install it from Grafana → Settings → Software update." low
      echo "$LATEST" >"$STATE/notified-tag"
    fi
  fi
  log "check: $msg"; status idle "$msg"
}

recent_data() {  # device reports since $1 (RFC3339) -> success if any
  docker exec archmon-influxdb influx query --org "$INFLUX_ORG" --token "$INFLUX_ADMIN_TOKEN" --raw \
    "from(bucket: \"$INFLUX_BUCKET_RAW\") |> range(start: $1) |> filter(fn: (r) => r._measurement == \"device_state\") |> limit(n: 1)" \
    2>/dev/null | grep -q ',_result,'
}

healthy() {  # every service running, Grafana and InfluxDB answering, and (if sensors were reporting) new data; up to 5 min
  local since=$1 had=$2 want running
  want=$($DC config --services | sort)
  for _ in $(seq 30); do
    sleep 10
    running=$($DC ps --status running --services | sort)
    [ "$want" = "$running" ] || continue
    curl -sf -o /dev/null http://127.0.0.1:3000/api/health || continue
    docker exec archmon-influxdb influx ping >/dev/null 2>&1 || continue
    [ "$had" = no ] && return 0
    recent_data "$since" && return 0
  done
  log "health check failed. Running: $(echo $running)"
  return 1
}

deploy() {  # git ref -> working tree, dashboards, containers
  git -c advice.detachedHead=false checkout --quiet "$1" || return 1
  python3 scripts/build_dashboards.py >>"$LOG" 2>&1 || return 1
  $DC pull --quiet >>"$LOG" 2>&1 || log "image pull failed; using cached images"
  $DC up -d --remove-orphans >>"$LOG" 2>&1 || return 1
  $DC restart >>"$LOG" 2>&1   # mounted config and code changed; recreate is not triggered by file contents
}

cmd_apply() {
  local tag=$1 prev prev_name had t0
  [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "'$tag' is not a release tag (vX.Y.Z)."
  git rev-parse -q --verify "refs/tags/$tag" >/dev/null || git fetch --quiet --tags --force origin 2>>"$LOG" || true
  git rev-parse -q --verify "refs/tags/$tag" >/dev/null || die "Release $tag not found."
  verify "$tag" || die "$tag is not signed by a trusted key (state/allowed_signers); not installed."
  [ -z "$(git status --porcelain --untracked-files=no)" ] || \
    die "Local changes to tracked files, update refused: $(git status --porcelain --untracked-files=no | head -3 | tr '\n' ' ')"
  prev=$(git rev-parse HEAD); prev_name=$(installed)
  [ "$prev" != "$(git rev-parse "$tag^{commit}")" ] || { scan; status idle "Already on $tag."; return 0; }
  scan; log "update $prev_name -> $tag requested by $BY"
  status updating "Backing up before updating to $tag..."
  scripts/backup.sh >>"$LOG" 2>&1 || die "Backup failed; nothing was changed."
  had=no; recent_data "-10m" && had=yes
  status updating "Installing $tag (containers restart; dashboards may be unavailable for a minute)..."
  t0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if deploy "$tag" && healthy "$t0" "$had"; then
    echo "$(date '+%F %R %Z')  $BY  $prev_name -> $tag  updated" >>"$HIST"
    log "updated to $tag"; scan; status updated "Updated to $tag."
    notify "Updated $prev_name → $tag (by $BY)." low
    return 0
  fi
  log "update to $tag failed; rolling back to $prev_name"
  status updating "Update to $tag failed; rolling back to $prev_name..."
  t0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if deploy "$prev" && healthy "$t0" "$had"; then
    echo "$(date '+%F %R %Z')  $BY  $prev_name -> $tag  FAILED, rolled back" >>"$HIST"
    scan; status rolled_back "Update to $tag failed the health check and was rolled back to $prev_name. See the log below."
    notify "Update to $tag failed and was rolled back to $prev_name." high
  else
    echo "$(date '+%F %R %Z')  $BY  $prev_name -> $tag  FAILED, rollback unhealthy" >>"$HIST"
    scan; status failed "Update to $tag failed and the rollback to $prev_name is not healthy either. Check 'docker compose ps' on the server."
    notify "Update to $tag failed and the rollback is unhealthy. Check the server." high
  fi
  return 1
}

cmd_request() {  # request written by config/targets_api.py: {"action": "check"|"install", "tag": "...", "user": "..."}
  local req=$STATE/update-request.json action tag user
  [ -f "$req" ] || return 0
  read -r action tag user < <(python3 - "$req" <<'EOF' || echo "bad - -"
import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
clean = lambda v, pat: v if isinstance(v, str) and re.fullmatch(pat, v) else "-"
print(clean(d.get("action"), "check|install"), clean(d.get("tag", "-"), r"v\d+\.\d+\.\d+|-"), clean(d.get("user"), r"[\w.@-]{1,64}"))
EOF
)
  rm -f "$req"
  BY="grafana:$user"
  case "$action" in
    check) cmd_check ;;
    install) cmd_apply "$tag" ;;
    *) log "ignored malformed update request"; scan; status idle "Ignored a malformed request." ;;
  esac
}

main "$@"; exit $?

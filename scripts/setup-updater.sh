#!/usr/bin/env bash
# Installs the Grafana-driven updater (requirement F16): trusted signing key, systemd units, daily check. Idempotent.
# Run as root from the install folder (must be a Git checkout; see scripts/convert-to-git.sh for older copies):
#   sudo scripts/setup-updater.sh <admin-user> <signer-email> <signer-public-key-file>
#   e.g. sudo scripts/setup-updater.sh pi5 me@example.org /home/pi5/archmon_signing.pub
# The key is the public half of the SSH key you sign release tags with (git tag -s, gpg.format=ssh).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
ADMIN="${1:?usage: setup-updater.sh <admin-user> <signer-email> <public-key-file>}"
EMAIL="${2:?signer email (as in your git config user.email)}"
KEY="${3:?public key file, e.g. ~/.ssh/archmon_signing.pub}"
[ -d .git ] || { echo "$ROOT is not a Git checkout; run scripts/convert-to-git.sh first."; exit 1; }
grep -qE '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-|sk-)' "$KEY" || { echo "$KEY does not look like an SSH public key."; exit 1; }
command -v git >/dev/null && command -v python3 >/dev/null || apt-get install -y -q git python3

install -d -o "$ADMIN" -g "$ADMIN" -m 755 state
printf '%s namespaces="git" %s\n' "$EMAIL" "$(cut -d' ' -f1-2 "$KEY")" >state/allowed_signers
chown "$ADMIN:$ADMIN" state/allowed_signers; chmod 644 state/allowed_signers

cat >/etc/systemd/system/archmon-update.path <<EOF
[Unit]
Description=Archmon: run an update requested from Grafana
[Path]
PathExists=$ROOT/state/update-request.json
Unit=archmon-update.service
[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/archmon-update.service <<EOF
[Unit]
Description=Archmon: software update / check requested from Grafana
After=docker.service
[Service]
Type=oneshot
User=$ADMIN
WorkingDirectory=$ROOT
ExecStart=$ROOT/scripts/update.sh run-request
TimeoutStartSec=30min
EOF
cat >/etc/systemd/system/archmon-update-check.service <<EOF
[Unit]
Description=Archmon: check for a new signed release
After=network-online.target docker.service
[Service]
Type=oneshot
User=$ADMIN
WorkingDirectory=$ROOT
ExecStart=$ROOT/scripts/update.sh check
EOF
cat >/etc/systemd/system/archmon-update-check.timer <<'EOF'
[Unit]
Description=Archmon: daily release check
[Timer]
OnCalendar=*-*-* 03:30
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now archmon-update.path archmon-update-check.timer
sudo -u "$ADMIN" "$ROOT/scripts/update.sh" check || true
cat state/update-status.json 2>/dev/null | head -c 400; echo
echo "UPDATER_OK"

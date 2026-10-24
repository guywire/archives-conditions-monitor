#!/usr/bin/env bash
# Phase 0: harden the Pi and install Docker. Run as root (sudo). Idempotent.
# Usage: sudo LAN_CIDR=<lan>/24 bash bootstrap.sh <admin-user>
set -euo pipefail
ADMIN_USER="${1:?usage: bootstrap.sh <admin-user>}"
LAN_CIDR="${LAN_CIDR:?set LAN_CIDR to the staff LAN, e.g. 10.0.0.0/24}"
export DEBIAN_FRONTEND=noninteractive

apt-get update -q
apt-get -y -q full-upgrade
apt-get -y -q install docker.io docker-compose ufw unattended-upgrades fail2ban apparmor openssl git python3 curl

# Automatic security updates
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# Unneeded network service
systemctl disable --now rpcbind.service rpcbind.socket 2>/dev/null || true

# Host firewall (covers SSH and anything not published by Docker)
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$LAN_CIDR" to any port 22 proto tcp
ufw --force enable

# Docker-published ports bypass ufw: restrict them in DOCKER-USER to the LAN.
cat >/usr/local/sbin/archmon-docker-fw <<EOF
#!/bin/sh
iptables -N DOCKER-USER 2>/dev/null || true
iptables -F DOCKER-USER
iptables -A DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A DOCKER-USER -s 172.16.0.0/12 -j RETURN
iptables -A DOCKER-USER -s $LAN_CIDR -j RETURN
iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 1883 -j DROP
iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 3000 -j DROP
iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 8090 -j DROP
iptables -A DOCKER-USER -j RETURN
EOF
chmod 755 /usr/local/sbin/archmon-docker-fw
cat >/etc/systemd/system/archmon-docker-fw.service <<'EOF'
[Unit]
Description=Archmon: restrict Docker-published ports to LAN
After=docker.service
Requires=docker.service
PartOf=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/archmon-docker-fw
RemainAfterExit=yes
[Install]
WantedBy=docker.service
EOF

# SSH hardening (password login stays ON until the admin confirms their own key works)
cat >/etc/ssh/sshd_config.d/10-archmon.conf <<'EOF'
PermitRootLogin no
MaxAuthTries 4
X11Forwarding no
EOF
systemctl reload ssh

usermod -aG docker "$ADMIN_USER"
systemctl enable --now docker fail2ban
systemctl daemon-reload
systemctl enable --now archmon-docker-fw.service

install -d -o "$ADMIN_USER" -g "$ADMIN_USER" -m 750 /opt/archmon
echo "BOOTSTRAP_OK"
docker --version; (docker compose version 2>/dev/null || docker-compose version)

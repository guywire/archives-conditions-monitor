#!/usr/bin/env bash
# Join the Pi to the Wi-Fi network in /opt/archmon/.env (WIFI_SSID, WIFI_PASSWORD) and make the firewall follow
# the Pi's own private subnet, so SSH, Grafana, MQTT and ntfy keep working on the new network.
#
#   sudo scripts/pi-wifi.sh        # save the network: used when the current one disappears, or after a reboot
#   sudo scripts/pi-wifi.sh now    # also switch now; goes back to the previous network if it fails to connect
#
# The password goes straight into a root-only NetworkManager file, never onto a command line.
# Only private subnets (10/8, 172.16/12, 192.168/16) are ever opened; old ones stay listed in /etc/archmon/lan-cidrs.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
cd "$(dirname "$0")/.."
val() { grep -m1 "^$1=" .env | cut -d= -f2- || true; }
SSID=$(val WIFI_SSID); PSK=$(val WIFI_PASSWORD)
[ -n "$SSID" ] && [ -n "$PSK" ] || { echo "Add WIFI_SSID=... and WIFI_PASSWORD=... to .env first."; exit 1; }
case "$SSID$PSK" in *\\*|*\;*) echo "Backslash or ; in the SSID/password: add the network with nmtui instead."; exit 1 ;; esac

# 1. NetworkManager profile, preferred over the current network (priority 10 vs 0).
#    2.4 GHz, plain WPA2, no PMF: the Pi's brcmfmac chip was refused (status 16) on a GL.iNet 5 GHz radio.
f=/etc/NetworkManager/system-connections/archmon-wifi.nmconnection
uuid=$(sed -n 's/^uuid=//p' "$f" 2>/dev/null || true)
umask 077
cat >"$f" <<EOF
[connection]
id=archmon-wifi
uuid=${uuid:-$(cat /proc/sys/kernel/random/uuid)}
type=wifi
interface-name=wlan0
autoconnect=true
autoconnect-priority=10

[wifi]
mode=infrastructure
ssid=$SSID
band=bg

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
pairwise=ccmp
pmf=1
psk=$PSK

[ipv4]
method=auto

[ipv6]
method=auto
EOF
chmod 600 "$f"
nmcli connection reload
echo "Saved Wi-Fi network '$SSID' (profile archmon-wifi)."

# 2. Firewall follows the Pi's subnet: allowed LANs live in /etc/archmon/lan-cidrs
umask 022
mkdir -p /etc/archmon
touch /etc/archmon/lan-cidrs
cat >/usr/local/sbin/archmon-docker-fw <<'EOF'
#!/bin/sh
# Docker-published ports bypass ufw: allow 1883/3000/8090 only from the LANs in /etc/archmon/lan-cidrs
iptables -N DOCKER-USER 2>/dev/null || true
iptables -F DOCKER-USER
iptables -A DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A DOCKER-USER -s 172.16.0.0/12 -j RETURN
for net in $(grep -E '^[0-9./]+$' /etc/archmon/lan-cidrs); do iptables -A DOCKER-USER -s "$net" -j RETURN; done
iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 1883 -j DROP
iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 3000 -j DROP
iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport 8090 -j DROP
iptables -A DOCKER-USER -j RETURN
EOF
cat >/etc/NetworkManager/dispatcher.d/90-archmon-lan <<'EOF'
#!/bin/sh
# Archmon: when wlan0/eth0 comes up on a private subnet, allow it (SSH via ufw, Docker ports via DOCKER-USER)
case "$2" in up|dhcp4-change) ;; *) exit 0 ;; esac
case "$1" in wlan*|eth*) ;; *) exit 0 ;; esac
for net in $(ip -o -4 route show dev "$1" proto kernel scope link | awk '{print $1}'); do
  case "$net" in 10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) ;; *) continue ;; esac
  grep -qxF "$net" /etc/archmon/lan-cidrs || { echo "$net" >>/etc/archmon/lan-cidrs; logger -t archmon "allowing LAN $net"; }
  ufw allow from "$net" to any port 22 proto tcp >/dev/null
done
/usr/local/sbin/archmon-docker-fw
EOF
chmod 755 /usr/local/sbin/archmon-docker-fw /etc/NetworkManager/dispatcher.d/90-archmon-lan
/usr/local/sbin/archmon-docker-fw
echo "Firewall now follows the Pi's subnet. Allowed LANs: $(tr '\n' ' ' </etc/archmon/lan-cidrs)"

# 3. Optional: move everything now. Runs in the background on the Pi, so it survives the SSH session dropping:
#    join the new network to learn the Pi's address there, go back to the old one, tell the sensors that address
#    and send them to Wi-Fi slot 2, then move the Pi for good and update ntfy's base-url.
if [ "${1:-}" = now ]; then
  prev=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: '$2=="wlan0"{print $1}')
  log=/var/log/archmon-wifi.log
  echo "Moving the Pi and sensors to '$SSID' in the background (about 4 minutes). Your SSH session may drop."
  echo "Progress: $log (or after the move: http://<new address>:3000)."
  cat >/run/archmon-migrate.sh <<EOF
set -u
say() { echo "\$(date '+%H:%M:%S') \$*"; }
say "joining '$SSID'"
if ! nmcli --wait 60 connection up archmon-wifi; then
  say "could not join '$SSID'; staying on '$prev'"; journalctl -n 15 -u wpa_supplicant --no-pager
  nmcli --wait 60 connection up '$prev'; exit 1
fi
sleep 5
ip=\$(ip -4 -o addr show wlan0 | awk '{split(\$4,a,"/"); print a[1]; exit}')
say "Pi address on '$SSID': \$ip"
[ -n "\$ip" ] || { say "no address; going back"; nmcli --wait 60 connection up '$prev'; exit 1; }
say "back to '$prev' to brief the sensors"
nmcli --wait 60 connection up '$prev'
sleep 60   # sensors reconnect to MQTT
cd $(pwd)
scripts/sensor-wifi.sh move all "\$ip"
sleep 5
say "moving the Pi to '$SSID' for good"
nmcli --wait 60 connection up archmon-wifi
sed -i "s#^base-url: .*#base-url: \"http://\$ip:8090\"#" ntfy/server.yml && docker restart archmon-ntfy >/dev/null
say "done. Dashboards: http://\$ip:3000  ntfy server: http://\$ip:8090"
EOF
  nohup sh /run/archmon-migrate.sh >"$log" 2>&1 &
fi

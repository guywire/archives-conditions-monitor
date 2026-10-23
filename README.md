# Archival Conditions Monitor

Monitors temperature and relative humidity in archive storage rooms, compares each room with archival targets,
and alerts staff by phone (ntfy). Runs on a Raspberry Pi 5 with Mosquitto (MQTT), Telegraf, InfluxDB and Grafana in Docker.

- Requirements: https://claude.ai/code/artifact/6d8efa7b-0632-405d-9ed2-1bbfcc2cb5a0
- Current state, versions and to-dos: [STATUS.md](STATUS.md) · history: [CHANGELOG.md](CHANGELOG.md)

## Everyday use

`<pi-address>` below is the Pi's fixed LAN address (site details are kept in the local-only STATUS.md).

| What | Where |
| --- | --- |
| Dashboards | http://<pi-address>:3000 (login `staff`, read-only) |
| Wall screen / no menus | http://<pi-address>:3000/d/archmon-overview?kiosk&refresh=1m |
| Monthly report | Dashboards → 3 Monthly Report (defaults to this month so far; time picker → "Previous month" for a finished month; print from kiosk view) |
| Change targets | Dashboards → 6 Settings (Editors/Admins) |
| Phone alerts | ntfy app → server `http://<pi-address>:8090`, user `staff`, topic `archmon-alerts` (site network only) |

Passwords are never stored in this folder. They live only on the Pi in `/opt/archmon/.env` (readable by the admin user only):
`grep -E 'GRAFANA_STAFF|NTFY_STAFF|TASMOTA_WEB' /opt/archmon/.env`

## How data flows

Sensor (Tasmota, MQTT topic `<sensor_id>_<MAC>`) → Mosquitto → Telegraf (adds room from `registry/sensors.csv`,
target band from `registry/room_profiles.csv`, calibration, dew point, status 0/1/2) → InfluxDB (`environment_raw`, 2 years;
`environment_rollup`, 15-min, forever) → Grafana dashboards and alert rules → ntfy push.

Data is stored in °C; dashboards and alerts show °F.

## Common tasks

All commands run on the Pi (`ssh <user>@<pi-address>`) in `/opt/archmon`, unless noted. Files are edited in this
folder on the admin PC and copied to the Pi (see "Deploying changes").

### Add a sensor
1. Join the ST01 to Wi-Fi (its setup hotspot) and set MQTT: host `<pi-address>`, port 1883, user `sensor`
   (password: `grep MQTT_SENSOR /opt/archmon/.env`), topic `<sensor_id>_%06X`, e.g. `bldga-f1-vault-01_%06X`.
2. Make sure the sensor_id has a row in `registry/sensors.csv` (site, building, floor, room, room_type).
3. `scripts/setup-sensor.sh <full topic>` (e.g. `bldga-f1-vault-01_A1B2C3`): pin template, 60 s reporting, LED, web password.
4. `docker restart archmon-telegraf` if you changed the registry.

### Calibrate a sensor
Calibration offsets live in **one place**: `cal_t` (°C) and `cal_rh` (%RH) in `registry/sensors.csv`, added by Telegraf
(`telegraf/normalize.star`) before data is stored. Do not also set Tasmota's `TempOffset`/`HumOffset` on the sensor,
or the correction is applied twice. Offset = reference − sensor reading (°C; a °F difference × 5/9). Only new readings
change; history is not rewritten.

### Move a sensor to another room
Change its Tasmota topic (MQTT command `Topic <new_id>_%06X`) or edit its row in `registry/sensors.csv`, then restart Telegraf.

### Change target ranges (what counts as Watch/Alarm)
Grafana → **7 Settings** (Grafana Editors/Admins only). Edit a room type's range, or give one room
an override (blank = room-type value), then **Save targets**. Applies within about a minute; every change is logged
with the user name in `registry/targets-changes.log`. How it works: nginx (`proxy/`) serves Grafana and the editor API
(`config/targets_api.py`, at `/archmon-api/`) on port 3000; the API checks the user's Grafana session, writes
`registry/room_profiles.csv` / `room_overrides.csv` (°C), and Telegraf reloads itself (`--watch-config`).
Watch/Alarm margins: `telegraf/normalize.star`. Alert delays: `scripts/build_dashboards.py` → `alerting()`.

### Simulated sensors (demo / testing)
`docker compose --profile sim up -d simulator` starts 12 fake sensors (`sim/simulator.py`): temperature/RH for the
rooms without real sensors, 3 water-leak probes and 3 air-quality sensors (SEN55 particles/VOC/NOx, SCD40 CO2). They
appear in rooms named "… (sim)" and their alerts go to ntfy topic `archmon-sim` (subscribe with the ntfy admin login),
never to staff. Built-in scenarios: map room humid (Watch), Building B too warm in the afternoon (Alarm), cold-room
door openings, Building C dusty with a daily VOC spike, cold-room leak for 20 min every 12 h.
Remove everything with `scripts/sim-clean.sh` (stops it and deletes all `sim-*` data).

### Leak and air-quality sensors
Tasmota payloads are recognised automatically: `Switch1` ON = water (leak probe on a switch input), and SEN5X, SCD40,
PMS5003 or SGP30 readings become an `air_quality` record. Limits are `AQ_LIMITS` in `telegraf/normalize.star`
(the dashboards read them from there). See the "Leaks & Air Quality" dashboard.

### Change sensor Wi-Fi
Add `WIFI_SSID=` and `WIFI_PASSWORD=` to `/opt/archmon/.env`, then `scripts/sensor-wifi.sh switch all`
(new network goes in slot 2; the old one stays as fallback).

### Website monitor (image download speed)
The `archmon-webmon` container downloads images listed in `registry/web_targets.csv` (one row each: name, URL, average
minutes between checks, ± jitter %, cache_bust) at randomised times, one at a time, and records DNS, connect, TLS,
server-wait and transfer times, size, speed and failures. Grafana → **6 Website**. Edits to the CSV apply within
30 s; the file is not overwritten by the deploy below once edited on the Pi (exclude it like the target files if you
edit it there). Start it with `docker compose up -d webmon`; logs: `docker logs archmon-webmon`. Include at least one
large image (1 MB+): small files finish before a connection reaches full speed, so their Mbit/s is not meaningful.
Results describe this site's internet link as much as the website, so compare with a check from elsewhere before
blaming the web host.

### Site data (kept out of Git)
Room names, sensor list, per-room overrides, the targets change log, website targets and STATUS.md describe the
real site, so they are git-ignored and live only on the admin PC and the Pi. A fresh clone gets them from the
templates: `for f in sensors room_overrides web_targets; do cp registry/$f.example.csv registry/$f.csv; done`, then
edit `registry/sensors.csv`. Set `NTFY_BASE_URL=http://<pi-address>:8090` in `.env`.

### Deploying changes (from the admin PC, Git Bash)
```
python scripts/build_dashboards.py          # after editing dashboards/alerts or room_profiles.csv
tar czf - --exclude='registry/room_profiles.csv' --exclude='registry/room_overrides.csv' --exclude='registry/targets-changes.log'     grafana registry telegraf webmon docker-compose.yml | ssh archmon-pi 'cd /opt/archmon && tar xzf -'
ssh archmon-pi 'docker restart archmon-grafana'     # Telegraf reloads by itself when telegraf.conf changes
```
Targets are edited on the Pi (Settings dashboard), so the Pi's copies of `room_profiles.csv`, `room_overrides.csv` and
`targets-changes.log` are the master; the deploy above never overwrites them. Pull them back with
`scp archmon-pi:/opt/archmon/registry/room_* archmon-pi:/opt/archmon/registry/targets-changes.log registry/`.
Dashboards and alert rules are generated: edit `scripts/build_dashboards.py`, not the JSON/YAML files.

### Backups and restore
Nightly 02:20 → `/opt/archmon/backups/archmon-<date>.tar.gz` (14 kept; contains secrets — protect it).
Set `BACKUP_DEST=/path/to/mounted/share` in `.env` for an off-device copy. Restore onto a fresh Pi:
1. Run `scripts/bootstrap.sh` (sudo), unpack `config.tar.gz` into `/opt/archmon` (brings back `.env`).
2. `scripts/setup-stack.sh` (recreates MQTT users from `.env`), then stop InfluxDB data and restore:
   `docker cp influxdb archmon-influxdb:/tmp/restore && docker exec archmon-influxdb influx restore /tmp/restore --full --token <INFLUX_ADMIN_TOKEN from .env>`
3. Copy `grafana.db` into the `archmon_grafana-data` volume and `ntfy-user.db` into `archmon_ntfy-lib`, then `docker compose up -d`.

### Moving to a PC or VM instead of the Pi
Everything is in `docker-compose.yml`. Install Docker, copy the folder plus a backup, and follow the restore steps.
Update the IP in `ntfy/server.yml` and point sensors' MqttHost at the new machine.

## Security notes
- No default passwords; all secrets generated on the Pi. Services are reachable from the Pi's own LAN(s) only (/etc/archmon/lan-cidrs, follows the Pi's subnet)
  (ufw + DOCKER-USER rules); InfluxDB only from the Pi itself.
- MQTT requires a login; sensors may only publish telemetry. ntfy denies anonymous access.
- Grafana staff accounts are read-only. Tasmota web pages require the `admin` login.

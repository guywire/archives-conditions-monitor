# Changelog

## 0.8.1 — 2026-10-23
- README screenshots (docs/screenshots, simulated data in example buildings).
- README rewritten around the Linux/Docker stack: requirements, step-by-step install (incl. the rollup task,
  which no script created), updating with `git pull`; Raspberry Pi specifics moved to their own section.
- Fix: Website dashboard "Failed checks per hour" and "Recent failures" panels failed (Flux type conflicts).
- Fix: Sensor Health drift check treated same-named rooms in different buildings (e.g. two "Stacks") as one room.

## 0.8.0 — 2026-10-23 (not yet deployed: Pi offline)
- Website monitor: `webmon/webmon.py` (stdlib, compose service `webmon`, uid 1000, no ports/secrets) downloads the
  images in `registry/web_targets.csv` on a randomised schedule (interval ± jitter, serial), records DNS / connect /
  TLS / server wait / transfer / total time, size, Mbit/s, HTTP status and failure cause as `web_probe`.
- Telegraf: internal `http_listener_v2` on :8186 (not published, accepts `web_probe` only).
- New dashboard "6 Website" (summary stats, per-image table, time and speed trends, time breakdown, failures).
  Settings moves to "7 Settings".
- README: calibration is applied only from `registry/sensors.csv` (don't also set offsets on the sensor).
- Site data out of Git: STATUS.md, registry sensors/overrides/web targets/change log and the generated Settings
  dashboard are git-ignored; `registry/*.example.csv` templates instead. ntfy base URL now from `.env`
  (`NTFY_BASE_URL`); `bootstrap.sh` requires `LAN_CIDR`.


## 0.7.1 — 2026-10-18
- `scripts/pi-wifi.sh` (sudo): saves the new Wi-Fi network for the Pi from .env; the firewall now follows the Pi's private subnet (`/etc/archmon/lan-cidrs`, NetworkManager dispatcher hook) so a network move can't lock out SSH/Grafana/MQTT/ntfy.
- `sensor-wifi.sh move <topic|all> <ip>`: point sensors at a new Pi address and switch Wi-Fi in one step; simulator topics are skipped.
- Sensor 2 calibrated (cal_t).

## 0.7.0 — 2026-10-17
- Target editor in Grafana: "Settings" dashboard (Business Forms 6.3.5, pinned) for room-type targets and per-room
  overrides; API `config/targets_api.py` (stdlib, uid 1000, no secrets) checks the user's Grafana session (Editor/Admin
  to save), validates, writes registry CSVs, logs every change (`registry/targets-changes.log`), rebuilds dashboards.
- nginx 1.30.6 proxy now owns port 3000 (Grafana + `/archmon-api/` on one origin; place for TLS later).
- Telegraf: per-room overrides lookup (`room_overrides.csv`, `o_*` tags); registry mounted as a folder; `--watch-config poll`.
- Room Detail: Building selector (room names repeat across buildings), building in title, leak/air-quality panels.
- Overview charts and status history label rooms "Building · Room" so same-named rooms no longer merge.
- Monthly Report defaults to this month so far (last month had no data yet).

## 0.6.0 — 2026-10-17
- Sensor simulator (compose profile `sim`, stdlib Python, 12 sensors) with archival scenarios; `scripts/sim-clean.sh`.
- Water-leak (`leak` measurement) and air-quality (`air_quality`: PM2.5, PM10, CO2, VOC index, NOx index) support in Telegraf.
- New dashboard "Leaks & Air Quality"; leak/air-quality table on Overview; rules panel lists AQ limits.
- Alerts: Water leak (immediate), Air quality alarm (30 min) / watch (2 h); offline rule covers all sensor kinds.
- Alerts from "(sim)" rooms route to ntfy topic `archmon-sim`.
- Fix: "Sensor needs checking" fired on new sensors with few readings; now needs 300+ readings in 6 h.
- Rollup task keeps 15-min air-quality and leak history.

## 2026-10-17 — v0.5.0
- ntfy custom template `archmon`: alarm = high priority with "ALARM:" title, watch/sensor = default, resolved = low.
- Room watch now stays active while a room is in alarm (no misleading "resolved" when conditions worsen).
- New alert: large daily RH swing (24 h range above 2 × room profile's rh_swing_24h). Telegraf stores rh_swing_max.
- Tables: Last reading column was invisible (transparent text) — fixed; columns ordered building, floor, room, status,
  temp, RH, dew point, targets, why; rows sorted by location. Same ordering applied to report and health tables.
- scripts/setup-sensor.sh: one-step ST01 setup incl. Tasmota web password (TASMOTA_WEB_PASSWORD). Applied to both sensors.
- LEDs re-enabled (WS2812 on GPIO21, 1 pixel, dim green).
- README.md operator guide.

## 2026-10-17 — v0.4.0
- Second sensor live. scripts/sensor-wifi.sh for Wi-Fi changes over MQTT (slot 2 with fallback).
- Grafana: Overview is the home page; read-only `staff` login; room names link to Room Detail; kiosk URL.
- Monthly Report dashboard: % time in range / alarm, TWPI (Sebera isoperm), hours RH > 65 %, min/mean/max, data coverage.
- Sensor Health: drift (peer deviation) and plausibility (stuck/saturated) table; room/device columns fixed.
- Alerts added: Mould risk (RH > 65 % for 48 h), Sensor needs checking (RH flat 6 h). All templates verified rendering.
- InfluxDB task rollup_15m → environment_rollup. Nightly backup (cron 02:20, 14 kept).
- Security: InfluxDB operator token rotated (old one exposed in a debug trace and deleted).

## 2026-10-17 — v0.3.0
- Display in °F (storage stays °C). Overview table shows Temp/RH targets and a plain-language 'Why' column; 'Alarm rules and target ranges' panel generated from room_profiles.csv; Room Detail shows target range.
- Telegraf writes t_status / rh_status separately.
- ntfy v2.31.1 self-hosted (deny-all default; users admin/grafana/staff); firewall port 8090 LAN only.
- Grafana alerting provisioned (generated): Room alarm → high priority, Room watch, Sensor offline → ntfy.

## 2026-10-17 — v0.2.0
- First real sensor: IoTorero ST01 (Tasmota 16.0.0.1, ESP32-C3, SHT40). Applied SHT40 I2C template (SCL 8, SDA 10).
- Telegraf: regex splits Tasmota `%06X` MAC suffix into `device_id`; room profile lookup adds target band fields and `status` (0 OK / 1 watch / 2 alarm).
- Dashboards v1: Overview (status table, status timeline, T/RH/dew point with group-by), Room Detail (bands, 24 h RH swing), Sensor Health (Wi-Fi, uptime, last report).
- Telegraf `skip_processors_after_aggregators = false` set explicitly.

## 2026-10-16 — v0.1.0
- Requirements document drafted (Claude Docs).
- Pi bootstrap: full-upgrade, Docker 26.1.5, Compose 2.26.1, ufw, fail2ban, unattended-upgrades, DOCKER-USER LAN filter, SSH hardening.
- Stack deployed: Mosquitto 2.0.22, InfluxDB 2.7.12, Telegraf 1.40.2, Grafana 13.2.4.
- MQTT auth + ACLs (telegraf/sensor/admin), InfluxDB buckets `environment_raw` (730 d) and `environment_rollup` (infinite), write-only Telegraf token, read-only Grafana token.
- Telegraf pipeline: Tasmota JSON → registry tags (`registry/sensors.csv`) → calibration offsets → dew point → `environment` measurement. Device STATE (RSSI, uptime) → `device_state`.
- Registry seeded with 8 rooms across 3 buildings.

"""Generates Grafana dashboards (grafana/dashboards/) and alert rules (grafana/provisioning/alerting/).
Run: python scripts/build_dashboards.py
Data is stored in °C; dashboards and alerts display °F."""
import ast, csv, json, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "grafana" / "dashboards"
ALERT_OUT = ROOT / "grafana" / "provisioning" / "alerting" / "archmon-alerts.yml"
DS = {"type": "influxdb", "uid": "archmon-influx"}
B = "environment_raw"
T_MARGIN_F, RH_MARGIN = 1.8, 5   # must match telegraf/normalize.star (1 °C, 5 %RH)
OK, WATCH, ALARM, GREY = "#2E8540", "#E3A008", "#C81E1E", "#7A8B99"
TEMP_LINE = "#1F6FB2"
FILTERS = ('  |> filter(fn: (r) => contains(value: r.site, set: ${site:json}))\n'
           '  |> filter(fn: (r) => contains(value: r.room_type, set: ${room_type:json}))\n'
           '  |> filter(fn: (r) => contains(value: r.room, set: ${room:json}))\n')
TO_F = '  |> map(fn: (r) => ({r with _value: r._value * 1.8 + 32.0}))\n'
STATUS_MAP = [{"type": "value", "options": {
    "0": {"text": "OK", "color": OK, "index": 0},
    "1": {"text": "Watch", "color": WATCH, "index": 1},
    "2": {"text": "Alarm", "color": ALARM, "index": 2}}}]
ROOM_TYPE_NAMES = {"general_storage": "General storage (stacks, map room)", "vault": "Vault", "chilled": "Cold room",
                   "humidification": "Humidification room", "cold_vault": "Freezer", "test": "Test bench (no alerts)",
                   "unregistered": "Unregistered"}
ROOM_TYPE_TEXT = [{"type": "value", "options": {k: {"text": v.split(" (")[0]} for k, v in ROOM_TYPE_NAMES.items()}}]

ROOM_LINK = [{"title": "Open room detail",
              "url": "/d/archmon-room?var-one_building=${__data.fields.building}&var-one_room=${__value.text}&${__url_time_range}"}]
ROOM_FILT = '  |> filter(fn: (r) => r.building == "${one_building}" and r.room == "${one_room}")\n'   # Room Detail
# Preservation Index (years), Sebera isoperm style: 45 y at 20 °C / 50 % RH, Ea ≈ 100 kJ/mol (Ea/R ≈ 12028 K)
PI_FLUX = 'pi = (t, h) => 45.0 * (50.0 / h) * math.exp(x: 12028.0 * (1.0 / (t + 273.15) - 1.0 / 293.15))\n'

# Air-quality limits live in telegraf/normalize.star (single source of truth): field -> (key, watch, alarm)
AQ_LIMITS = ast.literal_eval(next(l for l in (ROOT / "telegraf" / "normalize.star").read_text(encoding="utf-8").splitlines()
                                  if l.startswith("AQ_LIMITS =")).split("=", 1)[1].strip())
AQ_INFO = {"pm25": ("PM2.5", "µg/m³", "Fine dust; soils and abrades surfaces, carries acidic pollutants"),
           "pm10": ("PM10", "µg/m³", "Coarse dust"),
           "co2_ppm": ("CO2", "ppm", "Ventilation / occupancy indicator"),
           "voc_index": ("VOC index", "", "Sensirion 1–500 scale, 100 = typical; off-gassing, cleaning products, paints"),
           "nox_index": ("NOx index", "", "Sensirion 1–500 scale, 1 = clean; NO2 from traffic/combustion attacks paper and pigments")}
SIM_ROOM_RE = ".*\\(sim\\)"   # simulated rooms are named "... (sim)"; their alerts go to ntfy topic archmon-sim

def f(c): return float(c) * 1.8 + 32
def q(flux, ref="A"): return {"refId": ref, "datasource": DS, "query": flux}
def by_name(name, *props): return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": k, "value": v} for k, v in props]}

def tagvar(name, label, tag, pred="", multi=True):
    flux = ('import "influxdata/influxdb/schema"\n'
            f'schema.tagValues(bucket: "{B}", tag: "{tag}", start: -30d,\n'
            f'  predicate: (r) => r._measurement == "environment" and r.room_type != "unregistered"{pred})')
    return {"name": name, "label": label, "type": "query", "datasource": DS, "query": flux,
            "refresh": 2, "multi": multi, "includeAll": multi, "current": {}, "sort": 1}

GROUPBY = {"name": "group_by", "label": "Group by", "type": "custom", "query": "room,room_type,floor,building,site,sensor_id",
           "current": {"text": "room", "value": "room"},
           "options": [{"text": v, "value": v, "selected": v == "room"} for v in "room,room_type,floor,building,site,sensor_id".split(",")]}

def series(field, to_f=False, agg="mean", meas="environment", filt=FILTERS, per_sensor=False):
    if per_sensor:
        grp, label = '["sensor_id"]', 'r.sensor_id'
    else:  # "room" also groups by building: several buildings have a room called "Stacks"
        grp = 'if "${group_by}" == "room" then ["building", "room"] else ["${group_by}"]'
        label = 'if "${group_by}" == "room" then r.building + " · " + r.room else string(v: r["${group_by}"])'
    return (f'from(bucket: "{B}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
            f'  |> filter(fn: (r) => r._measurement == "{meas}" and r._field == "{field}")\n' + filt +
            f'  |> group(columns: {grp})\n'
            f'  |> aggregateWindow(every: v.windowPeriod, fn: {agg}, createEmpty: false)\n' + (TO_F if to_f else '') +
            f'  |> map(fn: (r) => ({{_time: r._time, _value: r._value, _field: {label}}}))\n'
            '  |> group(columns: ["_field"])')

def ts(title, field, unit, y, h=9, to_f=False, desc="", minv=None, maxv=None, meas="environment", limits=None, w=24, x=0, **kw):
    d = {"unit": unit, "decimals": 1, "color": {"mode": "palette-classic-by-name"},
         "custom": {"lineWidth": 2, "fillOpacity": 0, "showPoints": "never", "spanNulls": 600000, "axisSoftMin": minv, "axisSoftMax": maxv}}
    if limits:  # dashed watch/alarm lines
        d["thresholds"] = {"mode": "absolute", "steps": [{"color": "transparent", "value": None}, {"color": WATCH, "value": limits[0]}, {"color": ALARM, "value": limits[1]}]}
        d["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return {"type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"h": h, "w": w, "x": x, "y": y}, "targets": [q(series(field, to_f, meas=meas, **kw))],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "min", "max", "mean"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def profiles():
    path = ROOT / "registry" / "room_profiles.csv"
    if not path.exists():  # fresh clone: site registry is git-ignored
        path = ROOT / "registry" / "room_profiles.example.csv"
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))

def rules_panel(y):
    rows = ["| Room type | Temperature target | RH target | Max RH swing / 24 h |", "| --- | --- | --- | --- |"]
    for p in profiles():
        if p["room_type"] == "test":
            continue
        swing = "—" if float(p["rh_swing_24h"]) >= 100 else f'±{p["rh_swing_24h"]} %'
        rows.append(f'| {ROOM_TYPE_NAMES.get(p["room_type"], p["room_type"])} | {f(p["t_min_c"]):.0f}–{f(p["t_max_c"]):.0f} °F '
                    f'| {float(p["rh_min"]):.0f}–{float(p["rh_max"]):.0f} % | {swing} |')
    md = ("**How status is decided.** Each room is compared with the target range for its room type.\n\n"
          f"- **OK**: inside the target range.\n"
          f"- **Watch**: up to {T_MARGIN_F:.1f} °F or {RH_MARGIN} % RH outside the range. A notification is sent if it lasts 1 hour.\n"
          f"- **Alarm**: further outside than that. A high-priority notification is sent if it lasts 10 minutes.\n"
          "- **Sensor offline**: no reading for 15 minutes.\n"
          "- Also notified: **mould risk** (RH above 65 % for 48 h), **large daily RH swing** (more than the room type allows), and a **stuck sensor** (no change in 6 h).\n\n" + "\n".join(rows) +
          overrides_md() +
          "\n\nTargets follow ISO 11799 / BS EN 16893 practice. Admins and Editors change them on the **Settings** dashboard.\n\n"
          "**Water leak probes**: any water = Alarm, notified at once.  **Air quality** (Watch above / Alarm above; "
          "notified after 2 h / 30 min): " + "; ".join(f"{AQ_INFO[k][0]} {v[1]:g} / {v[2]:g} {AQ_INFO[k][1]}".rstrip() for k, v in AQ_LIMITS.items()) + ".")
    return {"type": "text", "title": "Alarm rules and target ranges", "gridPos": {"h": 13, "w": 24, "x": 0, "y": y},
            "options": {"mode": "markdown", "content": md}}

def overrides_md():
    rows = []
    for o in overrides():
        b, room = o["room_key"].split("|", 1)
        t = (f'{f(o["o_t_min_c"]):.0f}' if o["o_t_min_c"] else "default") + "–" + (f'{f(o["o_t_max_c"]):.0f}' if o["o_t_max_c"] else "default")
        h = (o["o_rh_min"] or "default") + "–" + (o["o_rh_max"] or "default")
        rows.append(f"| {b} · {room} (room override) | {t} °F | {h} % | |")
    return ("\n" + "\n".join(rows)) if rows else ""

def overview():
    table_flux = ('import "math"\n'
                  'f = (c) => c * 1.8 + 32.0\n'
                  'r0 = (x) => string(v: int(v: math.round(x: x)))\n'
                  'r1 = (x) => string(v: math.round(x: x * 10.0) / 10.0)\n'
                  f'from(bucket: "{B}")\n  |> range(start: -30d)\n'
                  '  |> filter(fn: (r) => r._measurement == "environment")\n'
                  '  |> filter(fn: (r) => contains(value: r._field, set: ["temperature_c", "humidity_rh", "dew_point_c", "status", "t_status", "rh_status", "t_min_c", "t_max_c", "rh_min", "rh_max"]))\n'
                  + FILTERS + '  |> last()\n  |> toFloat()\n  |> group()\n'
                  '  |> pivot(rowKey: ["_time", "building", "floor", "room", "room_type", "sensor_id"], columnKey: ["_field"], valueColumn: "_value")\n'
                  '  |> map(fn: (r) => {\n'
                  '      tw = if not exists r.t_status then "" else if r.t_status == 0.0 then ""\n'
                  '           else if r.temperature_c > r.t_max_c then "Too warm: " + r1(x: f(c: r.temperature_c)) + " °F, max " + r0(x: f(c: r.t_max_c)) + " °F"\n'
                  '           else "Too cold: " + r1(x: f(c: r.temperature_c)) + " °F, min " + r0(x: f(c: r.t_min_c)) + " °F"\n'
                  '      hw = if not exists r.rh_status then "" else if r.rh_status == 0.0 then ""\n'
                  '           else if r.humidity_rh > r.rh_max then "Too humid: " + r1(x: r.humidity_rh) + " %, max " + r0(x: r.rh_max) + " %"\n'
                  '           else "Too dry: " + r1(x: r.humidity_rh) + " %, min " + r0(x: r.rh_min) + " %"\n'
                  '      return {status: r.status, building: r.building, floor: r.floor, room: r.room, room_type: r.room_type,\n'
                  '        temp_f: f(c: r.temperature_c),\n'
                  '        t_target: if exists r.t_min_c then r0(x: f(c: r.t_min_c)) + "–" + r0(x: f(c: r.t_max_c)) + " °F" else "none",\n'
                  '        humidity_rh: r.humidity_rh,\n'
                  '        rh_target: if exists r.rh_min then r0(x: r.rh_min) + "–" + r0(x: r.rh_max) + " %" else "none",\n'
                  '        dew_f: f(c: r.dew_point_c),\n'
                  '        why: if tw != "" and hw != "" then tw + "; " + hw else if tw != "" then tw else if hw != "" then hw else "In range",\n'
                  '        age_min: float(v: int(v: now()) - int(v: r._time)) / 60000000000.0, sensor_id: r.sensor_id}\n'
                  '    })\n'
                  '  |> sort(columns: ["building", "floor", "room"])')
    stale = {"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": ALARM, "value": 15}]}
    table = {"type": "table", "title": "Current conditions", "datasource": DS, "gridPos": {"h": 10, "w": 24, "x": 0, "y": 0},
             "description": "Latest reading per sensor, worst first. 'Why' explains any Watch or Alarm. See 'Alarm rules and target ranges' below.",
             "targets": [q(table_flux)],
             "options": {"showHeader": True, "cellHeight": "md", "footer": {"show": False}},
             "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": [
                 by_name("status", ("displayName", "Status"), ("mappings", STATUS_MAP), ("custom.width", 90),
                         ("custom.cellOptions", {"type": "color-background", "mode": "basic"})),
                 by_name("building", ("displayName", "Building")),
                 by_name("floor", ("displayName", "Floor"), ("custom.width", 60)),
                 by_name("room", ("displayName", "Room"), ("links", ROOM_LINK)),
                 by_name("room_type", ("displayName", "Room type"), ("mappings", ROOM_TYPE_TEXT)),
                 by_name("temp_f", ("displayName", "Temp"), ("unit", "fahrenheit"), ("decimals", 1), ("custom.width", 80)),
                 by_name("t_target", ("displayName", "Temp target"), ("custom.width", 100)),
                 by_name("humidity_rh", ("displayName", "RH"), ("unit", "humidity"), ("decimals", 1), ("custom.width", 70)),
                 by_name("rh_target", ("displayName", "RH target"), ("custom.width", 90)),
                 by_name("dew_f", ("displayName", "Dew point"), ("unit", "fahrenheit"), ("decimals", 1), ("custom.width", 90)),
                 by_name("why", ("displayName", "Why"), ("custom.minWidth", 220)),
                 by_name("age_min", ("displayName", "Last reading"), ("unit", "m"), ("decimals", 0), ("thresholds", stale),
                         ("custom.cellOptions", {"type": "color-text"}), ("custom.width", 100)),
                 by_name("sensor_id", ("displayName", "Sensor"))]}}
    timeline_flux = (f'from(bucket: "{B}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
                     '  |> filter(fn: (r) => r._measurement == "environment" and r._field == "status")\n' + FILTERS +
                     '  |> group(columns: ["building", "room"])\n  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)\n'
                     '  |> map(fn: (r) => ({_time: r._time, _value: r._value, _field: r.building + " · " + r.room}))\n  |> group(columns: ["_field"])')
    timeline = {"type": "state-timeline", "title": "Status history by room", "datasource": DS,
                "description": "Worst status in each time slice. Long amber or red bars show sustained excursions.",
                "gridPos": {"h": 7, "w": 24, "x": 0, "y": 10}, "targets": [q(timeline_flux)],
                "fieldConfig": {"defaults": {"mappings": STATUS_MAP, "color": {"mode": "fixed", "fixedColor": GREY},
                                             "custom": {"lineWidth": 0, "fillOpacity": 85}}, "overrides": []},
                "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8, "legend": {"showLegend": False}}}
    panels = [table, timeline, aq_table(17, h=7),
              ts("Temperature (°F)", "temperature_c", "fahrenheit", 24, to_f=True, desc="Mean per group. Use 'Group by' to compare rooms, room types, floors or buildings."),
              ts("Relative humidity (%)", "humidity_rh", "humidity", 33, minv=20, maxv=70, desc="Mean per group. Typical archival storage target is 35–50 % RH."),
              ts("Dew point (°F)", "dew_point_c", "fahrenheit", 42, h=8, to_f=True, desc="Condensation forms on any surface colder than this, e.g. items brought out of the cold room."),
              rules_panel(50)]
    return dash("archmon-overview", "Archive Conditions — Overview", panels,
                [tagvar("site", "Site", "site"),
                 tagvar("room_type", "Room type", "room_type", ' and contains(value: r.site, set: ${site:json})'),
                 tagvar("room", "Room", "room", ' and contains(value: r.site, set: ${site:json}) and contains(value: r.room_type, set: ${room_type:json})'),
                 GROUPBY])

def room_detail():
    def band(field, lo, hi, unit, title, y, to_f=False, minv=None, maxv=None):
        flux = (f'from(bucket: "{B}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
                f'  |> filter(fn: (r) => r._measurement == "environment" and r.room == "${{one_room}}")\n'
                f'  |> filter(fn: (r) => r._field == "{field}" or r._field == "{lo}" or r._field == "{hi}")\n'
                '  |> group(columns: ["_field"])\n  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n' + (TO_F if to_f else ''))
        line = lambda n, name: by_name(n, ("displayName", name), ("color", {"mode": "fixed", "fixedColor": GREY}),
                                       ("custom.lineStyle", {"fill": "dash", "dash": [8, 6]}), ("custom.lineWidth", 1))
        return {"type": "timeseries", "title": title, "datasource": DS, "gridPos": {"h": 10, "w": 24, "x": 0, "y": y},
                "targets": [q(flux)],
                "fieldConfig": {"defaults": {"unit": unit, "decimals": 1, "custom": {"lineWidth": 2, "showPoints": "never", "spanNulls": 600000,
                                                                                    "axisSoftMin": minv, "axisSoftMax": maxv}},
                                "overrides": [by_name(field, ("displayName", "${one_room}"), ("color", {"mode": "fixed", "fixedColor": TEMP_LINE})),
                                              line(lo, "Target min"), line(hi, "Target max")]},
                "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}}
    def stat(title, field, unit, x, to_f=False, flux=None, desc=""):
        flux = flux or (f'from(bucket: "{B}")\n  |> range(start: -1d)\n'
                        f'  |> filter(fn: (r) => r._measurement == "environment" and r.room == "${{one_room}}" and r._field == "{field}")\n'
                        '  |> group()\n  |> last()\n' + (TO_F if to_f else ''))
        return {"type": "stat", "title": title, "description": desc, "datasource": DS, "gridPos": {"h": 4, "w": 4, "x": x, "y": 0},
                "targets": [q(flux)], "fieldConfig": {"defaults": {"unit": unit, "decimals": 1, "color": {"mode": "fixed", "fixedColor": TEMP_LINE}}},
                "options": {"colorMode": "value", "graphMode": "none", "textMode": "value", "reduceOptions": {"calcs": ["lastNotNull"]}}}
    target = ('import "math"\nr0 = (x) => string(v: int(v: math.round(x: x)))\n'
              f'from(bucket: "{B}")\n  |> range(start: -1d)\n'
              '  |> filter(fn: (r) => r._measurement == "environment" and r.room == "${one_room}")\n'
              '  |> filter(fn: (r) => contains(value: r._field, set: ["t_min_c", "t_max_c", "rh_min", "rh_max"]))\n'
              '  |> last()\n  |> group()\n  |> pivot(rowKey: ["room"], columnKey: ["_field"], valueColumn: "_value")\n'
              '  |> map(fn: (r) => ({target: r0(x: r.t_min_c * 1.8 + 32.0) + "–" + r0(x: r.t_max_c * 1.8 + 32.0) + " °F  ·  " + r0(x: r.rh_min) + "–" + r0(x: r.rh_max) + " % RH"}))')
    swing = (f'from(bucket: "{B}")\n  |> range(start: -24h)\n'
             '  |> filter(fn: (r) => r._measurement == "environment" and r.room == "${one_room}" and r._field == "humidity_rh")\n'
             '  |> group()\n  |> spread()')
    status = stat("Status", "status", "none", 0)
    status["fieldConfig"]["defaults"].update({"mappings": STATUS_MAP, "color": {"mode": "thresholds"}})
    status["options"]["colorMode"] = "background"
    tgt = stat("Target range", None, "string", 16, flux=target)
    tgt["gridPos"]["w"] = 8
    tgt["options"]["reduceOptions"] = {"calcs": ["lastNotNull"], "fields": "/^target$/"}
    tgt["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": GREY}
    panels = [status,
              stat("Temperature now", "temperature_c", "fahrenheit", 4, to_f=True),
              stat("RH now", "humidity_rh", "humidity", 8),
              stat("RH swing, last 24 h", None, "humidity", 12, flux=swing,
                   desc="Max minus min RH over 24 h. Large daily swings cause mechanical stress (cockling, flaking)."),
              tgt,
              band("temperature_c", "t_min_c", "t_max_c", "fahrenheit", "Temperature vs target (°F)", 4, to_f=True),
              band("humidity_rh", "rh_min", "rh_max", "humidity", "Relative humidity vs target (%)", 14, minv=20, maxv=70),
              band("dew_point_c", "dew_point_c", "dew_point_c", "fahrenheit", "Dew point (°F)", 24, to_f=True)]
    panels[-1]["fieldConfig"]["overrides"] = [by_name("dew_point_c", ("displayName", "${one_room}"), ("color", {"mode": "fixed", "fixedColor": TEMP_LINE}))]
    panels[-1]["gridPos"]["h"] = 8
    panels = json.loads(json.dumps(panels).replace('r.room == \\"${one_room}\\"',
                                                   'r.building == \\"${one_building}\\" and r.room == \\"${one_room}\\"'))
    for p in panels:
        if p["title"] == "Status":
            p["title"] = "${one_building} · ${one_room}"
    extra = [aq_table(32, h=5, filt=ROOM_FILT), *leak_air_panels(37, filt=ROOM_FILT, per_sensor=True)]
    for p in extra:
        p["description"] = (p.get("description") or "") + " Shows 'No data' if this room has no leak or air-quality sensor."
    return dash("archmon-room", "Archive Conditions — Room Detail", panels + extra,
                [tagvar("one_building", "Building", "building", multi=False),
                 tagvar("one_room", "Room", "room", ' and r.building == "${one_building}"', multi=False)])

def health():
    flux = (f'from(bucket: "{B}")\n  |> range(start: -30d)\n'
            '  |> filter(fn: (r) => r._measurement == "device_state" and (r._field == "Wifi_Signal" or r._field == "UptimeSec"))\n'
            '  |> last()\n  |> toFloat()\n'
            '  |> map(fn: (r) => ({r with room: if exists r.room then r.room else "Unregistered", device_id: if exists r.device_id then r.device_id else ""}))\n'
            '  |> group()\n'
            '  |> pivot(rowKey: ["_time", "sensor_id", "room", "device_id"], columnKey: ["_field"], valueColumn: "_value")\n'
            '  |> map(fn: (r) => ({sensor_id: r.sensor_id, device_id: r.device_id, room: r.room,\n'
            '      signal: r.Wifi_Signal, uptime_s: r.UptimeSec, age_min: float(v: int(v: now()) - int(v: r._time)) / 60000000000.0}))\n'
            '  |> sort(columns: ["age_min"], desc: true)')
    stale = {"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": ALARM, "value": 15}]}
    sigthr = {"mode": "absolute", "steps": [{"color": ALARM, "value": None}, {"color": WATCH, "value": -80}, {"color": OK, "value": -70}]}
    table = {"type": "table", "title": "Sensors", "datasource": DS, "gridPos": {"h": 10, "w": 24, "x": 0, "y": 0},
             "description": "Weak Wi-Fi (below -75 dBm), frequent restarts (short uptime) or a stale last report point to a sensor that needs attention. Drift checks come in a later phase.",
             "targets": [q(flux)], "fieldConfig": {"defaults": {}, "overrides": [
                 by_name("sensor_id", ("displayName", "Sensor")),
                 by_name("device_id", ("displayName", "Device (MAC)")),
                 by_name("room", ("displayName", "Room")),
                 by_name("signal", ("displayName", "Wi-Fi signal"), ("unit", "dBm"), ("thresholds", sigthr), ("custom.cellOptions", {"type": "color-text"})),
                 by_name("uptime_s", ("displayName", "Uptime"), ("unit", "s")),
                 by_name("age_min", ("displayName", "Last report"), ("unit", "m"), ("decimals", 0), ("thresholds", stale), ("custom.cellOptions", {"type": "color-text"}))]}}
    sig_flux = (f'from(bucket: "{B}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
                '  |> filter(fn: (r) => r._measurement == "device_state" and r._field == "Wifi_Signal")\n'
                '  |> group(columns: ["sensor_id"])\n  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n'
                '  |> map(fn: (r) => ({_time: r._time, _value: r._value, _field: r.sensor_id}))\n  |> group(columns: ["_field"])')
    sig = {"type": "timeseries", "title": "Wi-Fi signal (dBm)", "datasource": DS, "gridPos": {"h": 9, "w": 24, "x": 0, "y": 18},
           "targets": [q(sig_flux)], "fieldConfig": {"defaults": {"unit": "dBm", "custom": {"lineWidth": 1, "showPoints": "never"}}, "overrides": []},
           "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "min"]}, "tooltip": {"mode": "multi"}}}
    return dash("archmon-health", "Archive Conditions — Sensor Health", [table, drift_panel(10), sig], [])

def compliance():
    """Monthly report: time in band, TWPI, mould exposure, extremes per room (15-min means)."""
    flux = ('import "math"\n' + PI_FLUX +
            'f = (c) => c * 1.8 + 32.0\n'
            f'from(bucket: "{B}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
            f'  |> filter(fn: (r) => r._measurement == "environment" and {EXCL})\n'
            '  |> filter(fn: (r) => r._field == "temperature_c" or r._field == "humidity_rh" or r._field == "status")\n'
            + FILTERS +
            '  |> group(columns: ["building", "floor", "room", "_field"])\n'
            '  |> aggregateWindow(every: 15m, fn: max, createEmpty: false)\n'
            '  |> toFloat()\n'
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            '  |> filter(fn: (r) => exists r.temperature_c and exists r.humidity_rh and exists r.status)\n'
            '  |> reduce(identity: {n: 0.0, ok: 0.0, alarm: 0.0, mould: 0.0, ts: 0.0, tmin: 1000.0, tmax: -1000.0, hs: 0.0, hmin: 1000.0, hmax: -1000.0, ipi: 0.0},\n'
            '      fn: (r, accumulator) => ({n: accumulator.n + 1.0,\n'
            '        ok: accumulator.ok + (if r.status == 0.0 then 1.0 else 0.0),\n'
            '        alarm: accumulator.alarm + (if r.status >= 2.0 then 1.0 else 0.0),\n'
            '        mould: accumulator.mould + (if r.humidity_rh > 65.0 then 1.0 else 0.0),\n'
            '        ts: accumulator.ts + r.temperature_c, tmin: math.mMin(x: accumulator.tmin, y: r.temperature_c), tmax: math.mMax(x: accumulator.tmax, y: r.temperature_c),\n'
            '        hs: accumulator.hs + r.humidity_rh, hmin: math.mMin(x: accumulator.hmin, y: r.humidity_rh), hmax: math.mMax(x: accumulator.hmax, y: r.humidity_rh),\n'
            '        ipi: accumulator.ipi + 1.0 / pi(t: r.temperature_c, h: r.humidity_rh)}))\n'
            '  |> map(fn: (r) => ({building: r.building, floor: r.floor, room: r.room,\n'
            '      pct_ok: 100.0 * r.ok / r.n, pct_alarm: 100.0 * r.alarm / r.n, mould_h: r.mould / 4.0,\n'
            '      t_mean: f(c: r.ts / r.n), t_min: f(c: r.tmin), t_max: f(c: r.tmax),\n'
            '      h_mean: r.hs / r.n, h_min: r.hmin, h_max: r.hmax, twpi: r.n / r.ipi, coverage_d: r.n / 96.0}))\n'
            '  |> group()\n  |> sort(columns: ["building", "floor", "room"])')
    pct = lambda good_hi: {"mode": "absolute", "steps": [{"color": ALARM, "value": None}, {"color": WATCH, "value": 80}, {"color": OK, "value": 95}]} if good_hi else \
        {"mode": "absolute", "steps": [{"color": OK, "value": None}, {"color": WATCH, "value": 0.1}, {"color": ALARM, "value": 5}]}
    twpi_thr = {"mode": "absolute", "steps": [{"color": ALARM, "value": None}, {"color": WATCH, "value": 45}, {"color": OK, "value": 75}]}
    mould_thr = {"mode": "absolute", "steps": [{"color": OK, "value": None}, {"color": WATCH, "value": 1}, {"color": ALARM, "value": 48}]}
    ct = lambda: ("custom.cellOptions", {"type": "color-text"})
    table = {"type": "table", "title": "Room compliance for the selected period", "datasource": DS,
             "gridPos": {"h": 12, "w": 24, "x": 0, "y": 0}, "targets": [q(flux)],
             "description": "Based on 15-minute intervals. Default: this month so far. For a finished month use the time picker → 'Previous month'. Export: panel menu → Inspect → Data → Download CSV.",
             "options": {"showHeader": True, "cellHeight": "md", "footer": {"show": False}},
             "fieldConfig": {"defaults": {"decimals": 1}, "overrides": [
                 by_name("building", ("displayName", "Building")), by_name("floor", ("displayName", "Floor"), ("custom.width", 60)),
                 by_name("room", ("displayName", "Room"), ("links", ROOM_LINK)),
                 by_name("pct_ok", ("displayName", "Time in range"), ("unit", "percent"), ("thresholds", pct(True)), ct()),
                 by_name("pct_alarm", ("displayName", "Time in alarm"), ("unit", "percent"), ("thresholds", pct(False)), ct()),
                 by_name("t_mean", ("displayName", "Mean temp"), ("unit", "fahrenheit")),
                 by_name("t_min", ("displayName", "Min temp"), ("unit", "fahrenheit")),
                 by_name("t_max", ("displayName", "Max temp"), ("unit", "fahrenheit")),
                 by_name("h_mean", ("displayName", "Mean RH"), ("unit", "humidity")),
                 by_name("h_min", ("displayName", "Min RH"), ("unit", "humidity")),
                 by_name("h_max", ("displayName", "Max RH"), ("unit", "humidity")),
                 by_name("mould_h", ("displayName", "Hours RH > 65 %"), ("unit", "h"), ("decimals", 0), ("thresholds", mould_thr), ct()),
                 by_name("twpi", ("displayName", "TWPI (years)"), ("decimals", 0), ("thresholds", twpi_thr), ct()),
                 by_name("coverage_d", ("displayName", "Data (days)"), ("decimals", 1))]}}
    md = ("**Reading this report**\n\n"
          "- **Time in range**: share of 15-minute intervals with status OK. Aim for 95 % or more.\n"
          "- **TWPI** (time-weighted preservation index): estimated years until paper and other organic materials show noticeable chemical decay "
          "at these conditions. About 45 years at 68 °F / 50 % RH. Above 75 is good, 45–75 is acceptable, below 45 means faster ageing. "
          "Cooler and drier raises it. (Sebera isoperm method, approximate IPI equivalent.)\n"
          "- **Hours RH > 65 %**: mould germination risk rises with time above 65 % RH, and sharply after about 48 continuous hours.\n"
          "- **Data (days)**: amount of data the row is based on. Low values mean the sensor was offline or newly installed.\n\n"
          "To print or save as PDF, use the browser's print function in kiosk view (add `?kiosk` to the address).")
    text = {"type": "text", "title": "About these metrics", "gridPos": {"h": 8, "w": 24, "x": 0, "y": 12}, "options": {"mode": "markdown", "content": md}}
    d = dash("archmon-report", "Archive Conditions — Monthly Report", [table, text],
             [tagvar("site", "Site", "site"),
              tagvar("room_type", "Room type", "room_type", ' and contains(value: r.site, set: ${site:json})'),
              tagvar("room", "Room", "room", ' and contains(value: r.site, set: ${site:json}) and contains(value: r.room_type, set: ${room_type:json})')])
    d["time"] = {"from": "now/M", "to": "now"}
    d["refresh"] = ""
    return d

def drift_panel(y):
    """Plausibility + peer comparison per sensor over the last 6 h."""
    flux = (f'base = from(bucket: "{B}")\n  |> range(start: -6h)\n'
            f'  |> filter(fn: (r) => r._measurement == "environment" and (r._field == "temperature_c" or r._field == "humidity_rh") and {EXCL})\n'
            'stats = base\n  |> group(columns: ["building", "room", "sensor_id", "_field"])\n'
            '  |> reduce(identity: {s: 0.0, s2: 0.0, n: 0.0, mx: -1000.0}, fn: (r, accumulator) => ({s: accumulator.s + r._value,\n'
            '      s2: accumulator.s2 + r._value * r._value, n: accumulator.n + 1.0, mx: if r._value > accumulator.mx then r._value else accumulator.mx}))\n'
            '  |> map(fn: (r) => ({r with mean: r.s / r.n, sd: if r.n > 1.0 then (r.s2 - r.s * r.s / r.n) / (r.n - 1.0) else 0.0}))\n'
            'rooms = stats\n  |> group(columns: ["building", "room", "_field"])\n'
            '  |> reduce(identity: {rs: 0.0, rn: 0.0}, fn: (r, accumulator) => ({rs: accumulator.rs + r.mean, rn: accumulator.rn + 1.0}))\n'
            'join(tables: {s: stats, r: rooms}, on: ["building", "room", "_field"])\n'
            '  |> map(fn: (r) => ({building: r.building, room: r.room, sensor_id: r.sensor_id, _field: r._field, n: r.n, max: r.mx, var: r.sd,\n'
            '      dev: if r.rn > 1.0 then r.mean - (r.rs - r.mean) / (r.rn - 1.0) else 0.0, peers: r.rn - 1.0}))\n'
            '  |> group()\n'
            '  |> pivot(rowKey: ["building", "room", "sensor_id", "peers"], columnKey: ["_field"], valueColumn: "dev")\n'
            '  |> rename(columns: {temperature_c: "dev_t", humidity_rh: "dev_h"})\n')
    # second query for plausibility flags, kept simple and joined in Grafana by sensor_id
    flags = (f'from(bucket: "{B}")\n  |> range(start: -6h)\n'
             f'  |> filter(fn: (r) => r._measurement == "environment" and (r._field == "temperature_c" or r._field == "humidity_rh") and {EXCL})\n'
             '  |> group(columns: ["sensor_id", "_field"])\n'
             '  |> reduce(identity: {lo: 1000.0, hi: -1000.0}, fn: (r, accumulator) => ({lo: if r._value < accumulator.lo then r._value else accumulator.lo,\n'
             '      hi: if r._value > accumulator.hi then r._value else accumulator.hi}))\n'
             '  |> map(fn: (r) => ({sensor_id: r.sensor_id, _field: r._field, flag: if r.hi - r.lo == 0.0 then "stuck (no change in 6 h)"\n'
             '      else if r._field == "humidity_rh" and r.hi >= 99.0 then "saturated (RH ≥ 99 %)" else ""}))\n'
             '  |> group(columns: ["sensor_id"])\n'
             '  |> reduce(identity: {check: ""}, fn: (r, accumulator) => ({check: if r.flag == "" then accumulator.check else if accumulator.check == "" then r.flag else accumulator.check + "; " + r.flag}))\n'
             '  |> map(fn: (r) => ({sensor_id: r.sensor_id, check: if r.check == "" then "OK" else r.check}))\n  |> group()')
    devthr = lambda w, a: {"mode": "absolute", "steps": [{"color": ALARM, "value": None}, {"color": WATCH, "value": -a}, {"color": "text", "value": -w},
                                                          {"color": WATCH, "value": w}, {"color": ALARM, "value": a}]}
    return {"type": "table", "title": "Drift and plausibility (last 6 h)", "datasource": DS, "gridPos": {"h": 8, "w": 24, "x": 0, "y": y},
            "description": "Deviation compares each sensor with the average of the OTHER sensors in the same room (needs 2+ sensors per room). "
                           "Flag for checking at > 1 °C (1.8 °F) or > 4 % RH. 'Stuck' or 'saturated' suggests a failing sensor element.",
            "targets": [q(flux, "A"), q(flags, "B")],
            "transformations": [{"id": "joinByField", "options": {"byField": "sensor_id", "mode": "outer"}}],
            "fieldConfig": {"defaults": {"decimals": 2}, "overrides": [
                by_name("building", ("displayName", "Building")), by_name("room", ("displayName", "Room")), by_name("sensor_id", ("displayName", "Sensor")),
                by_name("peers", ("displayName", "Peers in room"), ("decimals", 0)),
                by_name("dev_t", ("displayName", "Temp deviation (°C)"), ("thresholds", devthr(0.7, 1.0)), ("custom.cellOptions", {"type": "color-text"})),
                by_name("dev_h", ("displayName", "RH deviation (%)"), ("thresholds", devthr(3, 4)), ("custom.cellOptions", {"type": "color-text"})),
                by_name("check", ("displayName", "Plausibility"))]}}

def aq_table(y, h=10, filt=FILTERS):
    reading = ' + " · " + '.join(f'(if exists r.{k} then "{AQ_INFO[k][0]} " + r0(x: r.{k}) + "{(" " + AQ_INFO[k][1]) if AQ_INFO[k][1] else ""}" else "")' for k in AQ_LIMITS)
    why = " + ".join(f'(if exists r.{k}_status and r.{k}_status > 0.0 then "{AQ_INFO[k][0]} above " + r0(x: if r.{k}_status > 1.0 then {v[2]} else {v[1]}) + "; " else "")'
                     for k, v in AQ_LIMITS.items())
    flux = ('import "math"\nimport "strings"\nr0 = (x) => string(v: int(v: math.round(x: x)))\n'
            f'base = from(bucket: "{B}")\n  |> range(start: -30d)\n' + filt +
            '  |> last()\n  |> toFloat()\n'
            'leak = base\n  |> filter(fn: (r) => r._measurement == "leak" and r._field == "water")\n'
            '  |> map(fn: (r) => ({building: r.building, floor: r.floor, room: r.room, sensor_id: r.sensor_id, kind: "Water leak",\n'
            '      status: r._value * 2.0, reading: if r._value > 0.0 then "WATER DETECTED" else "Dry",\n'
            '      why: if r._value > 0.0 then "Water at the probe: check now" else "Dry"}))\n'
            'air = base\n  |> filter(fn: (r) => r._measurement == "air_quality")\n  |> group()\n'
            '  |> pivot(rowKey: ["building", "floor", "room", "sensor_id"], columnKey: ["_field"], valueColumn: "_value")\n'
            '  |> map(fn: (r) => {\n'
            f'      w = {why}\n'
            f'      rd = {reading}\n'
            '      return {building: r.building, floor: r.floor, room: r.room, sensor_id: r.sensor_id, kind: "Air quality", status: r.status,\n'
            '        reading: strings.trim(v: strings.replaceAll(v: rd, t: " ·  · ", u: " · "), cutset: " ·"),\n'
            '        why: if w == "" then "In range" else strings.trimSuffix(v: w, suffix: "; ")}\n'
            '    })\n'
            'union(tables: [leak |> group(), air])\n  |> sort(columns: ["building", "floor", "room", "kind"])')
    return {"type": "table", "title": "Leaks and air quality", "datasource": DS, "gridPos": {"h": h, "w": 24, "x": 0, "y": y},
            "description": "Latest state of water-leak probes and air-quality sensors. Limits: see 'Alarm rules and target ranges' on the Overview.",
            "targets": [q(flux)],
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
            "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": [
                by_name("status", ("displayName", "Status"), ("mappings", STATUS_MAP), ("custom.width", 90),
                        ("custom.cellOptions", {"type": "color-background", "mode": "basic"})),
                by_name("building", ("displayName", "Building")), by_name("floor", ("displayName", "Floor"), ("custom.width", 60)),
                by_name("room", ("displayName", "Room"), ("links", ROOM_LINK)), by_name("kind", ("displayName", "Type"), ("custom.width", 110)),
                by_name("reading", ("displayName", "Reading"), ("custom.minWidth", 260)), by_name("why", ("displayName", "Why"), ("custom.minWidth", 200)),
                by_name("sensor_id", ("displayName", "Sensor"))]}}

def leak_air_panels(y, filt=FILTERS, per_sensor=False):
    """Leak history timeline + one chart per air-quality field, starting at row y."""
    leak_flux = (f'from(bucket: "{B}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
                 '  |> filter(fn: (r) => r._measurement == "leak" and r._field == "water")\n' + filt +
                 '  |> group(columns: ["room", "sensor_id"])\n  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)\n'
                 '  |> map(fn: (r) => ({_time: r._time, _value: r._value, _field: r.room + " · " + r.sensor_id}))\n  |> group(columns: ["_field"])')
    leak = {"type": "state-timeline", "title": "Water leak history", "datasource": DS, "gridPos": {"h": 6, "w": 24, "x": 0, "y": y},
            "targets": [q(leak_flux)],
            "fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {"0": {"text": "Dry", "color": OK}, "1": {"text": "WATER", "color": ALARM}}}],
                                         "color": {"mode": "fixed", "fixedColor": GREY}, "custom": {"lineWidth": 0, "fillOpacity": 85}}, "overrides": []},
            "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8, "legend": {"showLegend": False}}}
    occ_flux = leak_flux.replace('r._measurement == "leak" and r._field == "water"', 'r._measurement == "occupancy" and r._field == "motion"')
    occ = {"type": "state-timeline", "title": "Presence (motion sensors)", "datasource": DS, "gridPos": {"h": 5, "w": 24, "x": 0, "y": y + 6},
           "description": "Motion or presence sensors (PIR / mmWave, registry kind=motion). Explains short temperature/RH bumps: one person adds about 100 W and 40-70 g of water vapour an hour.",
           "targets": [q(occ_flux)],
           "fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {"0": {"text": "Empty", "color": "#E8ECEF"}, "1": {"text": "Motion", "color": TEMP_LINE}}}],
                                        "color": {"mode": "fixed", "fixedColor": GREY}, "custom": {"lineWidth": 0, "fillOpacity": 85}}, "overrides": []},
           "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8, "legend": {"showLegend": False}}}
    y += 5
    charts = []
    for i, (k, v) in enumerate(AQ_LIMITS.items()):
        name, unit, desc = AQ_INFO[k]
        charts.append(ts(f"{name}{' (' + unit + ')' if unit else ''}", k, {"µg/m³": "conμgm3", "ppm": "ppm"}.get(unit, "none"),
                         y + 6 + (i // 2) * 8, h=8, w=12, x=(i % 2) * 12, meas="air_quality", limits=v[1:],
                         filt=filt, per_sensor=per_sensor, desc=f"{desc}. Dashed lines: Watch {v[1]:g}, Alarm {v[2]:g}."))
    return [leak, occ, *charts]

def air_quality():
    return dash("archmon-air", "Archive Conditions — Leaks & Air Quality", [aq_table(0), *leak_air_panels(10)],
                [tagvar("site", "Site", "site"),
                 tagvar("room_type", "Room type", "room_type", ' and contains(value: r.site, set: ${site:json})'),
                 tagvar("room", "Room", "room", ' and contains(value: r.site, set: ${site:json}) and contains(value: r.room_type, set: ${room_type:json})'),
                 GROUPBY])

def slug(building, room):  # must match config/targets_api.py
    return re.sub(r"[^a-z0-9]+", "_", f"{building}_{room}".lower()).strip("_")

def registry_rooms():
    path = ROOT / "registry" / "sensors.csv"
    if not path.exists():  # fresh clone: site registry is git-ignored
        path = ROOT / "registry" / "sensors.example.csv"
    with open(path, encoding="utf-8") as fh:
        rooms = {}
        for r in csv.DictReader(fh):
            if r["room_type"] != "test":
                rooms.setdefault((r["building"], r["room"]), r["room_type"])
    return rooms

def overrides():
    path = ROOT / "registry" / "room_overrides.csv"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))

def software_update(y):
    """Updater (requirement F16): Business Forms panel <-> /archmon-api/update <-> scripts/update.sh on the host."""
    ro = lambda id_, title, type_="disabled", **kw: {"id": id_, "uid": id_, "title": title, "type": type_, "section": "",
                                                       "labelWidth": 22, "width": 0, "tooltip": "", "unit": "", "value": "", **kw}
    elements = [ro("installed", "Installed version"), ro("latest", "Newest release"), ro("state", "State"),
                ro("message", "Status"), ro("updated_at", "Last checked / changed"),
                ro("notes", "What's new in the newest release", "disabledTextarea", rows=6),
                {**ro("action", "Action", "radio"), "value": "check", "optionsSource": "Custom",
                 "options": [{"value": "check", "label": "Check for updates now"}, {"value": "install", "label": "Install the newest release"}]},
                ro("history", "Update history (newest first)", "disabledTextarea", rows=4),
                ro("log", "Updater log (last lines)", "disabledTextarea", rows=6)]
    url = "/archmon-api/update"
    initial_code = ("const r = context.panel.response;\n"
                    "if (r && !r.ok) {\n"
                    "  r.json().then((b) => context.grafana.notifyError(['Software update', b.error || r.statusText])).catch(() => {});\n}")
    update_code = ("const r = context.panel.response;\nif (!r) { return; }\n"
                   "r.json().then((b) => {\n"
                   "  if (r.ok) {\n    context.grafana.notifySuccess(['Software update', b.message]);\n"
                   "    setTimeout(() => context.grafana.locationService.reload(), 4000);\n"
                   "  } else {\n    context.grafana.notifyError(['Not started', b.error || r.statusText]);\n  }\n}).catch(() => {});")
    form = {"type": "volkovlabs-form-panel", "title": "Software update", "gridPos": {"h": 22, "w": 16, "x": 0, "y": y},
            "description": "Grafana Admins can check for and install new signed releases. Everyone can see the installed version.",
            "options": {
                "layout": {"variant": "single", "orientation": "vertical", "padding": 10},
                "elements": elements,
                "initial": {"method": "GET", "url": url, "contentType": "application/json", "code": initial_code,
                            "highlight": False, "payload": {}},
                "update": {"method": "POST", "url": url, "contentType": "application/json", "payloadMode": "all", "code": update_code,
                           "confirm": True, "payload": {}},
                "updateEnabled": "auto",
                "confirmModal": {"title": "Run the software updater?", "body": "Install: takes a backup, switches to the newest signed release, "
                                 "restarts every service (about a minute without dashboards) and rolls back automatically if the health check fails.",
                                 "cancel": "Cancel", "confirm": "Go", "elementDisplayMode": "modified",
                                 "columns": {"include": ["name", "newValue"], "name": "Setting", "newValue": "Value"}},
                "submit": {"text": "Run", "icon": "sync", "variant": "primary"},
                "reset": {"text": "Reload", "icon": "history", "variant": "secondary"},
                "resetAction": {"mode": "initial", "confirm": False, "payload": {}},
                "saveDefault": {"text": "Save Default", "icon": "save", "variant": "hidden"},
                "buttonGroup": {"orientation": "left", "size": "md"},
                "sync": True}}
    help_md = ("**Software updates**\n\n"
               "New versions are published as signed releases on GitHub. The server checks once a day and sends an ntfy note "
               "when one is available.\n\n"
               "1. Choose **Check for updates now** and press **Run** to check immediately.\n"
               "2. To install, choose **Install the newest release**, press **Run** and confirm.\n\n"
               "The updater runs on the server, not in Grafana: it takes a backup, installs the release, restarts the services and "
               "checks that data is still arriving. If anything fails it goes back to the previous version by itself.\n\n"
               "Only releases signed with the maintainer's key are installed. Room lists, targets, passwords and data are never "
               "changed by an update. Only Grafana **Admins** can run it.")
    text = {"type": "text", "title": "About updates", "gridPos": {"h": 22, "w": 8, "x": 16, "y": y}, "options": {"mode": "markdown", "content": help_md}}
    return [form, text]

def settings():
    """Target editor: Business Forms panel <-> config/targets_api.py (via nginx at /archmon-api/)."""
    def el(id_, title, section, type_="number", **kw):
        e = {"id": id_, "uid": id_, "title": title, "type": type_, "section": section, "labelWidth": 22, "width": 12,
             "tooltip": kw.pop("tooltip", ""), "unit": kw.pop("unit", ""), "value": "" if type_ != "number" else None}
        e.update(kw)
        return e
    elements, sections = [], []
    for p in profiles():
        rt = p["room_type"]
        if rt == "test":
            continue
        sec = "Room type: " + ROOM_TYPE_NAMES.get(rt, rt)
        sections.append({"id": sec, "name": sec})
        elements += [el(f"{rt}__t_min", "Temperature min", sec, unit="°F", min=-40, max=120, step=1),
                     el(f"{rt}__t_max", "Temperature max", sec, unit="°F", min=-40, max=120, step=1),
                     el(f"{rt}__rh_min", "RH min", sec, unit="%", min=0, max=100, step=1),
                     el(f"{rt}__rh_max", "RH max", sec, unit="%", min=0, max=100, step=1),
                     el(f"{rt}__swing", "Max daily RH swing", sec, unit="± %", min=0, max=100, step=1,
                        tooltip="Alert when RH varies more than twice this over 24 h. 100 = off.")]
    for (b, room), rt in sorted(registry_rooms().items()):
        sec = f"Override: {b} · {room}"
        sections.append({"id": sec, "name": sec})
        s = slug(b, room)
        tip = f"Leave blank to use the {ROOM_TYPE_NAMES.get(rt, rt).split(' (')[0]} targets."
        elements += [el(f"o_{s}__t_min", "Temperature min", sec, "string", unit="°F", tooltip=tip),
                     el(f"o_{s}__t_max", "Temperature max", sec, "string", unit="°F", tooltip=tip),
                     el(f"o_{s}__rh_min", "RH min", sec, "string", unit="%", tooltip=tip),
                     el(f"o_{s}__rh_max", "RH max", sec, "string", unit="%", tooltip=tip)]
    sections.append({"id": "Change log", "name": "Change log (newest first)"})
    elements.append({**el("audit", "Recent changes", "Change log", "disabledTextarea"), "rows": 10, "width": 0})
    url = "/archmon-api/targets"
    initial_code = ("const r = context.panel.response;\n"
                    "if (r && !r.ok) {\n"
                    "  r.json().then((b) => context.grafana.notifyError(['Targets', b.error || r.statusText])).catch(() => {});\n}")
    update_code = ("const r = context.panel.response;\nif (!r) { return; }\n"
                   "r.json().then((b) => {\n"
                   "  if (r.ok) {\n    context.grafana.notifySuccess(['Targets', b.message]);\n"
                   "    setTimeout(() => context.grafana.locationService.reload(), 1500);\n"
                   "  } else {\n    context.grafana.notifyError(['Not saved', b.error || r.statusText]);\n  }\n}).catch(() => {});")
    form = {"type": "volkovlabs-form-panel", "title": "Target ranges", "gridPos": {"h": 26, "w": 16, "x": 0, "y": 0},
            "description": "Grafana Editors and Admins can change targets here. Viewers can look but not save.",
            "options": {
                "layout": {"variant": "split", "orientation": "vertical", "padding": 10, "sectionVariant": "collapsable", "sections": sections},
                "elements": elements,
                "initial": {"method": "GET", "url": url, "contentType": "application/json", "code": initial_code,
                            "highlight": True, "highlightColor": "orange", "payload": {}},
                "update": {"method": "POST", "url": url, "contentType": "application/json", "payloadMode": "all", "code": update_code,
                           "confirm": True, "payload": {}},
                "updateEnabled": "auto",
                "confirmModal": {"title": "Save new targets?", "body": "These targets apply to new readings within about a minute and are recorded in the change log.",
                                 "cancel": "Cancel", "confirm": "Save", "elementDisplayMode": "modified",
                                 "columns": {"include": ["name", "oldValue", "newValue"], "name": "Setting", "oldValue": "Old", "newValue": "New"}},
                "submit": {"text": "Save targets", "icon": "save", "variant": "primary"},
                "reset": {"text": "Undo edits", "icon": "history", "variant": "secondary"},
                "resetAction": {"mode": "initial", "confirm": False, "payload": {}},
                "saveDefault": {"text": "Save Default", "icon": "save", "variant": "hidden"},
                "buttonGroup": {"orientation": "left", "size": "md"},
                "sync": True}}
    help_md = ("**How to change targets**\n\n"
               "1. Open a **room type** section and edit its range; this applies to every room of that type.\n"
               "2. To give **one room** different targets (for example a stacks room allowed up to 72 °F), open its **Override** "
               "section and fill in only the values that differ. Blank = use the room type's value.\n"
               "3. Press **Save targets** and confirm.\n\n"
               "Status, alerts, the 'Temp target' / 'RH target' columns and the rules panel use the new values within about a minute. "
               "Past readings keep the status they had.\n\n"
               f"**Watch** starts at the edge of the range; **Alarm** at more than {T_MARGIN_F:.1f} °F or {RH_MARGIN} % RH outside it.\n\n"
               "Only Grafana **Editors and Admins** can save. Every change is logged with the user name (Change log section, "
               "and `registry/targets-changes.log` on the Pi).")
    text = {"type": "text", "title": "Help", "gridPos": {"h": 26, "w": 8, "x": 16, "y": 0}, "options": {"mode": "markdown", "content": help_md}}
    d = dash("archmon-settings", "Archive Conditions — Settings", [form, text, *software_update(26)], [])
    d["refresh"] = ""
    return d

COLUMN_ORDER = {
    "Leaks and air quality": ["building", "floor", "room", "kind", "status", "reading", "why", "sensor_id"],
    "Current conditions": ["building", "floor", "room", "status", "temp_f", "humidity_rh", "dew_f", "t_target", "rh_target", "why",
                           "age_min", "room_type", "sensor_id"],
    "Room compliance for the selected period": ["building", "floor", "room", "pct_ok", "pct_alarm", "t_mean", "t_min", "t_max",
                                                "h_mean", "h_min", "h_max", "mould_h", "twpi", "coverage_d"],
    "Sensors": ["room", "sensor_id", "device_id", "age_min", "signal", "uptime_s"],
    "Drift and plausibility (last 6 h)": ["building", "room", "sensor_id", "peers", "dev_t", "dev_h", "check"],
    "Website images": ["target", "last_ok", "last_status", "med_ms", "p95_ms", "ttfb", "speed", "size", "pct_ok", "checks", "age_min", "host"],
    "Recent failures": ["_time", "target", "status", "error"],
}

# Menu order (link bar on every dashboard). The number prefix keeps Grafana's alphabetical Dashboards list in the same order.
NAV = {"archmon-overview": "1 Overview", "archmon-room": "2 Room Detail", "archmon-report": "3 Monthly Report",
       "archmon-air": "4 Leaks & Air Quality", "archmon-health": "5 Sensor Health", "archmon-web": "6 Website",
       "archmon-settings": "7 Settings"}

def dash(uid, title, panels, variables):
    for i, p in enumerate(panels):
        p["id"] = i + 1
        order = COLUMN_ORDER.get(p.get("title"))
        if order:  # Flux returns columns alphabetically; fix the display order
            p.setdefault("transformations", []).append(
                {"id": "organize", "options": {"indexByName": {c: n for n, c in enumerate(order)}}})
    title = NAV.get(uid, title)
    return {"uid": uid, "title": title, "tags": ["archmon"], "timezone": "browser", "schemaVersion": 39,
            "editable": False, "graphTooltip": 1, "refresh": "1m", "time": {"from": "now-7d", "to": "now"},
            "links": [{"type": "link", "title": t.split(" ", 1)[1], "url": f"/d/{u}", "keepTime": u != "archmon-report",
                       "icon": "dashboard", "tooltip": "", "targetBlank": False} for u, t in NAV.items()],
            "templating": {"list": variables}, "panels": panels}

def website():
    """Website monitor (webmon/webmon.py -> measurement web_probe): image download times and speed."""
    var = {"name": "target", "label": "Image", "type": "query", "datasource": DS, "refresh": 2, "multi": True, "includeAll": True,
           "current": {}, "sort": 1, "query": 'import "influxdata/influxdb/schema"\n'
           f'schema.tagValues(bucket: "{B}", tag: "target", start: -30d, predicate: (r) => r._measurement == "web_probe")'}
    base = (f'from(bucket: "{B}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
            '  |> filter(fn: (r) => r._measurement == "web_probe" and contains(value: r.target, set: ${target:json}))\n')
    def per_target(field, agg="mean"):
        return (base + f'  |> filter(fn: (r) => r._field == "{field}")\n  |> group(columns: ["target"])\n'
                f'  |> aggregateWindow(every: v.windowPeriod, fn: {agg}, createEmpty: false)\n'
                '  |> map(fn: (r) => ({_time: r._time, _value: r._value, _field: r.target}))\n  |> group(columns: ["_field"])')
    def chart(title, flux, unit, y, desc, stack=False, bars=False):
        custom = {"lineWidth": 2, "fillOpacity": 70 if stack or bars else 0, "showPoints": "auto", "spanNulls": 10800000,
                  "drawStyle": "bars" if bars else "line", "stacking": {"mode": "normal" if stack else "none"}}
        return {"type": "timeseries", "title": title, "description": desc, "datasource": DS,
                "gridPos": {"h": 9, "w": 24, "x": 0, "y": y}, "targets": [q(flux)],
                "fieldConfig": {"defaults": {"unit": unit, "decimals": 1, "min": 0, "color": {"mode": "palette-classic-by-name"},
                                             "custom": custom}, "overrides": []},
                "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": [] if bars else ["lastNotNull", "mean", "max"]},
                            "tooltip": {"mode": "multi", "sort": "desc"}}}
    def stat(title, flux, unit, x, desc, thr=None):
        return {"type": "stat", "title": title, "description": desc, "datasource": DS, "gridPos": {"h": 4, "w": 6, "x": x, "y": 0},
                "targets": [q(flux)], "fieldConfig": {"defaults": {
                    "unit": unit, "decimals": 1, "color": {"mode": "thresholds"},
                    "thresholds": thr or {"mode": "absolute", "steps": [{"color": TEMP_LINE, "value": None}]}}, "overrides": []},
                "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none", "textMode": "value"}}
    okthr = {"mode": "absolute", "steps": [{"color": ALARM, "value": None}, {"color": WATCH, "value": 95}, {"color": OK, "value": 99}]}
    one = lambda field, fn: base + f'  |> filter(fn: (r) => r._field == "{field}")\n  |> toFloat()\n  |> group()\n  |> {fn}'
    stats = [stat("Median total time", one("total_ms", "median()"), "ms", 0, "Half of the successful downloads in the period were faster than this (DNS + connect + TLS + server wait + transfer)."),
             stat("Slowest 5 %", one("total_ms", "quantile(q: 0.95)"), "ms", 6, "95th percentile of total download time: what a visitor sees at a bad moment."),
             stat("Median download speed", one("mbps", "median()"), "Mbits", 12, "Transfer speed of the image body. Small images never reach full speed, so compare like with like."),
             stat("Success rate", one("ok", 'mean()\n  |> map(fn: (r) => ({r with _value: r._value * 100.0}))'), "percent", 18,
                  "Share of checks that returned the complete image (HTTP 200, full length).", okthr)]
    part = lambda field, fn, name: (f'{name} = base |> filter(fn: (r) => r._field == "{field}") |> toFloat() |> group(columns: ["target", "host"]) |> {fn}\n'
                                    f'  |> map(fn: (r) => ({{target: r.target, host: r.host, _field: "{name}", _value: r._value}}))\n')
    table_flux = ('base = ' + base +
                  part("ok", "last()", "last_ok") + part("status", "last()", "last_status") + part("bytes", "last()", "size") +
                  part("total_ms", "median()", "med_ms") + part("total_ms", "quantile(q: 0.95)", "p95_ms") +
                  part("ttfb_ms", "median()", "ttfb") + part("mbps", "median()", "speed") +
                  part("ok", 'mean() |> map(fn: (r) => ({r with _value: r._value * 100.0}))', "pct_ok") +
                  part("ok", "count() |> toFloat()", "checks") +
                  'age = base |> filter(fn: (r) => r._field == "ok") |> group(columns: ["target", "host"]) |> last()\n'
                  '  |> map(fn: (r) => ({target: r.target, host: r.host, _field: "age_min", _value: float(v: int(v: now()) - int(v: r._time)) / 60000000000.0}))\n'
                  'union(tables: [last_ok, last_status, size, med_ms, p95_ms, ttfb, speed, pct_ok, checks, age])\n'
                  '  |> group()\n  |> pivot(rowKey: ["target", "host"], columnKey: ["_field"], valueColumn: "_value")\n  |> sort(columns: ["target"])')
    lastmap = [{"type": "value", "options": {"1": {"text": "OK", "color": OK}, "0": {"text": "Failed", "color": ALARM}}}]
    stale = {"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": WATCH, "value": 90}]}
    table = {"type": "table", "title": "Website images", "datasource": DS, "gridPos": {"h": 7, "w": 24, "x": 0, "y": 4},
             "description": "One row per monitored image for the selected period. Last check turns amber when no check has run for 90 minutes (the webmon container may be stopped).",
             "targets": [q(table_flux)], "fieldConfig": {"defaults": {"decimals": 0}, "overrides": [
                 by_name("target", ("displayName", "Image")), by_name("host", ("displayName", "Server")),
                 by_name("last_ok", ("displayName", "Last result"), ("mappings", lastmap), ("custom.cellOptions", {"type": "color-text"})),
                 by_name("last_status", ("displayName", "HTTP")), by_name("size", ("displayName", "Size"), ("unit", "decbytes"), ("decimals", 1)),
                 by_name("med_ms", ("displayName", "Median time"), ("unit", "ms")), by_name("p95_ms", ("displayName", "Slowest 5 %"), ("unit", "ms")),
                 by_name("ttfb", ("displayName", "Server wait"), ("unit", "ms")), by_name("speed", ("displayName", "Speed"), ("unit", "Mbits"), ("decimals", 1)),
                 by_name("pct_ok", ("displayName", "Success"), ("unit", "percent"), ("decimals", 1), ("thresholds", okthr), ("custom.cellOptions", {"type": "color-text"})),
                 by_name("checks", ("displayName", "Checks")),
                 by_name("age_min", ("displayName", "Last check"), ("unit", "m"), ("thresholds", stale), ("custom.cellOptions", {"type": "color-text"}))]}}
    phases = (base + '  |> filter(fn: (r) => contains(value: r._field, set: ["dns_ms", "connect_ms", "tls_ms", "ttfb_ms", "download_ms"]))\n'
              '  |> group(columns: ["_field"])\n  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n'
              '  |> map(fn: (r) => ({r with _field: if r._field == "dns_ms" then "1 DNS lookup" else if r._field == "connect_ms" then "2 Connect"\n'
              '      else if r._field == "tls_ms" then "3 TLS handshake" else if r._field == "ttfb_ms" then "4 Server wait" else "5 Transfer"}))')
    fails = (base + '  |> filter(fn: (r) => r._field == "ok")\n  |> toFloat()\n'
             '  |> map(fn: (r) => ({r with _value: 1.0 - r._value}))\n'
             '  |> group(columns: ["target"])\n  |> aggregateWindow(every: 1h, fn: sum, createEmpty: false)\n  |> filter(fn: (r) => r._value > 0.0)\n'
             '  |> map(fn: (r) => ({_time: r._time, _value: r._value, _field: r.target}))\n  |> group(columns: ["_field"])')
    recent = (base + '  |> filter(fn: (r) => r._field == "error" or r._field == "status")\n'
              '  |> map(fn: (r) => ({r with _value: string(v: r._value)}))\n  |> group()\n'
              '  |> pivot(rowKey: ["_time", "target"], columnKey: ["_field"], valueColumn: "_value")\n'
              '  |> filter(fn: (r) => exists r.error)\n  |> keep(columns: ["_time", "target", "status", "error"])\n'
              '  |> sort(columns: ["_time"], desc: true)\n  |> limit(n: 50)')
    errtxt = ("dns = name lookup failed · connect = server refused/unreachable · tls = certificate/HTTPS problem · "
              "timeout = no answer within 60 s · http_NNN = server error code (404 missing, 5xx server fault) · truncated = image cut short")
    rtable = {"type": "table", "title": "Recent failures", "datasource": DS, "gridPos": {"h": 8, "w": 24, "x": 0, "y": 47},
              "description": errtxt, "targets": [q(recent)], "fieldConfig": {"defaults": {}, "overrides": [
                  by_name("_time", ("displayName", "Time")), by_name("target", ("displayName", "Image")),
                  by_name("status", ("displayName", "HTTP")), by_name("error", ("displayName", "Problem"))]}}
    panels = stats + [table,
              chart("Total download time", per_target("total_ms"), "ms", 11, "Start of the check to the last byte, per image. Spikes on every image at once point to our network or the whole site; one image alone points to that file or its server."),
              chart("Download speed", per_target("mbps"), "Mbits", 20, "Transfer rate of the image body (excludes connection setup)."),
              chart("Where the time goes", phases, "ms", 29, "Average time per step across the selected images, stacked. Large DNS = name lookup problem; Connect/TLS = network distance or busy server; Server wait = slow website backend; Transfer = bandwidth.", stack=True),
              chart("Failed checks per hour", fails, "short", 38, errtxt, bars=True), rtable]
    d = dash("archmon-web", "Archive Conditions — Website", panels, [var])
    d["time"] = {"from": "now-24h", "to": "now"}
    return d

# ---------------- Alerting ----------------
EXCL = 'r.room_type != "test" and r.room_type != "unregistered"'

def flux_q(ref, flux, secs=900):
    return {"refId": ref, "relativeTimeRange": {"from": secs, "to": 0}, "datasourceUid": "archmon-influx", "model": {"refId": ref, "query": flux}}

def expr(ref, model):
    return {"refId": ref, "datasourceUid": "__expr__", "model": {"refId": ref, **model}}

def status_data(cond_ref, evaluator):
    a = ('import "math"\nf = (c) => c * 1.8 + 32.0\nr0 = (x) => string(v: int(v: math.round(x: x)))\n'
         f'from(bucket: "{B}")\n  |> range(start: -15m)\n'
         f'  |> filter(fn: (r) => r._measurement == "environment" and {EXCL})\n'
         '  |> filter(fn: (r) => contains(value: r._field, set: ["status", "t_status", "rh_status", "t_min_c", "t_max_c", "rh_min", "rh_max", "temperature_c", "humidity_rh"]))\n'
         '  |> toFloat()\n  |> group(columns: ["building", "room", "sensor_id"])\n'
         '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
         '  |> map(fn: (r) => {\n'
         '      tc = if not exists r.t_status then "" else if r.t_status == 0.0 then "" else if r.temperature_c > r.t_max_c then "too warm" else "too cold"\n'
         '      hc = if not exists r.rh_status then "" else if r.rh_status == 0.0 then "" else if r.humidity_rh > r.rh_max then "too humid" else "too dry"\n'
         '      return {_time: r._time, _value: r.status, room: r.building + " / " + r.room,\n'
         '        cause: if tc != "" and hc != "" then tc + " and " + hc else if tc != "" then tc else if hc != "" then hc else "in range",\n'
         '        target: r0(x: f(c: r.t_min_c)) + "-" + r0(x: f(c: r.t_max_c)) + " °F, " + r0(x: r.rh_min) + "-" + r0(x: r.rh_max) + " % RH"}\n'
         '    })\n  |> group(columns: ["room", "cause", "target"])')
    def now(field, conv):
        return (f'from(bucket: "{B}")\n  |> range(start: -15m)\n'
                f'  |> filter(fn: (r) => r._measurement == "environment" and r._field == "{field}" and {EXCL})\n'
                f'  |> map(fn: (r) => ({{_time: r._time, _value: {conv}, room: r.building + " / " + r.room}}))\n  |> group(columns: ["room"])')
    return [flux_q("A", a), flux_q("T", now("temperature_c", "r._value * 1.8 + 32.0")), flux_q("H", now("humidity_rh", "r._value")),
            expr("B", {"type": "reduce", "expression": "A", "reducer": "last", "settings": {"mode": "dropNN"}}),
            expr("TL", {"type": "reduce", "expression": "T", "reducer": "last", "settings": {"mode": "dropNN"}}),
            expr("HL", {"type": "reduce", "expression": "H", "reducer": "last", "settings": {"mode": "dropNN"}}),
            expr(cond_ref, {"type": "threshold", "expression": "B", "conditions": [{"evaluator": evaluator}]})]

NOW_TXT = 'Now {{ printf "%.1f" $values.TL.Value }} °F, {{ printf "%.0f" $values.HL.Value }} % RH. Target {{ $labels.target }}.'

def alerting():
    offline = (f'from(bucket: "{B}")\n  |> range(start: -30d)\n'
               f'  |> filter(fn: (r) => r._measurement == "environment" and r._field == "temperature_c" and {EXCL})\n'
               '  |> group(columns: ["building", "room", "sensor_id"])\n  |> last()\n'
               '  |> map(fn: (r) => ({_time: now(), _value: float(v: int(v: now()) - int(v: r._time)) / 60000000000.0,\n'
               '      room: r.building + " / " + r.room, sensor_id: r.sensor_id}))\n  |> group(columns: ["room", "sensor_id"])')
    offline = offline.replace('r._measurement == "environment" and r._field == "temperature_c" and',
                              '((r._measurement == "environment" and r._field == "temperature_c") or (r._measurement == "leak" and r._field == "water")'
                              ' or (r._measurement == "air_quality" and r._field == "status")'
                              ' or (r._measurement == "occupancy" and r._field == "motion")) and')
    def aq_rule(uid, title, thr, dur, sev):
        why = " + ".join(f'(if exists r.{k}_status and r.{k}_status > {thr - 1.0} then "{AQ_INFO[k][0]} high " else "")' for k in AQ_LIMITS)
        a = (f'import "strings"\nfrom(bucket: "{B}")\n  |> range(start: -15m)\n'
             f'  |> filter(fn: (r) => r._measurement == "air_quality" and {EXCL})\n  |> toFloat()\n'
             '  |> group(columns: ["building", "room", "sensor_id"])\n'
             '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
             f'  |> map(fn: (r) => ({{_time: r._time, _value: r.status, room: r.building + " / " + r.room, sensor_id: r.sensor_id,\n'
             f'      cause: "air quality: " + strings.trimSpace(v: {why})}}))\n'
             '  |> group(columns: ["room", "sensor_id", "cause"])')
        return {"uid": uid, "title": title, "condition": "C", "for": dur, "noDataState": "OK", "execErrState": "Error",
                "labels": {"severity": sev},
                "annotations": {"summary": "{{ $labels.room }}: {{ $labels.cause }}",
                                "description": "{{ $labels.cause }} at {{ $labels.sensor_id }} for " + dur + "+. See the Leaks & Air Quality dashboard."},
                "data": [flux_q("A", a), expr("B", {"type": "reduce", "expression": "A", "reducer": "last", "settings": {"mode": "dropNN"}}),
                         expr("C", {"type": "threshold", "expression": "B", "conditions": [{"evaluator": {"type": "gt", "params": [thr - 0.5]}}]})]}
    leak = (f'from(bucket: "{B}")\n  |> range(start: -5m)\n'
            f'  |> filter(fn: (r) => r._measurement == "leak" and r._field == "water" and {EXCL})\n'
            '  |> group(columns: ["building", "room", "sensor_id"])\n  |> max()\n'
            '  |> map(fn: (r) => ({_time: now(), _value: float(v: r._value), room: r.building + " / " + r.room, sensor_id: r.sensor_id}))\n'
            '  |> group(columns: ["room", "sensor_id"])')
    web = (f'from(bucket: "{B}")\n  |> range(start: -45m)\n'
           '  |> filter(fn: (r) => r._measurement == "web_probe" and r._field == "ok")\n  |> toFloat()\n'
           '  |> group(columns: ["target", "host"])\n'
           '  |> reduce(identity: {n: 0.0, s: 0.0}, fn: (r, accumulator) => ({n: accumulator.n + 1.0, s: accumulator.s + r._value}))\n'
           '  |> map(fn: (r) => ({_time: now(), _value: if r.n >= 2.0 then r.s / r.n * 100.0 else 100.0, room: "Website · " + r.target, host: r.host}))\n'
           '  |> group(columns: ["room", "host"])')
    rules = [
        {"uid": "archmon-water-leak", "title": "Water leak", "condition": "C", "for": "0s",
         "noDataState": "OK", "execErrState": "Error", "labels": {"severity": "alarm", "cause": "WATER DETECTED"},
         "annotations": {"summary": "{{ $labels.room }}: water detected", "description": "Leak probe {{ $labels.sensor_id }} is wet. Check the room now and protect collections."},
         "data": [flux_q("A", leak, 300), expr("B", {"type": "reduce", "expression": "A", "reducer": "last", "settings": {"mode": "dropNN"}}),
                  expr("C", {"type": "threshold", "expression": "B", "conditions": [{"evaluator": {"type": "gt", "params": [0.5]}}]})]},
        aq_rule("archmon-air-alarm", "Air quality alarm", 2.0, "30m", "alarm"),
        aq_rule("archmon-air-watch", "Air quality watch", 1.0, "2h", "watch"),
        {"uid": "archmon-room-alarm", "title": "Room alarm", "condition": "C", "for": "10m",
         "noDataState": "OK", "execErrState": "Error", "labels": {"severity": "alarm"},
         "annotations": {"summary": "{{ $labels.room }}: {{ $labels.cause }}", "description": "ALARM for 10+ min. " + NOW_TXT},
         "data": status_data("C", {"type": "gt", "params": [1.5]})},
        {"uid": "archmon-room-watch", "title": "Room watch", "condition": "C", "for": "1h",
         "noDataState": "OK", "execErrState": "Error", "labels": {"severity": "watch"},
         "annotations": {"summary": "{{ $labels.room }}: {{ $labels.cause }}", "description": "Outside target for 1+ hour. " + NOW_TXT},
         "data": status_data("C", {"type": "gt", "params": [0.5]})},
        {"uid": "archmon-rh-swing", "title": "Large daily RH swing", "condition": "C", "for": "0s",
         "noDataState": "OK", "execErrState": "Error", "labels": {"severity": "watch", "cause": "large daily humidity swing"},
         "annotations": {"summary": "{{ $labels.room }}: large daily humidity swing",
                         "description": 'RH varied {{ printf "%.1f" $values.B.Value }} % more than the allowed {{ $labels.allowed }} % range in the last 24 h. Repeated swings stress paper, parchment and bindings.'},
         "data": [flux_q("A", f'from(bucket: "{B}")\n  |> range(start: -24h)\n'
                              f'  |> filter(fn: (r) => r._measurement == "environment" and (r._field == "humidity_rh" or r._field == "rh_swing_max") and {EXCL})\n'
                              '  |> group(columns: ["building", "room", "_field"])\n'
                              '  |> reduce(identity: {n: 0.0, lo: 1000.0, hi: -1000.0}, fn: (r, accumulator) => ({n: accumulator.n + 1.0,\n'
                              '      lo: if r._value < accumulator.lo then r._value else accumulator.lo, hi: if r._value > accumulator.hi then r._value else accumulator.hi}))\n'
                              '  |> filter(fn: (r) => r._field != "humidity_rh" or r.n >= 1000.0)\n'
                              '  |> map(fn: (r) => ({building: r.building, room: r.room, _field: r._field, v: if r._field == "humidity_rh" then r.hi - r.lo else r.hi}))\n'
                              '  |> group(columns: ["building", "room"])\n'
                              '  |> pivot(rowKey: ["building", "room"], columnKey: ["_field"], valueColumn: "v")\n'
                              '  |> filter(fn: (r) => exists r.humidity_rh and exists r.rh_swing_max)\n'
                              '  |> map(fn: (r) => ({_time: now(), _value: r.humidity_rh - 2.0 * r.rh_swing_max, room: r.building + " / " + r.room,\n'
                              '      allowed: string(v: int(v: 2.0 * r.rh_swing_max))}))\n'
                              '  |> group(columns: ["room", "allowed"])', 86400),
                  expr("B", {"type": "reduce", "expression": "A", "reducer": "last", "settings": {"mode": "dropNN"}}),
                  expr("C", {"type": "threshold", "expression": "B", "conditions": [{"evaluator": {"type": "gt", "params": [0]}}]})]},
        {"uid": "archmon-sensor-offline", "title": "Sensor offline", "condition": "C", "for": "0s",
         "noDataState": "OK", "execErrState": "Error", "labels": {"severity": "sensor", "cause": "sensor offline"},
         "annotations": {"summary": "{{ $labels.room }}: sensor offline",
                         "description": 'No reading from {{ $labels.sensor_id }} for {{ printf "%.0f" $values.B.Value }} min. Check power and Wi-Fi.'},
         "data": [flux_q("A", offline, 86400),
                  expr("B", {"type": "reduce", "expression": "A", "reducer": "last", "settings": {"mode": "dropNN"}}),
                  expr("C", {"type": "threshold", "expression": "B", "conditions": [{"evaluator": {"type": "gt", "params": [15]}}]})]},
        {"uid": "archmon-mould-risk", "title": "Mould risk", "condition": "C", "for": "0s",
         "noDataState": "OK", "execErrState": "Error", "labels": {"severity": "alarm", "cause": "mould risk (RH above 65 % for 48 h)"},
         "annotations": {"summary": "{{ $labels.room }}: mould risk",
                         "description": 'RH has stayed above 65 % for 48 hours (lowest {{ printf "%.0f" $values.B.Value }} %). Inspect for mould and dry the room.'},
         "data": [flux_q("A", f'from(bucket: "{B}")\n  |> range(start: -48h)\n'
                              f'  |> filter(fn: (r) => r._measurement == "environment" and r._field == "humidity_rh" and {EXCL})\n'
                              '  |> group(columns: ["building", "room"])\n  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)\n'
                              '  |> reduce(identity: {n: 0.0, lo: 1000.0}, fn: (r, accumulator) => ({n: accumulator.n + 1.0, lo: if r._value < accumulator.lo then r._value else accumulator.lo}))\n'
                              '  |> map(fn: (r) => ({_time: now(), _value: if r.n >= 44.0 then r.lo else 0.0, room: r.building + " / " + r.room}))\n  |> group(columns: ["room"])', 172800),
                  expr("B", {"type": "reduce", "expression": "A", "reducer": "last", "settings": {"mode": "dropNN"}}),
                  expr("C", {"type": "threshold", "expression": "B", "conditions": [{"evaluator": {"type": "gt", "params": [65]}}]})]},
        {"uid": "archmon-website-failing", "title": "Website check failing", "condition": "C", "for": "0s",
         "noDataState": "OK", "execErrState": "Error", "labels": {"severity": "watch", "cause": "website check failing"},
         "annotations": {"summary": "{{ $labels.room }}: website check failing",
                         "description": 'Only {{ printf "%.0f" $values.B.Value }} % of checks of {{ $labels.host }} succeeded in the last 45 min. See the Website dashboard; check from another network before contacting the web host.'},
         "data": [flux_q("A", web, 2700),
                  expr("B", {"type": "reduce", "expression": "A", "reducer": "last", "settings": {"mode": "dropNN"}}),
                  expr("C", {"type": "threshold", "expression": "B", "conditions": [{"evaluator": {"type": "lt", "params": [50]}}]})]},
        {"uid": "archmon-sensor-stuck", "title": "Sensor needs checking", "condition": "C", "for": "0s",
         "noDataState": "OK", "execErrState": "Error", "labels": {"severity": "sensor", "cause": "sensor reading stuck"},
         "annotations": {"summary": "{{ $labels.room }}: sensor reading stuck",
                         "description": "{{ $labels.sensor_id }} has reported exactly the same humidity for 6 hours. The sensor element may have failed; compare with a handheld meter."},
         "data": [flux_q("A", f'from(bucket: "{B}")\n  |> range(start: -6h)\n'
                              f'  |> filter(fn: (r) => r._measurement == "environment" and r._field == "humidity_rh" and {EXCL})\n'
                              '  |> group(columns: ["building", "room", "sensor_id"])\n'
                              '  |> reduce(identity: {n: 0.0, lo: 1000.0, hi: -1000.0}, fn: (r, accumulator) => ({n: accumulator.n + 1.0,\n'
                              '      lo: if r._value < accumulator.lo then r._value else accumulator.lo, hi: if r._value > accumulator.hi then r._value else accumulator.hi}))\n'
                              '  |> map(fn: (r) => ({_time: now(), _value: if r.n >= 300.0 then r.hi - r.lo else 1.0, room: r.building + " / " + r.room, sensor_id: r.sensor_id}))\n'
                              '  |> group(columns: ["room", "sensor_id"])', 21600),
                  expr("B", {"type": "reduce", "expression": "A", "reducer": "last", "settings": {"mode": "dropNN"}}),
                  expr("C", {"type": "threshold", "expression": "B", "conditions": [{"evaluator": {"type": "lt", "params": [0.05]}}]})]},
    ]
    def cp(name, prio, topic="archmon-alerts"):
        return {"orgId": 1, "name": name, "receivers": [{"uid": "archmon-" + name, "type": "webhook", "settings": {
            "url": f"http://ntfy:80/{topic}?template=archmon", "httpMethod": "POST",  # priority set by ntfy/templates/archmon.yml
            "username": "grafana", "password": "${NTFY_GRAFANA_PASSWORD}",
            "title": "{{ .CommonLabels.room }}: {{ .CommonLabels.cause }}",
            "message": "{{ range .Alerts }}{{ .Annotations.description }}\n{{ end }}"}}]}
    return {"apiVersion": 1,
            "contactPoints": [cp("ntfy-alarm", "high"), cp("ntfy-normal", "default"), cp("ntfy-sim", "default", "archmon-sim")],
            "policies": [{"orgId": 1, "receiver": "ntfy-normal", "group_by": ["alertname", "room", "cause"],
                          "group_wait": "30s", "group_interval": "5m", "repeat_interval": "12h",
                          "routes": [{"receiver": "ntfy-sim", "object_matchers": [["room", "=~", SIM_ROOM_RE]]},
                                     {"receiver": "ntfy-alarm", "object_matchers": [["severity", "=", "alarm"]], "repeat_interval": "4h"}]}],
            "groups": [{"orgId": 1, "name": "archival-conditions", "folder": "Archival Conditions", "interval": "1m", "rules": rules}]}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for d in (overview(), air_quality(), room_detail(), health(), compliance(), website(), settings()):
        (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        print("wrote", d["uid"])
    ALERT_OUT.parent.mkdir(parents=True, exist_ok=True)
    ALERT_OUT.write_text("# GENERATED by scripts/build_dashboards.py - do not edit by hand.\n"
                         "# Alert templates use plain $labels/$values; avoid $ in SSE math expressions (Grafana env expansion eats it).\n"
                         + json.dumps(alerting(), indent=2, ensure_ascii=False), encoding="utf-8")
    print("wrote", ALERT_OUT.name)

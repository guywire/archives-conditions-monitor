# Turns any Tasmota sensor payload into a standard "environment" metric:
#   temperature_c, humidity_rh (calibrated), dew_point_c (Magnus, a=17.62 b=243.12)
#   t_min_c/t_max_c/rh_min/rh_max (room profile band) and status: 0 OK, 1 watch, 2 alarm
# Tasmota must report Celsius (SetOption8 0).
# Also: water-leak probes (Switch1 ON = water) -> "leak" metric, and air-quality sensors (SEN5X, SCD40, PMS5003,
# SGP30...) -> "air_quality" metric. One payload can yield several metrics.
load("math.star", "math")

T_MARGIN = 1.0   # degC outside band = watch; beyond = alarm
RH_MARGIN = 5.0  # %RH outside band = watch; beyond = alarm
# Air-quality limits: field -> (Tasmota key suffix, watch above, alarm above). Archive guidance (ISO 11799,
# PAS 198, Sensirion index scales); scripts/build_dashboards.py reads this line, keep it a one-line dict.
AQ_LIMITS = {"pm25": ("_PM2.5", 10.0, 25.0), "pm10": ("_PM10", 20.0, 50.0), "co2_ppm": ("_CarbonDioxide", 1000.0, 2000.0), "voc_index": ("_VOC_Index", 200.0, 300.0), "nox_index": ("_NOx_Index", 20.0, 100.0)}
DROP_TAGS = ["cal_t", "cal_rh", "installed", "t_min_c", "t_max_c", "rh_min", "rh_max", "rh_swing_24h", "notes",
             "o_t_min_c", "o_t_max_c", "o_rh_min", "o_rh_max"]

def level(v, lo, hi, margin):
    if v >= lo and v <= hi:
        return 0
    if v >= lo - margin and v <= hi + margin:
        return 1
    return 2

def num(tags, k):
    v = tags.get("o_" + k, "")   # per-room override (registry/room_overrides.csv) wins when set
    if v == "":
        v = tags.get(k, "")
    if v == "":
        return None
    return float(v)

def copy_tags(metric, name):
    out = Metric(name)
    for k, v in metric.tags.items():
        if k not in DROP_TAGS:
            out.tags[k] = v
    if "room_type" not in out.tags:
        out.tags["room_type"] = "unregistered"
        out.tags["room"] = "Unregistered " + out.tags.get("sensor_id", "?")
    return out

def extras(metric):
    res = []
    sw = metric.fields.get("Switch1")
    if sw != None:
        m = copy_tags(metric, "leak")
        wet = 1 if str(sw).upper() in ("ON", "1") else 0
        m.fields["water"] = wet
        m.fields["status"] = 2 * wet
        res.append(m)
    aq = copy_tags(metric, "air_quality")
    status = None
    for field, spec in AQ_LIMITS.items():
        for k, v in metric.fields.items():
            if k.endswith(spec[0]) and field not in aq.fields:
                v = float(v)
                s = 2 if v > spec[2] else (1 if v > spec[1] else 0)
                aq.fields[field] = v
                aq.fields[field + "_status"] = s
                status = s if status == None else max(status, s)
    if status != None:
        aq.fields["status"] = status
        res.append(aq)
    return res

def apply(metric):
    res = extras(metric)
    env = climate(metric)
    if env != None:
        res.append(env)
    return res

def climate(metric):
    t = None
    h = None
    for k, v in metric.fields.items():
        if k.endswith("_Temperature") and t == None:
            t = float(v)
        elif k.endswith("_Humidity") and h == None:
            h = float(v)
    if t == None and h == None:
        return None

    out = copy_tags(metric, "environment")

    if t != None:
        t = t + float(metric.tags.get("cal_t", "0") or "0")
        out.fields["temperature_c"] = t
    if h != None:
        h = h + float(metric.tags.get("cal_rh", "0") or "0")
        h = max(0.1, min(100.0, h))
        out.fields["humidity_rh"] = h
    if t != None and h != None:
        g = math.log(h / 100.0) + (17.62 * t) / (243.12 + t)
        out.fields["dew_point_c"] = (243.12 * g) / (17.62 - g)

    tmin, tmax = num(metric.tags, "t_min_c"), num(metric.tags, "t_max_c")
    rmin, rmax = num(metric.tags, "rh_min"), num(metric.tags, "rh_max")
    status = None
    if tmin != None and tmax != None and t != None:
        out.fields["t_min_c"] = tmin
        out.fields["t_max_c"] = tmax
        status = level(t, tmin, tmax, T_MARGIN)
        out.fields["t_status"] = status
    if rmin != None and rmax != None and h != None:
        out.fields["rh_min"] = rmin
        out.fields["rh_max"] = rmax
        s = level(h, rmin, rmax, RH_MARGIN)
        out.fields["rh_status"] = s
        status = s if status == None else max(status, s)
    if status != None:
        out.fields["status"] = status
    swing = num(metric.tags, "rh_swing_24h")
    if swing != None:
        out.fields["rh_swing_max"] = swing

    return out

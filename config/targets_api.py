"""Target-range editor API for the Grafana "Settings" dashboard (Business Forms panel).

Reached only through the nginx proxy at /archmon-api/ (same origin as Grafana), never published directly.
Authentication: the caller's own Grafana session cookie is checked against Grafana (/api/user/orgs).
  GET  /targets   any logged-in Grafana user: current values, °F / %RH
  POST /targets   Grafana Admin or Editor: validate, write registry CSVs, append to the audit log,
                  make Telegraf reload (touch telegraf.conf; it runs with --watch-config poll) and rebuild dashboards.
  GET  /update    any logged-in user: software version and update status (written by scripts/update.sh on the host)
  POST /update    Grafana Admin only: {"action": "check"|"install"} -> state/update-request.json for the host updater.
                  This container never runs Git or Docker; it can only ask for the newest signed release.
Standard library only."""
import csv, datetime, io, json, os, re, subprocess, sys, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = "/app"
PROFILES = f"{ROOT}/registry/room_profiles.csv"
OVERRIDES = f"{ROOT}/registry/room_overrides.csv"
SENSORS = f"{ROOT}/registry/sensors.csv"
AUDIT = f"{ROOT}/registry/targets-changes.log"
UPDATE_STATUS = f"{ROOT}/state/update-status.json"
UPDATE_REQUEST = f"{ROOT}/state/update-request.json"
TELEGRAF_CONF = f"{ROOT}/telegraf/telegraf.conf"
GRAFANA = os.environ.get("GRAFANA_URL", "http://grafana:3000")
EDIT_ROLES = {"Admin", "Editor"}
LOCK = threading.Lock()
FIELDS = [("t_min", "t_min_c"), ("t_max", "t_max_c"), ("rh_min", "rh_min"), ("rh_max", "rh_max")]

def slug(building, room):  # must match scripts/build_dashboards.py
    return re.sub(r"[^a-z0-9]+", "_", f"{building}_{room}".lower()).strip("_")

def c2f(c): return round(float(c) * 1.8 + 32, 1)
def f2c(f): return round((float(f) - 32) / 1.8, 3)

def read_csv(path):
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        return r.fieldnames, list(r)

def write_csv(path, header, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, header, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)

def rooms():
    _, rows = read_csv(SENSORS)
    seen = {}
    for r in rows:
        if r["room_type"] != "test":
            seen.setdefault(slug(r["building"], r["room"]), (r["building"], r["room"]))
    return seen

def current():
    out = {}
    _, profiles = read_csv(PROFILES)
    for p in profiles:
        rt = p["room_type"]
        out[f"{rt}__t_min"], out[f"{rt}__t_max"] = c2f(p["t_min_c"]), c2f(p["t_max_c"])
        out[f"{rt}__rh_min"], out[f"{rt}__rh_max"] = float(p["rh_min"]), float(p["rh_max"])
        out[f"{rt}__swing"] = float(p["rh_swing_24h"])
    _, ovr = read_csv(OVERRIDES)
    by_key = {o["room_key"]: o for o in ovr}
    for s, (b, rm) in rooms().items():
        o = by_key.get(f"{b}|{rm}", {})
        for short, col in FIELDS:
            v = o.get("o_" + col, "")
            out[f"o_{s}__{short}"] = "" if v == "" else (c2f(v) if col.startswith("t_") else float(v))
    try:
        with open(AUDIT, encoding="utf-8") as fh:
            out["audit"] = "".join(fh.readlines()[-15:][::-1]) or "No changes yet."
    except FileNotFoundError:
        out["audit"] = "No changes yet."
    return out

def num(v, what, lo, hi, blank_ok=False):
    if v is None or str(v).strip() == "":
        if blank_ok:
            return None
        raise ValueError(f"{what}: a value is required")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: '{v}' is not a number")
    if not lo <= x <= hi:
        raise ValueError(f"{what}: {x:g} is outside {lo:g} to {hi:g}")
    return x

def pair(lo, hi, what):
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError(f"{what}: minimum ({lo:g}) must be below maximum ({hi:g})")

def apply(body, user):
    header, profiles = read_csv(PROFILES)
    changes = []
    for p in profiles:
        rt = p["room_type"]
        if rt == "test" or f"{rt}__t_min" not in body:
            continue
        name = rt.replace("_", " ")
        tmin, tmax = num(body[f"{rt}__t_min"], f"{name} temp min", -40, 120), num(body[f"{rt}__t_max"], f"{name} temp max", -40, 120)
        rmin, rmax = num(body[f"{rt}__rh_min"], f"{name} RH min", 0, 100), num(body[f"{rt}__rh_max"], f"{name} RH max", 0, 100)
        swing = num(body.get(f"{rt}__swing", p["rh_swing_24h"]), f"{name} RH swing", 0, 100)
        pair(tmin, tmax, f"{name} temperature")
        pair(rmin, rmax, f"{name} RH")
        new = {"t_min_c": f2c(tmin), "t_max_c": f2c(tmax), "rh_min": rmin, "rh_max": rmax, "rh_swing_24h": swing}
        for k, v in new.items():
            if abs(float(p[k]) - v) > 0.01:
                shown = (lambda c: f"{c2f(c):g} °F") if k.startswith("t_") else (lambda c: f"{float(c):g} %")
                changes.append(f"{name} {k.replace('_c', '').replace('_24h', '')}: {shown(p[k])} -> {shown(v)}")
                p[k] = f"{v:g}"
    ovr_rows = []
    _, old_ovr = read_csv(OVERRIDES)
    old = {o["room_key"]: o for o in old_ovr}
    for s, (b, rm) in rooms().items():
        key, vals = f"{b}|{rm}", {}
        label = f"{b} / {rm}"
        for short, col in FIELDS:
            is_t = col.startswith("t_")
            v = num(body.get(f"o_{s}__{short}", ""), f"{label} {short.replace('_', ' ')}", -40 if is_t else 0, 120 if is_t else 100, blank_ok=True)
            vals["o_" + col] = "" if v is None else f"{(f2c(v) if is_t else v):g}"
        pair(*(None if vals[f"o_{c}"] == "" else float(vals[f"o_{c}"]) for c in ("t_min_c", "t_max_c")), f"{label} temperature")
        pair(*(None if vals[f"o_{c}"] == "" else float(vals[f"o_{c}"]) for c in ("rh_min", "rh_max")), f"{label} RH")
        before = {k: old.get(key, {}).get(k, "") for k in vals}
        if before != vals:
            show = lambda k, v: "default" if v == "" else (f"{c2f(v):g} °F" if k.startswith("o_t") else f"{float(v):g} %")
            for k in vals:
                if before[k] != vals[k]:
                    changes.append(f"{label} {k[2:].replace('_c', '').replace('_', ' ')}: {show(k, before[k])} -> {show(k, vals[k])}")
        if any(vals.values()):
            ovr_rows.append({"room_key": key, **vals})
    if not changes:
        return "No changes."
    write_csv(PROFILES, header, profiles)
    write_csv(OVERRIDES, ["room_key", "o_t_min_c", "o_t_max_c", "o_rh_min", "o_rh_max"], ovr_rows)
    stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    with open(AUDIT, "a", encoding="utf-8") as fh:
        for c in changes:
            fh.write(f"{stamp}  {user}  {c}\n")
    os.utime(TELEGRAF_CONF)  # Telegraf (--watch-config poll) reloads registry lookups
    r = subprocess.run([sys.executable, f"{ROOT}/scripts/build_dashboards.py"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("dashboard rebuild failed:", r.stderr[-500:], flush=True)
    return f"Saved {len(changes)} change(s). Status uses the new targets within about a minute."

def update_status():
    try:
        with open(UPDATE_STATUS, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {"state": "unknown", "message": "The updater is not set up on this server (scripts/setup-updater.sh).",
             "installed": "", "latest": "", "latest_signed": False, "notes": "", "history": "", "log": ""}
    if os.path.exists(UPDATE_REQUEST):
        d["state"], d["message"] = "queued", "A request is waiting for the host updater."
    d["action"] = "check"
    return d

def request_update(body, user):
    action = body.get("action")
    if action not in ("check", "install"):
        raise ValueError("Choose 'Check for updates' or 'Install'.")
    st = update_status()
    if st["state"] in ("unknown", "queued", "updating") or (action == "install" and st["state"] == "checking"):
        raise ValueError(st["message"] if st["state"] != "updating" else "An update is already running.")
    req = {"action": action, "user": user}
    if action == "install":
        if not st.get("latest") or st["latest"] == st.get("installed"):
            raise ValueError("No newer release to install. Check for updates first.")
        if not st.get("latest_signed"):
            raise ValueError(f"{st['latest']} is not signed by the trusted key, so it cannot be installed.")
        req["tag"] = st["latest"]
    with open(UPDATE_REQUEST + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(req, fh)
    os.replace(UPDATE_REQUEST + ".tmp", UPDATE_REQUEST)
    if action == "install":
        return f"Installing {req['tag']}: backup, restart and health check take a few minutes. Reload this page to follow progress."
    return "Checking for updates; reload this page in a few seconds."

def grafana_user(cookie):
    """Returns (login, org-1 role) for the caller's Grafana session, or (None, None)."""
    if not cookie:
        return None, None
    try:
        def get(path):
            req = urllib.request.Request(GRAFANA + path, headers={"Cookie": cookie})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.load(resp)
        user = get("/api/user")
        role = next((o["role"] for o in get("/api/user/orgs") if o.get("orgId") == 1), None)
        return user.get("login"), role
    except Exception:
        return None, None

class Handler(BaseHTTPRequestHandler):
    server_version = "archmon-config"
    sys_version = ""

    def send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def auth(self, need=None):  # need: None (any login), "edit" (Editor/Admin), "admin"
        login, role = grafana_user(self.headers.get("Cookie", ""))
        if not login:
            self.send(401, {"error": "Please log in to Grafana."})
            return None
        if need == "edit" and role not in EDIT_ROLES:
            self.send(403, {"error": f"{login} is a {role or 'non-member'}; only Grafana Editors and Admins can change targets."})
            return None
        if need == "admin" and role != "Admin":
            self.send(403, {"error": f"{login} is a {role or 'non-member'}; only Grafana Admins can update the software."})
            return None
        return login

    def do_GET(self):
        path = self.path.rstrip("/")
        if path not in ("/targets", "/update"):
            return self.send(404, {"error": "not found"})
        if self.auth():
            self.send(200, current() if path == "/targets" else update_status())

    def do_POST(self):
        path = self.path.rstrip("/")
        if path not in ("/targets", "/update"):
            return self.send(404, {"error": "not found"})
        origin, host = self.headers.get("Origin"), self.headers.get("Host", "")
        if origin and origin.split("://", 1)[-1] != host:  # CSRF: same-origin requests only
            return self.send(403, {"error": "cross-origin request refused"})
        user = self.auth("edit" if path == "/targets" else "admin")
        if not user:
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 100_000:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(n) or b"{}")
            with LOCK:
                msg = apply(body, user) if path == "/targets" else request_update(body, user)
            print(f"{user}: {msg}", flush=True)
            self.send(200, {"ok": True, "message": msg})
        except ValueError as e:
            self.send(400, {"error": str(e)})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8095), Handler).serve_forever()

"""Website monitor: downloads images from our website on a jittered schedule and records how long each step took.

Targets: registry/web_targets.csv (re-read when it changes, no restart needed).
Output: InfluxDB line protocol, measurement `web_probe`, POSTed to Telegraf's internal listener (not published).
  tags   target, host
  fields ok (1/0), status, redirects, and on success dns_ms, connect_ms, tls_ms, ttfb_ms, download_ms,
         total_ms, bytes, mbps; on failure error (dns|connect|tls|timeout|http_<code>|truncated|other)
Downloads run one at a time so checks never compete with each other for bandwidth. Stdlib only.
"""
import csv, http.client, os, random, socket, ssl, time, urllib.request
from collections import deque
from urllib.parse import urljoin, urlsplit

TARGETS = os.environ.get("WEBMON_TARGETS", "/app/registry/web_targets.csv")
SINK = os.environ.get("WEBMON_SINK", "http://telegraf:8186/write")
TIMEOUT = float(os.environ.get("WEBMON_TIMEOUT_S", "60"))
MAX_BYTES = int(float(os.environ.get("WEBMON_MAX_MB", "50")) * 1e6)   # stop reading oversized files
MAX_REDIRECTS = 3
UA = "archmon-webmon/1.0 (site uptime and speed check)"
CTX = ssl.create_default_context()


def load_targets():
    """name,url,interval_min,jitter_pct,cache_bust  (lines starting with # are comments)."""
    out = {}
    with open(TARGETS, encoding="utf-8", newline="") as fh:
        rows = csv.DictReader(l for l in fh if l.strip() and not l.lstrip().startswith("#"))
        for r in rows:
            try:
                name, url = r["name"].strip(), r["url"].strip()
                if not name or urlsplit(url).scheme not in ("http", "https"):
                    raise ValueError("bad name/url")
                out[name] = {"url": url, "interval": max(1.0, float(r.get("interval_min") or 15)) * 60,
                             "jitter": min(0.9, max(0.0, float(r.get("jitter_pct") or 30) / 100)),
                             "cache_bust": (r.get("cache_bust") or "").strip().lower() in ("yes", "y", "true", "1")}
            except (KeyError, ValueError) as e:
                print(f"skipping row {r}: {e}")
    return out


def probe(url):
    """One download. Phase timings are for the final hop; total_ms covers every hop (redirects included)."""
    ms = lambda a, b: round((b - a) * 1000, 2)
    res, start = {"redirects": 0}, time.perf_counter()
    for _ in range(MAX_REDIRECTS + 1):
        u = urlsplit(url)
        https = u.scheme == "https"
        port = u.port or (443 if https else 80)
        stage = "dns"
        try:
            t = time.perf_counter()
            family, stype, proto, _, addr = socket.getaddrinfo(u.hostname, port, type=socket.SOCK_STREAM)[0]
            t1 = time.perf_counter(); res["dns_ms"] = ms(t, t1)
            stage = "connect"
            sock = socket.socket(family, stype, proto)
            sock.settimeout(TIMEOUT)
            sock.connect(addr)
            t2 = time.perf_counter(); res["connect_ms"] = ms(t1, t2)
            if https:
                stage = "tls"
                sock = CTX.wrap_socket(sock, server_hostname=u.hostname)
            t3 = time.perf_counter()
            if https:
                res["tls_ms"] = ms(t2, t3)
            stage = "other"
            conn = http.client.HTTPConnection(u.hostname, port, timeout=TIMEOUT)
            conn.sock = sock
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            conn.request("GET", path, headers={"Host": u.hostname + (f":{u.port}" if u.port else ""), "User-Agent": UA,
                                               "Accept": "image/*,*/*;q=0.8", "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            t4 = time.perf_counter(); res["ttfb_ms"] = ms(t3, t4)
            res["status"] = resp.status
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                resp.read(); conn.close()
                url = urljoin(url, resp.getheader("Location")); res["redirects"] += 1
                continue
            n = 0
            while chunk := resp.read(65536):
                n += len(chunk)
                if n >= MAX_BYTES:
                    break
            t5 = time.perf_counter(); conn.close()
            res.update(download_ms=ms(t4, t5), total_ms=ms(start, t5), bytes=n,
                       mbps=round(n * 8 / max(t5 - t4, 1e-6) / 1e6, 3))
            length = resp.getheader("Content-Length")
            if resp.status != 200:
                return fail(res, f"http_{resp.status}")
            if n == 0 or (length and length.isdigit() and int(length) != n and n < MAX_BYTES):
                return fail(res, "truncated")
            res["ok"] = 1
            return res
        except (socket.timeout, TimeoutError):
            return fail(res, "timeout")
        except socket.gaierror:
            return fail(res, "dns")
        except ssl.SSLError:
            return fail(res, "tls")
        except OSError:
            return fail(res, stage)
        except Exception:
            return fail(res, "other")
    return fail(res, "too_many_redirects")


def fail(res, error):
    # timings of a failed check would skew the speed graphs; keep only what explains the failure
    return {"ok": 0, "error": error, "status": res.get("status", 0), "redirects": res["redirects"]}


def esc_tag(v): return str(v).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def line(name, host, res):
    fields = []
    for k, v in res.items():
        if isinstance(v, str):
            fields.append(f'{k}="' + v.replace("\\", "\\\\").replace('"', '\\"') + '"')
        elif isinstance(v, int):
            fields.append(f"{k}={v}i")
        else:
            fields.append(f"{k}={v}")
    return f"web_probe,target={esc_tag(name)},host={esc_tag(host)} {','.join(fields)} {time.time_ns()}"


pending = deque(maxlen=2000)   # rides out Telegraf restarts


def flush():
    while pending:
        body = "\n".join(pending).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(SINK, data=body, method="POST"), timeout=10).read()
            pending.clear()
        except Exception as e:
            print(f"telegraf not reachable ({e}); {len(pending)} queued")
            return


def main():
    targets, due, mtime = {}, {}, None
    while True:
        try:
            m = os.stat(TARGETS).st_mtime
            if m != mtime:
                targets, mtime = load_targets(), m
                now = time.time()   # new targets start within a minute, spread out
                due = {n: due.get(n, now + random.uniform(5, 60)) for n in targets}
                print(f"{len(targets)} target(s): {', '.join(targets) or 'none'}")
        except OSError as e:
            if targets or mtime is not None:
                print(f"cannot read {TARGETS}: {e}")
            targets, due, mtime = {}, {}, None
        if not due:
            time.sleep(30); continue
        name = min(due, key=due.get)
        wait = due[name] - time.time()
        if wait > 0:
            time.sleep(min(wait, 30))   # wake regularly to notice edits to the targets file
            continue
        t = targets[name]
        url = t["url"]
        if t["cache_bust"]:
            url += ("&" if "?" in url else "?") + f"archmon={random.randrange(1 << 32):x}"
        res = probe(url)
        pending.append(line(name, urlsplit(t["url"]).hostname, res))
        flush()
        print(f"{name}: " + (f"{res['total_ms']:.0f} ms, {res['mbps']} Mbit/s" if res["ok"] else f"FAILED {res['error']}"))
        due[name] = time.time() + t["interval"] * random.uniform(1 - t["jitter"], 1 + t["jitter"])


if __name__ == "__main__":
    main()

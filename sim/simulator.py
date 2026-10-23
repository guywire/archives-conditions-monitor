"""Simulated Tasmota sensors for demos and testing (docker compose --profile sim up -d simulator).
Publishes the same MQTT payloads real devices send, so the whole pipeline (Telegraf, InfluxDB, dashboards,
alerts) is exercised. All simulated sensor_ids start with "sim-" and map to rooms named "... (sim)" in
registry/sensors.csv; their alerts go to the ntfy topic archmon-sim, not to staff.
Standard library only (minimal MQTT 3.1.1 client, QoS 0)."""
import json, math, os, random, socket, struct, time

HOST, PORT = os.environ.get("MQTT_HOST", "mosquitto"), 1883
USER, PASSWORD = os.environ.get("MQTT_USER", "sensor"), os.environ["MQTT_PASSWORD"]
PERIOD = int(os.environ.get("SIM_PERIOD", "60"))

def hour():  # local hour of day, 0..24 (site is US Eastern; close enough for diurnal shapes)
    return ((time.time() - 5 * 3600) % 86400) / 3600

def wave(peak_h, amp):  # daily sine, highest at peak_h
    return amp * math.cos((hour() - peak_h) / 24 * 2 * math.pi)

def window(every_h, minutes, offset_h=0.0):  # True during a recurring event window
    return ((time.time() / 3600 - offset_h) % every_h) * 60 < minutes

n = lambda s: random.gauss(0, s)

# Temperature / RH (SHT40, like the ST01). Each scenario returns (temp_c, rh).
CLIMATE = {
    "sim-cb-f1-vault-01": lambda: (16.5 + wave(15, 0.3) + n(0.05), 41 + wave(16, 1.5) + n(0.2)),
    # cold room: door opened every 3 h for 10 min -> short Watch, too short to alert
    "sim-cb-f1-cold-01": lambda: (5 + (3.5 if window(3, 10) else 0) + n(0.1), 35 + (6 if window(3, 10) else 0) + n(0.3)),
    "sim-cb-f1-humid-01": lambda: (20 + wave(15, 1) + n(0.1), 75 + wave(12, 5) + n(0.5)),
    # map room: RH hovering just above 50 % -> Watch most of the day (demo of watch alerts)
    "sim-cb-f1-map-01": lambda: (19 + wave(15, 0.6) + n(0.05), 51.5 + wave(16, 1.5) + n(0.2)),
    # Building B: HVAC can't keep up in the afternoon -> too warm (alarm around 15:00)
    "sim-rc-f1-stacks-01": lambda: (19.5 + max(0, wave(15, 3)) + n(0.1), 45 + wave(4, 2) + n(0.2)),
    # Building C: large daily RH swing (demo of swing alert)
    "sim-wc-f1-stacks-01": lambda: (18 + wave(15, 0.8) + n(0.05), 44 + wave(14, 6) + n(0.3)),
}
# Water leak probes (Tasmota switch input: ON = water). Cold room leaks for 20 min every 12 h.
LEAK = {
    "sim-cb-f1-cold-leak-01": lambda: window(12, 20, 6),
    "sim-cb-f1-vault-leak-01": lambda: False,
    "sim-rc-f1-stacks-leak-01": lambda: False,
}
# Air quality, in Tasmota's payload shapes for the Sensirion SEN55 (particles, VOC, NOx) and SCD40 (CO2).
AIR = {
    "sim-cb-f1-vault-aq-01": lambda: {"SEN5X": {"PM1": 2 + n(0.5), "PM2.5": 4 + abs(n(1)), "PM4": 5 + abs(n(1)), "PM10": 6 + abs(n(1.5)),
                                                "VOC_Index": 100 + n(8), "NOx_Index": 1 + abs(n(1))}},
    # Building C: dusty (Watch) and a daily cleaning-product VOC spike for an hour
    "sim-wc-f1-stacks-aq-01": lambda: {"SEN5X": {"PM1": 8 + n(1), "PM2.5": 15 + wave(11, 4) + abs(n(2)), "PM4": 18 + abs(n(2)),
                                                 "PM10": 24 + wave(11, 6) + abs(n(3)),
                                                 "VOC_Index": (320 if window(24, 60, 13) else 110) + n(10),
                                                 "NOx_Index": 3 + abs(n(2))}},
    # Map room: CO2 rises with researchers in the room during opening hours
    "sim-cb-f1-map-co2-01": lambda: {"SCD40": {"CarbonDioxide": 450 + (500 + wave(13, 300) if 9 <= hour() < 17 else 0) + n(15)}},
}

def topic(i, sid): return f"{sid}_5100{i:02X}"  # fake MAC suffix, like Tasmota's %06X

def enc(s): b = s.encode(); return struct.pack("!H", len(b)) + b

def packet(kind, body):
    out, ln = bytes([kind]), len(body)
    while True:
        d, ln = ln % 128, ln // 128
        out += bytes([d | (128 if ln else 0)])
        if not ln: return out + body

class Mqtt:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=10)
        self.s.sendall(packet(0x10, enc("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", PERIOD * 3) +
                              enc(f"archmon-sim-{os.getpid()}") + enc(USER) + enc(PASSWORD)))
        ack = self.s.recv(4)
        if len(ack) < 4 or ack[3] != 0:
            raise ConnectionError(f"MQTT connect refused: {ack!r}")
    def pub(self, t, payload, retain=False):
        self.s.sendall(packet(0x31 if retain else 0x30, enc(t) + (payload if isinstance(payload, str) else json.dumps(payload)).encode()))

def rnd(d): return {k: rnd(v) if isinstance(v, dict) else (round(v, 1) if isinstance(v, float) else v) for k, v in d.items()}

def tick(m, start):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    for i, sid in enumerate([*CLIMATE, *LEAK, *AIR]):
        t = topic(i, sid)
        if sid in CLIMATE:
            tc, rh = CLIMATE[sid]()
            body = {"SHT4X": {"Temperature": tc, "Humidity": max(1.0, min(100.0, rh))}, "TempUnit": "C"}
        elif sid in LEAK:
            body = {"Switch1": "ON" if LEAK[sid]() else "OFF"}
        else:
            body = AIR[sid]()
        m.pub(f"tele/{t}/SENSOR", rnd({"Time": ts, **body}))
        m.pub(f"tele/{t}/STATE", {"Time": ts, "UptimeSec": int(time.time() - start),
                                  "Wifi": {"RSSI": 60 + (i * 7) % 35, "Signal": -80 + (i * 7) % 35}})
        m.pub(f"tele/{t}/LWT", "Online", retain=True)

def main():
    random.seed()
    start = time.time()
    print(f"simulating {len(CLIMATE) + len(LEAK) + len(AIR)} sensors every {PERIOD}s", flush=True)
    while True:
        try:
            m = Mqtt()
            while True:
                tick(m, start)
                time.sleep(PERIOD)
        except (OSError, ConnectionError) as e:
            print("mqtt:", e, flush=True)
            time.sleep(10)

if __name__ == "__main__":
    main()

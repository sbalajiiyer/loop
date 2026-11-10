"""Fill a throwaway database with ~60 days of realistic synthetic data, so the
dashboard and scoring can be tried before the Pi and strap are set up.

    VITALS_DB=data/demo.sqlite python -m vitals.tools.seed_demo
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta

from .. import analytics, db

R = random.Random(7)
DAYS = 60


def truth_for(i: int) -> dict:
    """Ground truth for night i (shared by every 'device' so they roughly agree)."""
    weekly = math.sin(i / 7 * 2 * math.pi)
    stress = 1 if R.random() < 0.12 else 0
    hrv = math.exp(math.log(46) + 0.12 * weekly - 0.35 * stress + R.gauss(0, 0.12))
    rhr = 53 + 2.2 * stress - 0.8 * weekly + R.gauss(0, 1.3)
    sleep_h = max(4.8, min(9.2, 7.3 + R.gauss(0, 0.7) - 0.8 * stress))
    bed = 23.1 + R.gauss(0, 0.6)
    workout = R.random() < 0.55
    return {"hrv": hrv, "rhr": rhr, "sleep_h": sleep_h, "bed": bed, "workout": workout,
            "resp": round(14.6 + R.gauss(0, 0.5) + 0.8 * stress, 1)}


def gen_samples(day, t: dict, prev_t: dict) -> list[tuple]:
    tz = analytics.TZ
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    bed = midnight - timedelta(hours=24 - t["bed"])
    wake = bed + timedelta(hours=t["sleep_h"] / 0.93)
    rows = []

    breath_hz = t.get("resp", 14.5) / 60
    clock = {"t": 0.0}

    def beat_rr(hr: float, rmssd: float, seconds: int) -> list[float]:
        base = 60000 / hr
        n = max(1, round(seconds * hr / 60))
        out, x = [], base
        for _ in range(n):
            x = base + 0.5 * (x - base) + R.gauss(0, rmssd / 1.6)
            v = x + 0.8 * rmssd * math.sin(2 * math.pi * breath_hz * clock["t"])  # breathing rhythm
            clock["t"] += v / 1000
            out.append(round(max(320, min(1900, v)), 1))
        return out

    # Overnight at 5 s resolution with stage-like cycles.
    ts = int(bed.timestamp()) - 3600
    end = int(wake.timestamp()) + 1800
    while ts < end:
        in_sleep = bed.timestamp() <= ts < wake.timestamp()
        if in_sleep:
            phase = (ts - bed.timestamp()) / 5400 * 2 * math.pi  # ~90 min cycles
            hr = t["rhr"] + 3.5 + 3.2 * math.sin(phase) + R.gauss(0, 1.1)
            rm = t["hrv"] * (1.15 - 0.25 * math.sin(phase))
            if R.random() < 0.004:
                hr += 14
        else:
            hr = t["rhr"] + 18 + R.gauss(0, 3)
            rm = t["hrv"] * 0.5
        rows.append((ts, round(hr), beat_rr(hr, rm, 5), None))
        ts += 5

    # Daytime: only the hours at home are in range of the Pi (morning + evening),
    # plus a workout on some days.
    for h0, h1 in ((wake.hour + 1, 9), (17, 22)):
        ts = int((midnight + timedelta(hours=h0)).timestamp())
        stop = int((midnight + timedelta(hours=h1)).timestamp())
        while ts < stop:
            hr = t["rhr"] + 24 + 6 * math.sin(ts / 1500) + R.gauss(0, 4)
            rows.append((ts, round(hr), beat_rr(hr, t["hrv"] * 0.4, 5), None))
            ts += 5
    if t["workout"]:
        ts = int((midnight + timedelta(hours=18, minutes=10)).timestamp())
        for k in range(0, 50 * 60, 5):
            ramp = min(1, k / 300, (3000 - k) / 300)
            hr = t["rhr"] + 20 + 85 * ramp + R.gauss(0, 4)
            rows.append((ts + k, round(hr), beat_rr(hr, 8, 5), None))
    return rows


def main() -> None:
    db.conn()
    now = int(time.time())
    today = datetime.now(analytics.TZ).date()
    truths = [truth_for(i) for i in range(DAYS + 1)]
    # Journal tags on day i shape the night that follows (truths[i - 1]).
    effects = {"Alcohol": (.22, .78, 3.0, -.2), "Late meal": (.25, .93, 1.5, -.3), "Caffeine after 2pm": (.2, .96, .5, -.45),
               "Meditation": (.3, 1.07, -.5, .1), "Keto day": (.6, 1.02, -.3, 0), "Hard workout": (.0, .9, 1.5, .2)}
    entries = []
    for i in range(DAYS, 0, -1):
        for tag, (p, hrv_f, rhr_d, sleep_d) in effects.items():
            hit = truths[i]["workout"] if tag == "Hard workout" else R.random() < p
            if hit:
                entries.append(((today - timedelta(days=i)).isoformat(), tag))
                nxt = truths[i - 1]
                nxt["hrv"] *= hrv_f
                nxt["rhr"] += rhr_d
                nxt["sleep_h"] = max(4.5, nxt["sleep_h"] + sleep_d)
    with db.tx() as c:
        c.executemany("INSERT OR IGNORE INTO journal(day, tag) VALUES(?,?)", entries)
    print("generating samples ...")
    for i in range(DAYS, -1, -1):
        day = today - timedelta(days=i)
        t = truths[i]
        bed_ts = int((datetime(day.year, day.month, day.day, tzinfo=analytics.TZ)
                      - timedelta(hours=24 - t["bed"])).timestamp())
        wake_ts = bed_ts + int(t["sleep_h"] / 0.93 * 3600)
        rows = [s for s in gen_samples(day, t, truths[min(i + 1, DAYS)]) if s[0] <= now]
        if i <= DAYS - 3:  # strap worn from 3 days into the window
            db.insert_samples("whoop", rows)
        # Oura API style: ~1 heart-rate sample per 5 minutes, no beats, plus 5-minute HRV in sleep
        db.insert_samples("oura", [(ts, bpm - 1, None, None) for ts, bpm, _, _ in rows if ts % 300 < 5])
        db.insert_hrv("oura", [(ts, round(t["hrv"] * 0.92 * R.uniform(.75, 1.25), 1))
                               for ts in range(bed_ts, min(wake_ts, now), 300)])
        sleep_min = t["sleep_h"] * 60

        def stages(scale: float) -> dict:
            s = sleep_min * scale
            return {"sleep_min": round(s), "deep_min": round(s * R.uniform(.16, .22)),
                    "rem_min": round(s * R.uniform(.20, .26)), "in_bed_min": round(s / 0.92),
                    "awake_min": round(s / 0.92 - s)}

        rec_base = 50 + 32 * math.tanh((math.log(t["hrv"]) - math.log(46)) / 0.2) - 4 * (t["rhr"] - 53)
        # WHOOP official (baselines settle after ~3 weeks on a new wearer)
        settle = 1.0 if i < DAYS - 24 else 0.6
        w = stages(R.uniform(.96, 1.03))
        w["light_min"] = w["sleep_min"] - w["deep_min"] - w["rem_min"]
        db.upsert_daily("whoop", "official", day.isoformat(), {
            **w, "recovery": round(max(5, min(99, rec_base * settle + 50 * (1 - settle) + R.gauss(0, 6)))),
            "hrv": round(t["hrv"] * R.uniform(.92, 1.08), 1), "rhr": round(t["rhr"] + R.gauss(0, .8)),
            "strain": round(min(20.5, (13.5 if t["workout"] else 7.2) + R.gauss(0, 1.6)), 1),
            "sleep_perf": round(100 * sleep_min / 480), "efficiency": round(R.uniform(88, 95), 1),
            "resp_rate": round(t["resp"] + R.gauss(0, .3), 1), "spo2": round(R.uniform(95, 98), 1),
            "skin_temp": round(33.6 + R.gauss(0, .3), 2), "bedtime": bed_ts + R.randint(-300, 300),
            "waketime": wake_ts + R.randint(-300, 300), "calories": round(2350 + (550 if t["workout"] else 0)),
            "calibrating": int(i > DAYS - 21)}, now)
        # Oura official
        o = stages(R.uniform(.93, 1.05))
        o["light_min"] = o["sleep_min"] - o["deep_min"] - o["rem_min"]
        db.upsert_daily("oura", "official", day.isoformat(), {
            **o, "recovery": round(max(10, min(98, 0.7 * rec_base + 25 + R.gauss(0, 5)))),
            "hrv": round(t["hrv"] * R.uniform(.85, 1.0), 1), "rhr": round(t["rhr"] - 1 + R.gauss(0, .9)),
            "sleep_perf": round(100 * sleep_min / 480), "efficiency": round(R.uniform(86, 94)),
            "resp_rate": round(t["resp"] - 0.2 + R.gauss(0, .3), 1), "spo2": round(R.uniform(95, 98), 1),
            "skin_temp": round(R.gauss(0, .25), 2), "bedtime": bed_ts + R.randint(-600, 600),
            "waketime": wake_ts + R.randint(-600, 600), "steps": R.randint(5200, 12800),
            "calories": round(2300 + (500 if t["workout"] else 0)),
            "extra": {"sleep_score": R.randint(68, 90), "activity_score": R.randint(70, 95)}}, now)

    print("scoring ...")
    analytics.recompute("whoop", days=DAYS + 1, today=today)
    analytics.recompute("oura", days=DAYS + 1, today=today)
    print("tuning ...")
    for d in ("whoop", "oura"):
        analytics.recalibrate(d)
        analytics.recompute(d, days=DAYS + 1, today=today)
    with db.tx() as c:
        c.execute("INSERT INTO sync_log(device,source,at,ok,detail) VALUES('whoop','official',?,1,'demo')", (now,))
        c.execute("INSERT INTO sync_log(device,source,at,ok,detail) VALUES('oura','official',?,1,'demo')", (now,))
    print("done")


if __name__ == "__main__":
    main()

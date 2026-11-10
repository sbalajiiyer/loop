"""Our own scores, computed from raw beat-level data (no vendor algorithms).

Per local day D (the day you wake up on):
  sleep      longest low-heart-rate block in the night window (D-1 18:00 to D 14:00)
  rhr        lowest 5-minute average heart rate while asleep
  hrv        median RMSSD of 5-minute windows while asleep (cleaned R-R intervals)
  stages     rough estimate from heart-rate level and variability (labelled "est.")
  recovery   0-100 from tonight's HRV and RHR against your own 30-night baseline,
             plus sleep vs need
  strain     0-21, from heart-rate reserve minutes over the day (Banister TRIMP,
             compressed onto a 0-21 curve)

All of these are approximations. The point of the trial period is to measure how
closely they track the official numbers (see agreement()).
"""
from __future__ import annotations

import json
import math
import statistics as st
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, db

TZ = ZoneInfo(config.TIMEZONE)
MIN_COVERAGE_MIN = 180  # need 3 h of overnight data to score a night


# ---------------------------------------------------------------- helpers
def _minute_series(device: str, start: int, end: int) -> dict[int, dict]:
    rows = db.query("SELECT ts, bpm, rr_ms, motion FROM hr_samples WHERE device=? AND ts>=? AND ts<? ORDER BY ts",
                    (device, start, end))
    mins: dict[int, dict] = defaultdict(lambda: {"bpm": [], "rr": [], "motion": []})
    for r in rows:
        m = mins[r["ts"] // 60 * 60]
        if r["bpm"]:
            m["bpm"].append(r["bpm"])
        if r["rr_ms"]:
            m["rr"] += json.loads(r["rr_ms"])
        if r["motion"] is not None:
            m["motion"].append(r["motion"])
    out = {t: {"bpm": st.fmean(v["bpm"]) if v["bpm"] else None, "rr": v["rr"],
               "motion": st.fmean(v["motion"]) if v["motion"] else None} for t, v in sorted(mins.items())}
    # Sparse sources (Oura samples ~1 min in 5) are carried forward across short
    # gaps so they can be scored like dense ones. Gaps over 6 minutes stay gaps.
    filled, prev = {}, None
    for t in sorted(out):
        if prev is not None and out[prev]["bpm"] and 60 < t - prev <= FILL_GAP:
            for g in range(prev + 60, t, 60):
                filled[g] = {"bpm": out[prev]["bpm"], "rr": [], "motion": None}
        prev = t
    out.update(filled)
    return dict(sorted(out.items()))


FILL_GAP = 6 * 60


def _vendor_hrv(device: str, start: int, end: int) -> float | None:
    rows = db.query("SELECT rmssd FROM hrv_samples WHERE device=? AND ts>=? AND ts<?", (device, start, end))
    vals = [r["rmssd"] for r in rows if r["rmssd"]]
    return st.median(vals) if len(vals) >= 6 else None


def clean_rr(rr: list[float]) -> list[float]:
    """Drop physiologically impossible beats and ectopic jumps (>20% change)."""
    out: list[float] = []
    for x in rr:
        if not 300 <= x <= 2000:
            continue
        if out and abs(x - out[-1]) > 0.2 * out[-1]:
            continue
        out.append(x)
    return out


def rmssd(rr: list[float]) -> float | None:
    rr = clean_rr(rr)
    if len(rr) < 30:
        return None
    d = [(b - a) ** 2 for a, b in zip(rr, rr[1:])]
    return math.sqrt(st.fmean(d))


def resp_rate(rr: list[float]) -> float | None:
    """Breaths per minute from respiratory sinus arrhythmia: heartbeats speed up
    on each breath in and slow on each breath out, so the R-R series oscillates at
    the breathing rate. Resample the beat series at 2 Hz and find the dominant
    frequency between 7 and 27 breaths/min. Returns None when no clear peak."""
    rr = clean_rr(rr)
    if len(rr) < 120:  # ~2 minutes of beats
        return None
    t, times = 0.0, []
    for x in rr:
        t += x / 1000
        times.append(t)
    fs, n = 2.0, int(times[-1] * 2.0)
    series, j = [], 0
    for k in range(n):
        tk = times[0] + k / fs
        while j < len(times) - 2 and times[j + 1] < tk:
            j += 1
        t0, t1 = times[j], times[j + 1]
        w = 0 if t1 == t0 else min(1, max(0, (tk - t0) / (t1 - t0)))
        series.append(rr[j] + w * (rr[j + 1] - rr[j]))
    mean = st.fmean(series)
    series = [v - mean for v in series]
    best_f, best_p, powers = None, 0.0, []
    for i in range(24, 91):            # 0.120 .. 0.450 Hz in 0.005 Hz steps
        f = i * 0.005
        c = s = 0.0
        for k, v in enumerate(series):
            a = 2 * math.pi * f * k / fs
            c += v * math.cos(a)
            s += v * math.sin(a)
        p = c * c + s * s
        powers.append(p)
        if p > best_p:
            best_f, best_p = f, p
    if not best_f:
        return None
    # A real breathing rhythm concentrates power near one frequency; noise spreads it out.
    i = powers.index(best_p)
    near = sum(powers[max(0, i - 4):i + 5])
    if near < 0.45 * sum(powers):
        return None
    return round(best_f * 60, 1)


def _rolling_median(vals: list[float | None], half: int) -> list[float | None]:
    out = []
    for i in range(len(vals)):
        w = [v for v in vals[max(0, i - half):i + half + 1] if v is not None]
        out.append(st.median(w) if len(w) >= 3 else None)
    return out


def _local_midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())


# ---------------------------------------------------------------- nightly
def score_night(device: str, day: date) -> dict | None:
    start = _local_midnight(day) - 6 * 3600     # 18:00 the evening before
    end = _local_midnight(day) + 14 * 3600      # 14:00 on the day
    series = _minute_series(device, start, end)
    hr_minutes = [(t, v) for t, v in series.items() if v["bpm"]]
    if len(hr_minutes) < MIN_COVERAGE_MIN:
        return None

    times = [t for t, _ in hr_minutes]
    smooth = _rolling_median([v["bpm"] for _, v in hr_minutes], 7)
    vals = sorted(x for x in smooth if x)
    p10 = vals[int(0.10 * (len(vals) - 1))]
    p80 = vals[int(0.80 * (len(vals) - 1))]
    threshold = p10 + 0.4 * (p80 - p10)

    # Longest asleep block, bridging gaps (awakenings or missing data) under 30 min.
    blocks, cur = [], None
    for t, s, (_, v) in zip(times, smooth, hr_minutes):
        moving = v["motion"] is not None and v["motion"] > 0.15
        asleep = s is not None and s <= threshold and not moving
        if asleep:
            if cur and t - cur[1] <= 30 * 60:
                cur[1] = t
            else:
                cur = [t, t]
                blocks.append(cur)
    if not blocks:
        return None
    bed, wake = max(blocks, key=lambda b: b[1] - b[0])
    wake += 60
    if wake - bed < 2 * 3600:
        return None

    sleep_mins = [(t, v) for t, v in hr_minutes if bed <= t < wake]
    in_bed_min = (wake - bed) / 60

    # 5-minute epochs: HR, RMSSD, variability
    epochs = []
    for e0 in range(bed, wake, 300):
        chunk = [v for t, v in sleep_mins if e0 <= t < e0 + 300]
        bpms = [c["bpm"] for c in chunk if c["bpm"]]
        if len(bpms) < 3:
            continue
        rr = [x for c in chunk for x in c["rr"]]
        epochs.append({"t": e0, "hr": st.fmean(bpms), "sd": st.pstdev(bpms), "rmssd": rmssd(rr), "rr": rr})
    if not epochs:
        return None

    # Breathing rate from the calmest third of the night (every 3rd epoch keeps it cheap on a Pi Zero).
    calm = sorted(epochs, key=lambda e: e["hr"])[: max(1, len(epochs) // 3)]
    rates = [r for e in calm[::3] if (r := resp_rate(e["rr"])) is not None]
    resp = round(st.median(rates), 1) if len(rates) >= 3 else None

    rhr = min(e["hr"] for e in epochs)
    hrvs = [e["rmssd"] for e in epochs if e["rmssd"]]
    hrv = st.median(hrvs) if hrvs else None
    hrv_source = "beats"
    if hrv is None:  # no beat-to-beat data (Oura): use the vendor's 5-minute readings
        hrv, hrv_source = _vendor_hrv(device, bed, wake), "vendor_5min"

    # Rough staging: awake above threshold+5, deep = calm + lowest HR, REM = higher, more variable HR.
    hr_sorted = sorted(e["hr"] for e in epochs)
    q20 = hr_sorted[int(0.2 * (len(hr_sorted) - 1))]
    q55 = hr_sorted[int(0.55 * (len(hr_sorted) - 1))]
    sd_med = st.median(e["sd"] for e in epochs)
    stages = {"awake": 0.0, "light": 0.0, "deep": 0.0, "rem": 0.0}
    hypnogram = []
    for e in epochs:
        if e["hr"] > threshold + 5:
            s = "awake"
        elif e["hr"] <= q20 and e["sd"] <= sd_med:
            s = "deep"
        elif e["hr"] >= q55 and e["sd"] > sd_med:
            s = "rem"
        else:
            s = "light"
        stages[s] += 5
        hypnogram.append([e["t"], s])
    covered = len(epochs) * 5
    gap = max(0.0, in_bed_min - covered)
    stages["light"] += gap  # unobserved minutes inside the block count as light
    sleep_min = in_bed_min - stages["awake"]

    return {
        "rhr": round(rhr, 1), "hrv": round(hrv, 1) if hrv else None,
        "sleep_min": round(sleep_min, 1), "in_bed_min": round(in_bed_min, 1),
        "deep_min": stages["deep"], "rem_min": stages["rem"], "light_min": round(stages["light"], 1),
        "awake_min": stages["awake"], "efficiency": round(100 * sleep_min / in_bed_min, 1),
        "sleep_perf": round(100 * sleep_min / 60 / config.SLEEP_NEED_HOURS),
        "bedtime": bed, "waketime": wake, "resp_rate": resp,
        "extra": {"hypnogram": hypnogram, "coverage_min": len(sleep_mins), "stages_estimated": True,
                  "hrv_source": hrv_source},
    }


DEFAULT_RECOVERY = (0.0, 0.65, -0.35, 2.0)   # intercept, z_hrv, z_rhr, sleep term (logit space)
DEFAULT_STRAIN_K = 122.0


def _recovery_from(features: dict, coef) -> float:
    b0, b1, b2, b3 = coef
    raw = b0 + b1 * features["z_hrv"] + b2 * features["z_rhr"] + b3 * features["sleep_term"]
    return 100 / (1 + math.exp(-(1.3 * raw + 0.45)))


def recovery_score(device: str, day: date, night: dict) -> tuple[float | None, int]:
    prior = db.query(
        "SELECT hrv, rhr FROM daily WHERE device=? AND source='ours' AND day<? AND day>=? AND hrv IS NOT NULL",
        (device, day.isoformat(), (day - timedelta(days=30)).isoformat()))
    if night.get("hrv") is None:
        return None, 1
    calibrating = int(len(prior) < 7)
    if len(prior) < 4:
        ln_mu, ln_sd, rhr_mu, rhr_sd = math.log(night["hrv"]), 0.25, night["rhr"], 4.0
    else:
        lns = [math.log(p["hrv"]) for p in prior]
        ln_mu, ln_sd = st.fmean(lns), max(st.pstdev(lns), 0.08)
        rhrs = [p["rhr"] for p in prior]
        rhr_mu, rhr_sd = st.fmean(rhrs), max(st.pstdev(rhrs), 1.5)
    z_hrv = (math.log(night["hrv"]) - ln_mu) / ln_sd
    z_rhr = (night["rhr"] - rhr_mu) / rhr_sd
    sleep_ratio = min(night["sleep_min"] / 60 / config.SLEEP_NEED_HOURS, 1.1)
    features = {"z_hrv": round(z_hrv, 4), "z_rhr": round(z_rhr, 4), "sleep_term": round(sleep_ratio - 0.9, 4)}
    night.setdefault("extra", {})["features"] = features
    score = _recovery_from(features, DEFAULT_RECOVERY)
    cal = (db.kv_get(f"calib:{device}") or {}).get("recovery")
    if cal:  # blend toward the tuned model as evidence grows
        w = min(1.0, cal["n"] / 30)
        score = w * _recovery_from(features, cal["coef"]) + (1 - w) * score
    return round(max(1, min(99, score))), calibrating


# ---------------------------------------------------------------- daily strain
def strain_score(device: str, day: date, rhr: float | None) -> dict | None:
    start, end = _local_midnight(day), _local_midnight(day + timedelta(days=1))
    series = _minute_series(device, start, end)
    bpms = [v["bpm"] for v in series.values() if v["bpm"]]
    if len(bpms) < 60:
        return None
    rest = rhr or st.quantiles(bpms, n=20)[0]
    mx = config.max_hr()
    trimp, zone_min = 0.0, [0] * 6
    for b in bpms:
        hrr = max(0.0, min(1.0, (b - rest) / max(mx - rest, 1)))
        trimp += hrr * 0.64 * math.exp(1.92 * hrr) if hrr >= 0.3 else hrr * 0.15
        pct = b / mx
        zone = 0 if pct < 0.5 else min(5, int((pct - 0.5) / 0.1) + 1)
        zone_min[zone] += 1
    k = (db.kv_get(f"calib:{device}") or {}).get("strain_k") or DEFAULT_STRAIN_K
    strain = 21 * (1 - math.exp(-trimp / k))
    return {"strain": round(strain, 1),
            "extra": {"zone_minutes": zone_min, "strain_coverage_min": len(bpms), "trimp": round(trimp, 2)}}


# ---------------------------------------------------------------- auto-tuning
def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination for a small dense system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            raise ValueError("singular")
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def recalibrate(device: str, days: int = 90) -> dict:
    """Fit our Recovery model and Strain scale to the official numbers.

    Recovery: ridge regression in logit space of the official score on our nightly
    features (HRV and RHR vs baseline, sleep vs need), shrunk toward the default
    model. Nights the official app marks as calibrating are skipped, so WHOOP's
    first weeks on its previous wearer's baseline don't teach us anything wrong.
    Strain: the TRIMP scale that best matches official strain on well-covered days.
    The fitted numbers are kept after the membership ends.
    """
    since = (datetime.now(TZ).date() - timedelta(days=days)).isoformat()
    rows = db.query(
        "SELECT o.extra AS extra, f.recovery AS target, f.strain AS ostrain, o.day AS day FROM daily o "
        "JOIN daily f ON f.day=o.day AND f.device=o.device AND f.source='official' "
        "WHERE o.device=? AND o.source='ours' AND o.day>=? AND COALESCE(f.calibrating,0)=0", (device, since))
    X, y, strain_pairs = [], [], []
    for r in rows:
        extra = json.loads(r["extra"]) if r["extra"] else {}
        f = extra.get("features")
        if f and r["target"] is not None:
            p = min(max(r["target"], 2), 98) / 100
            y.append((math.log(p / (1 - p)) - 0.45) / 1.3)
            X.append([1.0, f["z_hrv"], f["z_rhr"], f["sleep_term"]])
        if extra.get("trimp") is not None and r["ostrain"] is not None and extra.get("strain_coverage_min", 0) >= 600:
            strain_pairs.append((extra["trimp"], r["ostrain"]))

    out = db.kv_get(f"calib:{device}") or {}
    if len(X) >= 14:
        lam = 3.0  # pull toward the default coefficients when data is thin or noisy
        prior = list(DEFAULT_RECOVERY)
        a = [[sum(x[i] * x[j] for x in X) + (lam if i == j else 0) for j in range(4)] for i in range(4)]
        b = [sum(x[i] * t for x, t in zip(X, y)) + lam * prior[i] for i in range(4)]
        try:
            coef = _solve(a, b)
            pred = [100 / (1 + math.exp(-(1.3 * sum(c * v for c, v in zip(coef, x)) + 0.45))) for x in X]
            actual = [100 / (1 + math.exp(-(1.3 * t + 0.45))) for t in y]
            out["recovery"] = {"coef": [round(c, 4) for c in coef], "n": len(X),
                               "mae": round(st.fmean(abs(p - q) for p, q in zip(pred, actual)), 1)}
        except ValueError:
            pass
    if len(strain_pairs) >= 10:
        def err(k: float) -> float:
            return sum((21 * (1 - math.exp(-t / k)) - s) ** 2 for t, s in strain_pairs)
        best = min((k for k in range(40, 401, 2)), key=err)
        out["strain_k"] = float(best)
        out["strain_n"] = len(strain_pairs)
    out["updated"] = int(time.time())
    db.kv_set(f"calib:{device}", out)
    return out


# ---------------------------------------------------------------- driver
def recompute(device: str, days: int = 3, today: date | None = None) -> int:
    today = today or datetime.now(TZ).date()
    now, n = int(time.time()), 0
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        night = score_night(device, day)
        rhr = None
        if night:
            rec, cal = recovery_score(device, day, night)
            night.update({"recovery": rec, "calibrating": cal})
            rhr = night["rhr"]
            db.upsert_daily(device, "ours", day.isoformat(), night, now)
            n += 1
        strain = strain_score(device, day, rhr)
        if strain:
            existing = db.query("SELECT extra FROM daily WHERE device=? AND source='ours' AND day=?",
                                (device, day.isoformat()))
            extra = json.loads(existing[0]["extra"]) if existing and existing[0]["extra"] else {}
            extra.update(strain["extra"])
            db.upsert_daily(device, "ours", day.isoformat(), {"strain": strain["strain"], "extra": extra}, now)
    return n


# ---------------------------------------------------------------- agreement
TOLERANCE = {"hrv": ("pct", 15), "rhr": ("abs", 3), "resp_rate": ("abs", 1), "sleep_min": ("abs", 30), "recovery": ("abs", 15),
             "strain": ("abs", 2.5), "bedtime": ("abs", 1800), "waketime": ("abs", 1800)}


def agreement(a: tuple[str, str], b: tuple[str, str], metric: str, days: int = 60) -> dict:
    since = (datetime.now(TZ).date() - timedelta(days=days)).isoformat()
    rows = db.query(
        f"SELECT x.day, x.{metric} AS a, y.{metric} AS b FROM daily x JOIN daily y ON x.day=y.day "
        f"WHERE x.device=? AND x.source=? AND y.device=? AND y.source=? AND x.day>=? "
        f"AND x.{metric} IS NOT NULL AND y.{metric} IS NOT NULL ORDER BY x.day",
        (a[0], a[1], b[0], b[1], since))
    n = len(rows)
    if n == 0:
        return {"metric": metric, "n": 0}
    diffs = [r["a"] - r["b"] for r in rows]
    kind, tol = TOLERANCE.get(metric, ("abs", 0))
    within = sum(1 for r in rows if (abs(r["a"] - r["b"]) <= tol if kind == "abs"
                                       else abs(r["a"] - r["b"]) <= tol / 100 * abs(r["b"])))
    r_val = None
    if n >= 3:
        xa, xb = [r["a"] for r in rows], [r["b"] for r in rows]
        sa, sb = st.pstdev(xa), st.pstdev(xb)
        if sa and sb:
            ma, mb = st.fmean(xa), st.fmean(xb)
            r_val = round(sum((p - ma) * (q - mb) for p, q in zip(xa, xb)) / (n * sa * sb), 2)
    return {"metric": metric, "n": n, "mean_diff": round(st.fmean(diffs), 2),
            "mae": round(st.fmean(abs(d) for d in diffs), 2), "r": r_val,
            "within_pct": round(100 * within / n), "tolerance": f"{'±' + str(tol)}{'%' if kind == 'pct' else ''}",
            "pairs": [[r["day"], r["a"], r["b"]] for r in rows]}


def readiness(device: str) -> dict:
    """Is 'ours' good enough to cancel this device's membership?"""
    checks = {m: agreement((device, "ours"), (device, "official"), m, days=45) for m in ("hrv", "rhr", "sleep_min")}
    if device == "whoop":  # WHOOP's own baseline is unreliable early; also cross-check against Oura
        checks["hrv_vs_oura"] = agreement(("whoop", "ours"), ("oura", "official"), "hrv", days=45)
    nights = max((c["n"] for c in checks.values()), default=0)
    passing = [k for k, c in checks.items() if c["n"] >= 30 and c.get("within_pct", 0) >= 80]
    # Ours only replaces the membership when it comes from our own Bluetooth collection.
    own = db.query("SELECT COUNT(*) AS n FROM raw_frames WHERE device=?", (device,))[0]["n"] > 0 or device == "whoop"
    blocked = None if own else "Our Oura scores still use Oura's API data. Cancelling needs direct Bluetooth support for Ring 4."
    return {"device": device, "nights": nights, "target_nights": 30, "checks": checks, "blocked": blocked,
            "ready": not blocked and nights >= 30 and len(passing) >= 3}

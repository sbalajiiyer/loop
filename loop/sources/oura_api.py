"""Official Oura API v2 (baseline while the Oura membership is active)."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from .. import config, db
from .oauth import Provider

BASE = "https://api.ouraring.com/v2/usercollection"

provider = Provider(
    name="oura",
    authorize_url="https://cloud.ouraring.com/oauth/authorize",
    token_url="https://api.ouraring.com/oauth/token",
    scopes="email personal daily heartrate workout session spo2Daily",
    client_id=config.OURA_CLIENT_ID,
    client_secret=config.OURA_CLIENT_SECRET,
)


def _collection(name: str, start: str, end: str, by_datetime: bool = False) -> list[dict]:
    out, token = [], None
    while True:
        params = ({"start_datetime": start + "T00:00:00Z", "end_datetime": end + "T00:00:00Z"} if by_datetime
                  else {"start_date": start, "end_date": end})
        if token:
            params["next_token"] = token
        page = provider.get(f"{BASE}/{name}", params)
        out += page.get("data", [])
        token = page.get("next_token")
        if not token:
            return out


def _min(seconds) -> float | None:
    return round(seconds / 60, 1) if seconds is not None else None


def _ts(iso: str | None) -> int | None:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()) if iso else None


def _expand(sample: dict | None) -> list[tuple[int, float]]:
    """Oura 'PublicSample' {interval, items, timestamp} -> [(unix_ts, value)]."""
    if not sample or not sample.get("items"):
        return []
    t0, step = _ts(sample["timestamp"]), float(sample.get("interval") or 300)
    return [(int(t0 + i * step), v) for i, v in enumerate(sample["items"]) if v is not None]


def import_raw(sleeps: list[dict], start: str, end: str) -> int:
    """Raw-ish inputs for our own Oura scores: heart-rate samples (day and night)
    and Oura's 5-minute HRV readings during sleep. Oura publishes no beat-to-beat data."""
    hr = {}
    for s in _collection("heartrate", start, end, by_datetime=True):
        if s.get("bpm"):
            hr[_ts(s["timestamp"])] = s["bpm"]
    hrv = []
    for s in sleeps:
        for ts, v in _expand(s.get("heart_rate")):
            hr.setdefault(ts, round(v))
        hrv += _expand(s.get("hrv"))
    db.insert_samples("oura", [(ts, bpm, None, None) for ts, bpm in sorted(hr.items())])
    db.insert_hrv("oura", hrv)
    return len(hr)


def sync() -> str:
    now = int(time.time())
    since = db.kv_get("cursor:oura") or config.OURA_IMPORT_FROM
    start = max(config.OURA_IMPORT_FROM, (date.fromisoformat(since) - timedelta(days=3)).isoformat())
    end = (date.today() + timedelta(days=1)).isoformat()

    for r in _collection("daily_readiness", start, end):
        db.upsert_daily("oura", "official", r["day"], {
            "recovery": r.get("score"), "skin_temp": r.get("temperature_deviation"),
            "extra": {"readiness_contributors": r.get("contributors")}}, now)

    sleeps = _collection("sleep", start, end)
    for s in sleeps:
        if s.get("type") != "long_sleep":
            continue
        db.upsert_daily("oura", "official", s["day"], {
            "sleep_min": _min(s.get("total_sleep_duration")), "in_bed_min": _min(s.get("time_in_bed")),
            "deep_min": _min(s.get("deep_sleep_duration")), "rem_min": _min(s.get("rem_sleep_duration")),
            "light_min": _min(s.get("light_sleep_duration")), "awake_min": _min(s.get("awake_time")),
            "efficiency": s.get("efficiency"), "hrv": s.get("average_hrv"),
            "rhr": s.get("lowest_heart_rate"), "resp_rate": s.get("average_breath"),
            "bedtime": _ts(s.get("bedtime_start")), "waketime": _ts(s.get("bedtime_end")),
            "sleep_perf": round(100 * (s.get("total_sleep_duration") or 0) / 3600 / config.SLEEP_NEED_HOURS),
        }, now)

    for d in _collection("daily_sleep", start, end):
        db.upsert_daily("oura", "official", d["day"], {"extra": {"sleep_score": d.get("score")}}, now)

    for a in _collection("daily_activity", start, end):
        db.upsert_daily("oura", "official", a["day"], {
            "calories": a.get("total_calories"), "steps": a.get("steps"),
            "extra": {"activity_score": a.get("score")}}, now)

    try:
        for s in _collection("daily_spo2", start, end):
            avg = (s.get("spo2_percentage") or {}).get("average")
            if avg:
                db.upsert_daily("oura", "official", s["day"], {"spo2": avg}, now)
    except Exception:  # noqa: BLE001  (SpO2 scope may not be granted)
        pass

    n_hr = import_raw(sleeps, start, end)

    db.kv_set("cursor:oura", date.today().isoformat())
    return f"{len(sleeps)} sleep periods, {n_hr} heart-rate samples"

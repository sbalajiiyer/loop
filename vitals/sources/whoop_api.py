"""Official WHOOP API v2 (temporary baseline while the membership lasts).

Imports only data on or after WHOOP_IMPORT_FROM, so the account's earlier
history (from its previous wearer) never enters the dashboard."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from .. import config, db
from .oauth import Provider

BASE = "https://api.prod.whoop.com/developer"

provider = Provider(
    name="whoop",
    authorize_url="https://api.prod.whoop.com/oauth/oauth2/auth",
    token_url="https://api.prod.whoop.com/oauth/oauth2/token",
    scopes="offline read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement",
    client_id=config.WHOOP_CLIENT_ID,
    client_secret=config.WHOOP_CLIENT_SECRET,
)


def _collection(path: str, start: str) -> list[dict]:
    out, token = [], None
    while True:
        params = {"limit": 25, "start": start}
        if token:
            params["nextToken"] = token
        page = provider.get(f"{BASE}{path}", params)
        out += page.get("records", [])
        token = page.get("next_token")
        if not token:
            return out


def _local(iso: str, offset: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    sign = -1 if offset.startswith("-") else 1
    hh, mm = offset.lstrip("+-").split(":")
    return dt.astimezone(timezone(sign * timedelta(hours=int(hh), minutes=int(mm))))


def _ms_to_min(v) -> float | None:
    return round(v / 60000, 1) if v is not None else None


def sync() -> str:
    now = int(time.time())
    floor = config.WHOOP_IMPORT_FROM + "T00:00:00.000Z"
    since = db.kv_get("cursor:whoop")
    # Re-read the last 3 days every time: scores get revised after more data syncs.
    start = max(floor, since or floor)
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")) - timedelta(days=3)
    start = max(floor, start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    sleeps = _collection("/v2/activity/sleep", start)
    sleep_day = {}
    for s in sleeps:
        if s.get("nap") or s.get("score_state") != "SCORED":
            continue
        end = _local(s["end"], s["timezone_offset"])
        day = end.date().isoformat()
        sleep_day[s["id"]] = day
        sc, st = s["score"], s["score"]["stage_summary"]
        light, deep, rem = (_ms_to_min(st.get(k)) or 0 for k in
                            ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
                             "total_rem_sleep_time_milli"))
        db.upsert_daily("whoop", "official", day, {
            "sleep_min": round(light + deep + rem, 1), "light_min": light, "deep_min": deep, "rem_min": rem,
            "in_bed_min": _ms_to_min(st.get("total_in_bed_time_milli")),
            "awake_min": _ms_to_min(st.get("total_awake_time_milli")),
            "sleep_perf": sc.get("sleep_performance_percentage"),
            "efficiency": sc.get("sleep_efficiency_percentage"),
            "resp_rate": sc.get("respiratory_rate"),
            "bedtime": int(_local(s["start"], s["timezone_offset"]).timestamp()),
            "waketime": int(end.timestamp()),
            "extra": {"sleep_consistency": sc.get("sleep_consistency_percentage"),
                      "disturbances": st.get("disturbance_count")},
        }, now)

    for r in _collection("/v2/recovery", start):
        day = sleep_day.get(r.get("sleep_id"))
        if not day or r.get("score_state") != "SCORED":
            continue
        sc = r["score"]
        db.upsert_daily("whoop", "official", day, {
            "recovery": sc.get("recovery_score"), "rhr": sc.get("resting_heart_rate"),
            "hrv": sc.get("hrv_rmssd_milli"), "spo2": sc.get("spo2_percentage"),
            "skin_temp": sc.get("skin_temp_celsius"), "calibrating": int(bool(sc.get("user_calibrating"))),
        }, now)

    for c in _collection("/v2/cycle", start):
        if c.get("score_state") != "SCORED":
            continue
        # A cycle starts when you fall asleep; it belongs to the day you then live.
        day = (_local(c["start"], c["timezone_offset"]) + timedelta(hours=6)).date().isoformat()
        sc = c["score"]
        db.upsert_daily("whoop", "official", day, {
            "strain": sc.get("strain"),
            "calories": round(sc["kilojoule"] / 4.184) if sc.get("kilojoule") else None,
            "steps": c.get("step_count"),
        }, now)

    workouts = _collection("/v2/activity/workout", start)
    with db.tx() as conn:
        for w in workouts:
            sc = w.get("score") or {}
            conn.execute(
                "INSERT OR REPLACE INTO workouts VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("whoop", "official", w["id"],
                 int(datetime.fromisoformat(w["start"].replace("Z", "+00:00")).timestamp()),
                 int(datetime.fromisoformat(w["end"].replace("Z", "+00:00")).timestamp()),
                 w.get("sport_name"), sc.get("strain"), sc.get("average_heart_rate"),
                 sc.get("max_heart_rate"), round(sc["kilojoule"] / 4.184) if sc.get("kilojoule") else None))

    db.kv_set("cursor:whoop", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    return f"{len(sleeps)} sleeps, {len(workouts)} workouts"

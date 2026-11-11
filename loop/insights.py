"""Journal patterns, alerts and the morning brief."""
from __future__ import annotations

import json
import statistics as st
import time
import urllib.request
from datetime import date, datetime, timedelta

from . import analytics, config, db

PREFERRED = [("whoop", "ours"), ("whoop", "official"), ("oura", "official"), ("oura", "ours")]


def _daily(device: str, source: str, since: str) -> dict[str, dict]:
    return {r["day"]: r for r in db.query(
        "SELECT * FROM daily WHERE device=? AND source=? AND day>=?", (device, source, since))}


def preferred_series() -> tuple[str, str] | None:
    for d, s in PREFERRED:
        if db.query("SELECT 1 FROM daily WHERE device=? AND source=? AND recovery IS NOT NULL LIMIT 1", (d, s)):
            return d, s
    return None


# ------------------------------------------------------------------ journal
def journal(days: int = 120) -> list[dict]:
    since = (datetime.now(analytics.TZ).date() - timedelta(days=days)).isoformat()
    return db.query("SELECT day, tag FROM journal WHERE day>=? ORDER BY day", (since,))


def set_tag(day: str, tag: str, on: bool) -> None:
    date.fromisoformat(day)
    tag = tag.strip()[:40]
    if not tag:
        raise ValueError("empty tag")
    with db.tx() as c:
        if on:
            c.execute("INSERT OR IGNORE INTO journal(day, tag) VALUES(?,?)", (day, tag))
        else:
            c.execute("DELETE FROM journal WHERE day=? AND tag=?", (day, tag))


def patterns(device: str, source: str, days: int = 120) -> list[dict]:
    """Effect of each tag on the night that follows it (a tag on day D is compared
    on the night ending D+1), against nights after untagged days."""
    since = (datetime.now(analytics.TZ).date() - timedelta(days=days)).isoformat()
    rows = _daily(device, source, since)
    tags: dict[str, set[str]] = {}
    for j in journal(days):
        tags.setdefault(j["tag"], set()).add(j["day"])
    out = []
    for tag, tdays in tags.items():
        res = {"tag": tag, "metrics": {}}
        for m in ("recovery", "hrv", "rhr", "sleep_min"):
            with_, without = [], []
            for day in rows:
                prev = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
                v = rows[day].get(m)
                if v is None:
                    continue
                if prev in tdays:
                    with_.append(v)
                else:
                    without.append(v)
            if len(with_) >= 4 and len(without) >= 4:
                a, b = st.fmean(with_), st.fmean(without)
                sd = st.pstdev(with_ + without) or 1
                effect = (a - b) / sd
                res["metrics"][m] = {"with": round(a, 1), "without": round(b, 1), "diff": round(a - b, 1),
                                     "pct": round(100 * (a - b) / b) if b else None, "n": len(with_),
                                     "strength": "clear" if abs(effect) >= 0.5 and len(with_) >= 6
                                     else "possible" if abs(effect) >= 0.3 else "none"}
        res["n"] = len(tdays)
        if res["metrics"]:
            out.append(res)
    out.sort(key=lambda r: -abs((r["metrics"].get("recovery") or r["metrics"].get("hrv") or {}).get("diff", 0)))
    return out


# ------------------------------------------------------------------ alerts
def _baseline(rows: dict[str, dict], field: str, before: str, n: int = 30) -> float | None:
    vals = [r[field] for d, r in rows.items() if d < before and r.get(field) is not None][-n:]
    return st.fmean(vals) if len(vals) >= 7 else None


def alerts() -> list[dict]:
    out = []
    today = datetime.now(analytics.TZ).date()
    since = (today - timedelta(days=45)).isoformat()

    # Body signals: two nights of raised resting HR and lowered HRV often precede illness.
    pref = preferred_series()
    if pref:
        rows = dict(sorted(_daily(*pref, since).items()))
        days = list(rows)[-2:]
        if len(days) == 2:
            rb, hb = _baseline(rows, "rhr", days[0]), _baseline(rows, "hrv", days[0])
            if rb and hb and all((rows[d].get("rhr") or 0) >= rb + 3 and (rows[d].get("hrv") or 1e9) <= 0.85 * hb
                                 for d in days):
                out.append({"id": f"strain-signal-{days[-1]}", "level": "warn", "title": "Resting heart rate up, HRV down",
                            "body": "Two nights in a row. This often comes before getting sick or after heavy load. "
                                    "Consider an easier day and an early night."})
        recs = [rows[d].get("recovery") for d in list(rows)[-3:]]
        if len(recs) == 3 and all(r is not None and r < 34 for r in recs):
            out.append({"id": f"low-streak-{list(rows)[-1]}", "level": "warn", "title": "Three low-recovery days",
                        "body": "Recovery has been under 34% for three days. Sleep and lighter training help most."})

    # Strap memory: it holds about 14 days; warn well before it fills.
    last = db.query("SELECT MAX(ts) AS t FROM hr_samples WHERE device='whoop'")[0]["t"]
    has_ble = db.query("SELECT 1 FROM raw_frames WHERE device='whoop' LIMIT 1")
    if last and has_ble:
        age = (time.time() - last) / 86400
        if age > 10:
            out.append({"id": "strap-critical", "level": "critical", "title": f"Strap hasn't synced in {int(age)} days",
                        "body": "Its memory holds about 14 days. Bring it near the Pi or run the laptop collector soon."})
        elif age > 3:
            out.append({"id": "strap-warn", "level": "info", "title": f"Strap hasn't synced in {int(age)} days",
                        "body": "No data is lost yet. It will catch up next time it's near the Pi or your laptop."})

    # Official API sync failing
    for d in ("whoop", "oura"):
        if not db.kv_get(f"token:{d}"):
            continue
        recent = db.query("SELECT ok FROM sync_log WHERE device=? ORDER BY at DESC LIMIT 3", (d,))
        if len(recent) == 3 and not any(r["ok"] for r in recent):
            out.append({"id": f"sync-{d}", "level": "warn", "title": f"{'WHOOP' if d == 'whoop' else 'Oura'} sync is failing",
                        "body": "Open Devices to reconnect. The membership may have ended or access was removed."})
    return out


# ------------------------------------------------------------------ morning brief
def summary(day: str | None = None) -> dict:
    pref = preferred_series()
    if not pref:
        return {"day": day, "headline": "No scores yet", "items": [], "series": None}
    today = date.fromisoformat(day) if day else datetime.now(analytics.TZ).date()
    since = (today - timedelta(days=45)).isoformat()
    rows = _daily(*pref, since)
    r = rows.get(today.isoformat())
    if not r:
        return {"day": today.isoformat(), "headline": "Last night isn't scored yet", "items": [], "series": list(pref)}
    items = []
    rec = r.get("recovery")
    tier = "high" if rec is not None and rec >= 67 else "moderate" if rec is not None and rec >= 34 else "low"
    advice = {"high": "a good day to push", "moderate": "train as planned, keep intensity in check",
              "low": "keep today light and prioritise sleep"}[tier]
    headline = f"Recovery {round(rec)}%: {advice}." if rec is not None else "Recovery not available."
    hb, rb = _baseline(rows, "hrv", today.isoformat()), _baseline(rows, "rhr", today.isoformat())
    if r.get("hrv") and hb:
        pct = round(100 * (r["hrv"] - hb) / hb)
        items.append(f"HRV {round(r['hrv'])} ms, {abs(pct)}% {'above' if pct >= 0 else 'below'} your 30-day average.")
    if r.get("rhr") and rb:
        d = round(r["rhr"] - rb)
        items.append(f"Resting HR {round(r['rhr'])} bpm" + (f", {abs(d)} {'above' if d > 0 else 'below'} usual." if d else ", right at your usual."))
    if r.get("sleep_min"):
        need = config.SLEEP_NEED_HOURS * 60
        gap = round(r["sleep_min"] - need)
        h, m = divmod(int(r["sleep_min"]), 60)
        items.append(f"Slept {h}h {m:02d}m" + (f", {abs(gap)} min {'over' if gap > 0 else 'short of'} your need." if abs(gap) >= 10 else ", right on your need."))
    y = rows.get((today - timedelta(days=1)).isoformat())
    if y and y.get("strain") is not None:
        items.append(f"Yesterday's strain {y['strain']:.1f}.")
    ytags = [j["tag"] for j in db.query("SELECT tag FROM journal WHERE day=?", ((today - timedelta(days=1)).isoformat(),))]
    if ytags:
        known = {p["tag"]: p for p in patterns(*pref)}
        for t in ytags:
            eff = (known.get(t, {}).get("metrics") or {}).get("hrv")
            if eff and eff["strength"] != "none":
                items.append(f"You logged {t.lower()} yesterday; it usually moves your HRV by {eff['pct']:+d}%.")
    n_alerts = len(alerts())
    if n_alerts:
        items.append(f"{n_alerts} alert{'s' if n_alerts > 1 else ''} to look at.")
    return {"day": today.isoformat(), "headline": headline, "items": items, "series": list(pref), "tier": tier}


# ------------------------------------------------------------------ push (optional)
def push(title: str, body: str) -> None:
    """Send a notification through ntfy (set NTFY_URL to https://ntfy.sh/<long-random-topic>
    or your own ntfy server). Messages carry only the summary numbers."""
    url = config.get("NTFY_URL")
    if not url:
        return
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={"Title": title, "Tags": "heart"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:  # noqa: BLE001
        pass


def push_new() -> None:
    """Called by the server loop: new alerts once each, and the morning brief once a day."""
    if not config.get("NTFY_URL"):
        return
    sent = set(db.kv_get("pushed", []))
    for a in alerts():
        if a["id"] not in sent:
            push(a["title"], a["body"])
            sent.add(a["id"])
    now = datetime.now(analytics.TZ)
    key = f"brief-{now.date()}"
    if key not in sent and now.hour >= int(config.get("BRIEF_HOUR", "8")):
        s = summary()
        if s["items"]:
            push("Morning brief", s["headline"] + "\n" + "\n".join(s["items"]))
            sent.add(key)
    db.kv_set("pushed", sorted(sent)[-200:])

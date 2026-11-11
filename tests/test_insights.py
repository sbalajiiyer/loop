"""Runs in a subprocess so it gets its own empty database."""
import os
import subprocess
import sys
import tempfile
import textwrap

CODE = textwrap.dedent("""
    import time
    from datetime import datetime, timedelta
    from loop import analytics, db, insights
    today = datetime.now(analytics.TZ).date()
    now = int(time.time())
    for i in range(40, -1, -1):
        d = today - timedelta(days=i)
        drank = i % 4 == 1                       # tag on day d ...
        hrv = 50.0
        if (i + 1) % 4 == 1:                     # ... lowers the next night's HRV
            hrv = 38.0
        if i < 2:                                # last two nights: illness-like signal
            hrv, rhr = 35.0, 60.0
        else:
            rhr = 54.0
        db.upsert_daily("whoop", "ours", d.isoformat(), {"recovery": 60, "hrv": hrv, "rhr": rhr, "sleep_min": 450}, now)
        if drank:
            insights.set_tag(d.isoformat(), "Alcohol", True)
    pats = {p["tag"]: p for p in insights.patterns("whoop", "ours")}
    m = pats["Alcohol"]["metrics"]["hrv"]
    assert m["diff"] < -5 and m["strength"] == "clear", m
    ids = [a["id"] for a in insights.alerts()]
    assert any(i.startswith("strain-signal") for i in ids), ids
    s = insights.summary()
    assert s["headline"].startswith("Recovery 60%") and any("HRV" in x for x in s["items"]), s
    print("ok")
""")


def test_patterns_alerts_summary():
    env = dict(os.environ, LOOP_DB=tempfile.mktemp(suffix=".sqlite"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", CODE], env=env, cwd=root, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr

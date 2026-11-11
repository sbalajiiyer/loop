"""Runs in a subprocess so it gets its own empty database."""
import os
import subprocess
import sys
import tempfile
import textwrap

CODE = textwrap.dedent("""
    import math
    from datetime import datetime, timedelta
    from loop import analytics, db
    day = datetime.now(analytics.TZ).date() - timedelta(days=2)
    mid = int(datetime(day.year, day.month, day.day, tzinfo=analytics.TZ).timestamp())
    rows = []
    for m in range(7 * 60, 21 * 60):                      # strap heard 07:00-21:00
        bpm = 72
        if 17 * 60 <= m < 17 * 60 + 40:                   # 40-minute run at 17:00 ...
            bpm = 150 if not (17 * 60 + 18 <= m < 17 * 60 + 20) else 95   # ... with a 2-minute breather
        if 9 * 60 <= m < 9 * 60 + 4:                      # 4 minutes of stairs: too short
            bpm = 140
        rows.append((mid + m * 60, bpm, None, None))
    db.insert_samples("whoop", rows)

    analytics.recompute_day("whoop", day)
    det = db.query("SELECT * FROM workouts WHERE source='detected'")
    assert len(det) == 1, det
    w = det[0]
    assert w["start"] == mid + 17 * 3600 and (w["end"] - w["start"]) // 60 == 40, w
    assert 140 < w["avg_hr"] < 150 and w["max_hr"] == 150 and w["strain"] > 3, w
    base = db.query("SELECT strain, extra FROM daily WHERE device='whoop' AND source='ours'")[0]

    # A logged session outside the heard hours adds estimated strain ...
    lw = analytics.log_workout("whoop", day.isoformat(), "Strength", "05:30", 60)
    after = db.query("SELECT strain, extra FROM daily WHERE device='whoop' AND source='ours'")[0]
    assert after["strain"] > base["strain"] and '"strain_estimated_min": 60' in after["extra"], (base, after)
    assert lw["avg_hr"] is None
    # ... one logged over the detected run takes it over and gets the heart-rate numbers ...
    run = analytics.log_workout("whoop", day.isoformat(), "Run", "17:00", 40)
    assert not db.query("SELECT 1 FROM workouts WHERE source='detected'")
    assert run["avg_hr"] and run["zones"], run
    both = db.query("SELECT strain FROM daily WHERE device='whoop' AND source='ours'")[0]
    assert both["strain"] == after["strain"]              # heard minutes are never counted twice
    # ... and removing the logs puts everything back.
    assert analytics.delete_workout("whoop", lw["id"]) and analytics.delete_workout("whoop", run["id"])
    end = db.query("SELECT strain FROM daily WHERE device='whoop' AND source='ours'")[0]
    assert end["strain"] == base["strain"] and len(db.query("SELECT 1 FROM workouts WHERE source='detected'")) == 1
    try:
        analytics.log_workout("whoop", day.isoformat(), "Run", "25:00", 30)
        raise SystemExit("bad time accepted")
    except ValueError:
        pass
    print("ok")
""")


def test_detect_log_and_strain():
    env = dict(os.environ, LOOP_DB=tempfile.mktemp(suffix=".sqlite"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", CODE], env=env, cwd=root, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr

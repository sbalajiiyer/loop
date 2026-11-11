"""Dashboard server: serves the web app and its API. Runs on the Pi.

    uvicorn loop.server:app --host 127.0.0.1 --port 8800
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import analytics, config, db, insights
from .ble import history
from .sources import oura_api, whoop_api

log = logging.getLogger("loop.server")
WEB = config.ROOT / "web"
DEVICES = ("whoop", "oura")
PROVIDERS = {"whoop": whoop_api, "oura": oura_api}
SYNC_EVERY = 30 * 60
RECOMPUTE_EVERY = 10 * 60

app = FastAPI(title="Loop", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=WEB), name="static")


# ------------------------------------------------------------------ background
def sync_source(name: str) -> None:
    mod = PROVIDERS[name]
    if not mod.provider.connected:
        return
    now = int(time.time())
    try:
        detail = mod.sync()
        db.log_sync(name, "official", True, detail, now)
    except Exception as e:  # noqa: BLE001
        log.exception("sync %s failed", name)
        db.log_sync(name, "official", False, str(e), now)


CALIBRATE_EVERY = 24 * 3600


def calibrate_all() -> None:
    for d in DEVICES:
        try:
            analytics.recalibrate(d)
            analytics.recompute(d, days=60)
        except Exception:  # noqa: BLE001
            log.exception("calibrate %s failed", d)


def _loop() -> None:
    last_sync = last_recompute = 0.0
    last_calib = float(db.kv_get("last_calibration", 0))
    while True:
        now = time.time()
        if now - last_calib > CALIBRATE_EVERY:
            calibrate_all()
            last_calib = now
            db.kv_set("last_calibration", now)
        if now - last_sync > SYNC_EVERY:
            for name in PROVIDERS:
                sync_source(name)
            last_sync = now
        if now - last_recompute > RECOMPUTE_EVERY:
            try:
                history.redecode("whoop", incremental=True)
            except Exception:  # noqa: BLE001
                log.exception("history decode failed")
            for d in DEVICES:
                try:
                    analytics.recompute(d, days=3)
                except Exception:  # noqa: BLE001
                    log.exception("recompute %s failed", d)
            last_recompute = now
            try:
                insights.push_new()
            except Exception:  # noqa: BLE001
                log.exception("push failed")
        time.sleep(30)


@app.on_event("startup")
def _start() -> None:
    db.conn()
    threading.Thread(target=_loop, daemon=True).start()


# ------------------------------------------------------------------ pages
PAGE_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#0C111A" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#EEF1F5" media="(prefers-color-scheme: light)">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/icon.svg">
<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style></head><body>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(PAGE_HEAD + (WEB / "app.html").read_text() + "\n</body></html>")


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


# ------------------------------------------------------------------ data API
def _rows(sql: str, params=()) -> list[dict]:
    rows = db.query(sql, params)
    for r in rows:
        if r.get("extra"):
            r["extra"] = json.loads(r["extra"])
            r["extra"].pop("hypnogram", None)  # fetched per night instead
    return rows


@app.get("/api/overview")
def overview(days: int = 120) -> dict:
    since = (datetime.now(analytics.TZ).date() - timedelta(days=days)).isoformat()
    return {
        "generated": int(time.time()),
        "profile": {"max_hr": config.max_hr(), "sleep_need_h": config.SLEEP_NEED_HOURS},
        "daily": _rows("SELECT * FROM daily WHERE day>=? ORDER BY day", (since,)),
        "workouts": _workouts(days),
        "status": status(),
    }


def _workouts(days: int) -> list[dict]:
    rows = db.query("SELECT * FROM workouts WHERE start>=? ORDER BY start", (int(time.time()) - days * 86400,))
    for r in rows:
        r["zones"] = json.loads(r["zones"]) if r.get("zones") else None
        r["day"] = datetime.fromtimestamp(r["start"], analytics.TZ).date().isoformat()  # local day it started
    return rows


WORKOUT_TYPES = ["Strength", "Run", "Walk", "Cycle", "Sport", "Other"]


@app.post("/api/workouts")
async def post_workout(request: Request) -> dict:
    """Log a workout: {day: YYYY-MM-DD, type, start: HH:MM, minutes}."""
    body = await request.json()
    try:
        w = analytics.log_workout("whoop", body["day"], str(body.get("type", "Other")), str(body["start"]),
                                  int(body["minutes"]))
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, f"bad workout: {e}") from e
    return {"ok": True, "id": w["id"]}


@app.delete("/api/workouts/{wid}")
def delete_workout(wid: str) -> dict:
    if not analytics.delete_workout("whoop", wid):
        raise HTTPException(404, "no logged workout with that id")
    return {"ok": True}


@app.get("/api/night")
def night(device: str, day: str) -> dict:
    rows = db.query("SELECT extra, bedtime, waketime FROM daily WHERE device=? AND source='ours' AND day=?",
                    (device, day))
    hyp, curve = [], []
    if rows and rows[0]["extra"]:
        hyp = json.loads(rows[0]["extra"]).get("hypnogram", [])
    if rows and rows[0]["bedtime"]:
        b, w = rows[0]["bedtime"] - 1800, rows[0]["waketime"] + 1800
        curve = db.query("SELECT ts/300*300 AS t, ROUND(AVG(bpm),1) AS bpm FROM hr_samples "
                         "WHERE device=? AND ts BETWEEN ? AND ? AND bpm IS NOT NULL GROUP BY t ORDER BY t",
                         (device, b, w))
    return {"hypnogram": hyp, "hr": curve}


@app.get("/api/live")
def live() -> dict:
    out = {}
    cutoff = int(time.time()) - 90
    for d in DEVICES:
        rows = db.query("SELECT ts, bpm FROM hr_samples WHERE device=? AND ts>=? ORDER BY ts", (d, cutoff))
        out[d] = rows
    return out


@app.get("/api/compare")
def compare(days: int = 60) -> dict:
    pairs = {
        "whoop_ours_vs_official": (("whoop", "ours"), ("whoop", "official")),
        "oura_ours_vs_official": (("oura", "ours"), ("oura", "official")),
        "whoop_vs_oura": (("whoop", "ours"), ("oura", "official")),
        "official_whoop_vs_oura": (("whoop", "official"), ("oura", "official")),
        "ours_whoop_vs_oura": (("whoop", "ours"), ("oura", "ours")),
    }
    metrics = ("hrv", "rhr", "sleep_min", "recovery", "strain", "resp_rate", "bedtime", "waketime")
    return {k: {m: analytics.agreement(a, b, m, days) for m in metrics} for k, (a, b) in pairs.items()}


@app.get("/api/readiness")
def readiness() -> dict:
    return {d: analytics.readiness(d) for d in DEVICES}


@app.get("/api/status")
def status() -> dict:
    out = {}
    for d in DEVICES:
        last = db.query("SELECT MAX(ts) AS ts FROM hr_samples WHERE device=?", (d,))[0]["ts"]
        day_cov = db.query("SELECT COUNT(DISTINCT ts/60) AS n FROM hr_samples WHERE device=? AND ts>=?",
                           (d, int(time.time()) - 86400))[0]["n"]
        mod = PROVIDERS[d]
        syncs = db.query("SELECT at, ok, detail FROM sync_log WHERE device=? ORDER BY at DESC LIMIT 5", (d,))
        out[d] = {
            "calibration": db.kv_get(f"calib:{d}"),
            "bluetooth": {"last_sample": last, "minutes_last_24h": day_cov},
            "official": {"configured": mod.provider.configured, "connected": mod.provider.connected,
                         "recent": syncs},
        }
    return out


@app.post("/api/sync")
def sync_now() -> dict:
    for name in PROVIDERS:
        sync_source(name)
    for d in DEVICES:
        analytics.recompute(d, days=3)
    return {"ok": True}


@app.get("/api/journal")
def get_journal(days: int = 120) -> dict:
    return {"entries": insights.journal(days), "presets": JOURNAL_PRESETS}


JOURNAL_PRESETS = ["Alcohol", "Late meal", "Caffeine after 2pm", "Keto day", "Hard workout",
                   "Meditation", "Screens in bed", "Travel", "Sick"]


@app.post("/api/journal")
async def post_journal(request: Request) -> dict:
    body = await request.json()
    try:
        insights.set_tag(body["day"], body["tag"], bool(body.get("on", True)))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"bad journal entry: {e}") from e
    return {"ok": True}


@app.get("/api/patterns")
def get_patterns(device: str = "", source: str = "") -> dict:
    pref = (device, source) if device and source else insights.preferred_series()
    return {"series": list(pref) if pref else None, "patterns": insights.patterns(*pref) if pref else []}


@app.get("/api/alerts")
def get_alerts() -> dict:
    return {"alerts": insights.alerts()}


@app.get("/api/summary")
def get_summary(day: str = "") -> dict:
    return insights.summary(day or None)


@app.post("/api/ingest")
async def ingest(request: Request) -> dict:
    """Upload endpoint for the laptop collector (over Tailscale)."""
    auth = request.headers.get("authorization", "")
    if not config.INGEST_TOKEN or not hmac.compare_digest(auth, f"Bearer {config.INGEST_TOKEN}"):
        raise HTTPException(401, "bad token")
    body = await request.json()
    device = body.get("device")
    if device not in DEVICES:
        raise HTTPException(400, "unknown device")
    samples = [tuple(s) for s in body.get("samples", [])]
    frames = [tuple(f) for f in body.get("frames", [])]
    if samples:
        db.insert_samples(device, samples)
    if frames:
        db.insert_frames(device, frames)
    return {"ok": True, "samples": len(samples), "frames": len(frames)}


# ------------------------------------------------------------------ OAuth
@app.get("/auth/{name}/start")
def auth_start(name: str):
    mod = PROVIDERS.get(name) or (_ for _ in ()).throw(HTTPException(404))
    if not mod.provider.configured:
        raise HTTPException(400, f"Add {name.upper()}_CLIENT_ID and {name.upper()}_CLIENT_SECRET to .env first")
    return RedirectResponse(mod.provider.start())


@app.get("/auth/{name}/callback")
def auth_callback(name: str, code: str = "", state: str = "", error: str = ""):
    mod = PROVIDERS.get(name) or (_ for _ in ()).throw(HTTPException(404))
    if error:
        return RedirectResponse(f"/#devices?error={error}")
    mod.provider.finish(code, state)
    threading.Thread(target=sync_source, args=(name,), daemon=True).start()
    return RedirectResponse("/#devices?connected=" + name)


@app.post("/api/disconnect/{name}")
def disconnect(name: str) -> dict:
    PROVIDERS[name].provider.disconnect()
    return {"ok": True}

"""SQLite storage. One file, safe to copy for backups (WAL mode)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

-- Raw beat-level samples from our own Bluetooth collection.
CREATE TABLE IF NOT EXISTS hr_samples (
    device   TEXT    NOT NULL,          -- 'whoop' | 'oura'
    ts       INTEGER NOT NULL,          -- unix seconds
    bpm      INTEGER,
    rr_ms    TEXT,                      -- JSON list of R-R intervals (ms) in this second
    motion   REAL,                      -- optional activity magnitude
    PRIMARY KEY (device, ts)
) WITHOUT ROWID;

-- Vendor-computed HRV for short windows (Oura gives RMSSD per 5 minutes of sleep).
CREATE TABLE IF NOT EXISTS hrv_samples (
    device   TEXT    NOT NULL,
    ts       INTEGER NOT NULL,
    rmssd    REAL    NOT NULL,
    PRIMARY KEY (device, ts)
) WITHOUT ROWID;

-- Every Bluetooth frame we receive, kept verbatim so decoders can be improved
-- later and re-run over old data.
CREATE TABLE IF NOT EXISTS raw_frames (
    id       INTEGER PRIMARY KEY,
    device   TEXT    NOT NULL,
    received INTEGER NOT NULL,
    channel  TEXT    NOT NULL,
    hex      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_frames_device_time ON raw_frames(device, received);

-- Per-day metrics. source = 'ours' (computed here) or 'official' (vendor API).
CREATE TABLE IF NOT EXISTS daily (
    device    TEXT NOT NULL,
    source    TEXT NOT NULL,
    day       TEXT NOT NULL,            -- local date the sleep ended / the day was lived
    recovery  REAL,                     -- 0-100 (WHOOP Recovery / Oura Readiness / ours)
    strain    REAL,                     -- 0-21 scale
    hrv       REAL,                     -- RMSSD ms
    rhr       REAL,                     -- bpm
    sleep_min REAL,                     -- minutes asleep
    in_bed_min REAL,
    deep_min  REAL,
    rem_min   REAL,
    light_min REAL,
    awake_min REAL,
    sleep_perf REAL,                    -- % of need
    efficiency REAL,
    bedtime   INTEGER,                  -- unix
    waketime  INTEGER,
    resp_rate REAL,
    spo2      REAL,
    skin_temp REAL,                     -- deviation or absolute, see notes
    calories  REAL,
    steps     INTEGER,
    calibrating INTEGER DEFAULT 0,
    extra     TEXT,                     -- JSON for anything else
    updated   INTEGER,
    PRIMARY KEY (device, source, day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS workouts (
    device   TEXT NOT NULL,
    source   TEXT NOT NULL,
    id       TEXT NOT NULL,
    start    INTEGER,
    end      INTEGER,
    sport    TEXT,
    strain   REAL,
    avg_hr   REAL,
    max_hr   REAL,
    calories REAL,
    zones    TEXT,                      -- JSON: minutes in heart-rate zones 1-5
    PRIMARY KEY (device, source, id)
) WITHOUT ROWID;                        -- source: 'official' | 'detected' | 'logged'

-- Journal: things you did on a day, to see how they affect the following night.
CREATE TABLE IF NOT EXISTS journal (
    day   TEXT NOT NULL,
    tag   TEXT NOT NULL,
    PRIMARY KEY (day, tag)
) WITHOUT ROWID;

-- OAuth tokens and sync cursors.
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Sync history, shown on the Devices screen.
CREATE TABLE IF NOT EXISTS sync_log (
    id      INTEGER PRIMARY KEY,
    device  TEXT,
    source  TEXT,
    at      INTEGER,
    ok      INTEGER,
    detail  TEXT
);
"""

_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_conn: sqlite3.Connection | None = None


def _migrate(c: sqlite3.Connection) -> None:
    """Columns added after the first release (databases created earlier lack them)."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(workouts)")}
    if "zones" not in cols:
        c.execute("ALTER TABLE workouts ADD COLUMN zones TEXT")
        c.commit()


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        _conn.executescript(SCHEMA)
        _migrate(_conn)
    return _conn


@contextmanager
def tx():
    with _lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def kv_get(key: str, default: Any = None) -> Any:
    rows = query("SELECT value FROM kv WHERE key=?", (key,))
    return json.loads(rows[0]["value"]) if rows else default


def kv_set(key: str, value: Any) -> None:
    with tx() as c:
        c.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))


def kv_del(key: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM kv WHERE key=?", (key,))


DAILY_FIELDS = ["recovery", "strain", "hrv", "rhr", "sleep_min", "in_bed_min", "deep_min", "rem_min",
                "light_min", "awake_min", "sleep_perf", "efficiency", "bedtime", "waketime", "resp_rate",
                "spo2", "skin_temp", "calories", "steps", "calibrating", "extra"]


def upsert_daily(device: str, source: str, day: str, values: dict, now: int) -> None:
    """Merge values into a day row (None never overwrites an existing value)."""
    vals = {k: values.get(k) for k in DAILY_FIELDS if k in values}
    if "extra" in vals and not isinstance(vals["extra"], (str, type(None))):
        vals["extra"] = json.dumps(vals["extra"])
    cols = ", ".join(["device", "source", "day", "updated", *vals])
    qs = ", ".join("?" * (4 + len(vals)))
    sets = ", ".join([f"{k}=COALESCE(excluded.{k}, daily.{k})" for k in vals] + ["updated=excluded.updated"])
    with tx() as c:
        c.execute(f"INSERT INTO daily({cols}) VALUES({qs}) ON CONFLICT(device,source,day) DO UPDATE SET {sets}",
                  (device, source, day, now, *vals.values()))


def insert_samples(device: str, rows: Iterable[tuple], prefer_existing: bool = False) -> int:
    """rows: (ts, bpm, rr_list|None, motion|None). Duplicate timestamps merge.
    prefer_existing keeps what's stored (used when back-filling from decoded history,
    so live readings are never overwritten)."""
    data = [(device, int(ts), bpm, json.dumps(rr) if rr else None, motion) for ts, bpm, rr, motion in rows]
    with tx() as c:
        if prefer_existing:
            c.executemany("INSERT OR IGNORE INTO hr_samples(device,ts,bpm,rr_ms,motion) VALUES(?,?,?,?,?)", data)
            return len(data)
        c.executemany(
            "INSERT INTO hr_samples(device,ts,bpm,rr_ms,motion) VALUES(?,?,?,?,?) "
            "ON CONFLICT(device,ts) DO UPDATE SET bpm=COALESCE(excluded.bpm,bpm), "
            "rr_ms=COALESCE(excluded.rr_ms,rr_ms), motion=COALESCE(excluded.motion,motion)", data)
    return len(data)


def insert_hrv(device: str, rows: Iterable[tuple]) -> None:
    """rows: (ts, rmssd_ms)."""
    with tx() as c:
        c.executemany("INSERT OR REPLACE INTO hrv_samples(device,ts,rmssd) VALUES(?,?,?)",
                      [(device, int(ts), v) for ts, v in rows])


def insert_frames(device: str, rows: Iterable[tuple]) -> None:
    """rows: (received_ts, channel, hex)."""
    with tx() as c:
        c.executemany("INSERT INTO raw_frames(device,received,channel,hex) VALUES(?,?,?,?)",
                      [(device, *r) for r in rows])


def log_sync(device: str, source: str, ok: bool, detail: str, now: int) -> None:
    with tx() as c:
        c.execute("INSERT INTO sync_log(device,source,at,ok,detail) VALUES(?,?,?,?,?)",
                  (device, source, now, int(ok), detail[:500]))

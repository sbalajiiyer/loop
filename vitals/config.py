"""Settings, read from environment variables or a .env file next to the project."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


DB_PATH = Path(get("VITALS_DB", str(ROOT / "data" / "vitals.sqlite")))
# Public base URL of the dashboard (your Tailscale HTTPS name), used for OAuth redirects.
BASE_URL = get("VITALS_BASE_URL", "http://localhost:8800").rstrip("/")
# Shared secret the laptop collector uses when uploading to the Pi.
INGEST_TOKEN = get("VITALS_INGEST_TOKEN", "")
TIMEZONE = get("VITALS_TZ", "America/Detroit")

# Personal profile used by our own scores.
MAX_HR = int(get("VITALS_MAX_HR", "0") or 0)        # 0 = estimate from age
AGE = int(get("VITALS_AGE", "45") or 45)
SLEEP_NEED_HOURS = float(get("VITALS_SLEEP_NEED_HOURS", "8") or 8)

WHOOP_CLIENT_ID = get("WHOOP_CLIENT_ID")
WHOOP_CLIENT_SECRET = get("WHOOP_CLIENT_SECRET")
# Only import official WHOOP data recorded on or after this date (the account's
# older history belongs to its previous wearer).
WHOOP_IMPORT_FROM = get("WHOOP_IMPORT_FROM", "2026-10-23")

OURA_CLIENT_ID = get("OURA_CLIENT_ID")
OURA_CLIENT_SECRET = get("OURA_CLIENT_SECRET")
OURA_IMPORT_FROM = get("OURA_IMPORT_FROM", "2026-01-01")


def max_hr() -> int:
    return MAX_HR or round(208 - 0.7 * AGE)  # Tanaka estimate

"""Where collected data goes: straight into the local database (on the Pi), or
uploaded over Tailscale to the Pi (when the collector runs on your laptop)."""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("vitals.sink")


class LocalSink:
    def __init__(self) -> None:
        from .. import db
        self.db = db

    def write(self, device: str, samples: list[tuple], frames: list[tuple]) -> None:
        if samples:
            self.db.insert_samples(device, samples)
        if frames:
            self.db.insert_frames(device, frames)

    def write_history(self, device: str, samples: list[tuple]) -> None:
        self.db.insert_samples(device, samples, prefer_existing=True)


class HttpSink:
    """Uploads batches to the Pi. If the Pi is unreachable (no Tailscale, offline),
    batches are spooled to disk and retried, so a long trip loses nothing."""

    def __init__(self, base_url: str, token: str, spool_dir: Path) -> None:
        self.url = base_url.rstrip("/") + "/api/ingest"
        self.token = token
        self.spool = spool_dir
        self.spool.mkdir(parents=True, exist_ok=True)

    def _post(self, body: bytes) -> bool:
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status == 200
        except Exception as e:  # noqa: BLE001
            log.warning("upload failed, spooling: %s", e)
            return False

    def write(self, device: str, samples: list[tuple], frames: list[tuple]) -> None:
        body = json.dumps({"device": device, "samples": samples, "frames": frames}).encode()
        # Retry anything spooled first, oldest first.
        for f in sorted(self.spool.glob("*.json")):
            if not self._post(f.read_bytes()):
                break
            f.unlink()
        if not self._post(body):
            (self.spool / f"{time.time_ns()}.json").write_bytes(body)

"""Claude connector (MCP over stdio) for your Loop data.

Runs on the computer where you use the Claude desktop app and reads from the Pi
over Tailscale, so your data never leaves your own network except as the answers
Claude shows you. No extra packages needed.

Claude desktop app → Settings → Developer → Edit Config, add:

    "mcpServers": {
      "loop": {
        "command": "python3",
        "args": ["-m", "loop.mcp_server"],
        "cwd": "/path/to/loop",
        "env": {"LOOP_URL": "https://loop.<your-tailnet>.ts.net"}
      }
    }
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

URL = os.environ.get("LOOP_URL", "http://localhost:8800").rstrip("/")
FIELDS = ["recovery", "strain", "hrv", "rhr", "sleep_min", "deep_min", "rem_min", "sleep_perf",
          "resp_rate", "spo2", "skin_temp", "bedtime", "waketime", "calibrating"]


def _get(path: str, **params) -> dict:
    q = ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})) if params else ""
    with urllib.request.urlopen(URL + path + q, timeout=20) as r:
        return json.loads(r.read())


def morning_brief(day: str = "") -> dict:
    return _get("/api/summary", day=day)


def daily_metrics(days: int = 14, device: str = "", source: str = "") -> dict:
    data = _get("/api/overview", days=max(1, min(int(days), 365)))
    rows = [{"day": r["day"], "device": r["device"], "source": r["source"],
             **{k: r.get(k) for k in FIELDS if r.get(k) is not None}}
            for r in data["daily"]
            if (not device or r["device"] == device) and (not source or r["source"] == source)]
    return {"units": {"hrv": "ms (RMSSD)", "rhr": "bpm", "sleep_min": "minutes", "recovery": "0-100",
                      "strain": "0-21", "bedtime/waketime": "unix seconds"},
            "notes": "source 'ours' = computed by Loop from raw strap data; 'official' = vendor app scores.",
            "rows": rows}


def agreement(days: int = 60) -> dict:
    data = _get("/api/compare", days=days)
    for pair in data.values():
        for m in pair.values():
            m.pop("pairs", None)
    return data


def cancel_readiness() -> dict:
    data = _get("/api/readiness")
    for d in data.values():
        for c in d.get("checks", {}).values():
            c.pop("pairs", None)
    return data


def workouts(days: int = 14) -> dict:
    data = _get("/api/overview", days=max(1, min(int(days), 365)))
    keep = ("day", "source", "sport", "start", "end", "strain", "avg_hr", "max_hr", "zones")
    return {"notes": "source 'detected' = found in heart rate; 'logged' = entered by the user; 'official' = WHOOP app. "
                     "zones = minutes in heart-rate zones 1-5.",
            "rows": [{k: w.get(k) for k in keep if w.get(k) is not None} for w in data["workouts"] if w["device"] == "whoop"]}


def habit_effects() -> dict:
    return _get("/api/patterns")


def alerts() -> dict:
    return _get("/api/alerts")


def journal(days: int = 30) -> dict:
    return _get("/api/journal", days=days)


TOOLS = {
    "morning_brief": (morning_brief, "Today's (or a given day's) recovery, HRV, resting HR, sleep and strain summary in plain language.",
                      {"day": {"type": "string", "description": "YYYY-MM-DD; empty for today"}}),
    "daily_metrics": (daily_metrics, "Day-by-day metrics from WHOOP and Oura, both official and Loop' own ('ours').",
                      {"days": {"type": "integer", "default": 14}, "device": {"type": "string", "enum": ["", "whoop", "oura"]},
                       "source": {"type": "string", "enum": ["", "ours", "official"]}}),
    "agreement": (agreement, "How closely Loop' own numbers match the official ones, and WHOOP vs Oura.",
                  {"days": {"type": "integer", "default": 60}}),
    "cancel_readiness": (cancel_readiness, "Whether each membership could be cancelled yet, based on 30 nights of agreement.", {}),
    "workouts": (workouts, "Workouts per day: detected from heart rate, logged by the user, or from the WHOOP app.",
                 {"days": {"type": "integer", "default": 14}}),
    "habit_effects": (habit_effects, "How journal tags (alcohol, late meals, etc.) change the following night.", {}),
    "alerts": (alerts, "Current health or sync alerts (e.g. raised resting HR, strap not synced).", {}),
    "journal": (journal, "Journal tags logged per day.", {"days": {"type": "integer", "default": 30}}),
}


def handle(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notification
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "loop", "version": "1.0"},
                  "instructions": "Personal WHOOP and Oura data from the user's own Loop dashboard. "
                                  "'ours' scores are approximations computed by Loop; say so when it matters."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p}}
                            for n, (_, d, p) in TOOLS.items()]}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            out = TOOLS[name][0](**args)
            result = {"content": [{"type": "text", "text": json.dumps(out, default=str)}]}
        except Exception as e:  # noqa: BLE001
            result = {"content": [{"type": "text", "text": f"Couldn't reach Loop at {URL}: {e}. "
                                   "Check that the Pi is on and Tailscale is connected."}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method {method} not supported"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

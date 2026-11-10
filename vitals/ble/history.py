"""Working out the WHOOP MG stored-history record layout from your own data.

The strap's live heart-rate broadcast (0x2A37) is reliable, so it serves as ground
truth. History records are stored raw. This tool lines the two up:

  1. reassemble every stored history frame (packet type 47 by default)
  2. find the byte offset holding a unix timestamp in the range we have live data for
  3. for every other offset, check how often its value matches the live heart rate
     at that timestamp (8-bit) and whether a 16-bit field tracks the R-R interval
  4. print the best candidates; --save writes data/history_layout.json, after which
     the collector decodes history frames as they arrive and `redecode` fills old data

    python -m vitals.ble.history probe
    python -m vitals.ble.history probe --save
    python -m vitals.ble.history redecode

Assumes one sample per record. If the report shows several timestamps or repeated
heart-rate fields per record, the layout needs a closer look before saving.
"""
from __future__ import annotations

import argparse
import json
import struct
from collections import defaultdict

from .. import config, db
from . import protocol as P
from .frames import FrameAssembler

LAYOUT_PATH = config.DB_PATH.parent / "history_layout.json"
DEVICE = "whoop"


def history_records(device: str = DEVICE, packet_types: tuple[int, ...] = (P.PT_HISTORICAL,),
                    after_id: int = 0) -> list[bytes]:
    """Reassemble stored raw notifications into frames, per channel, in arrival order."""
    rows = db.query("SELECT channel, hex FROM raw_frames WHERE device=? AND id>? ORDER BY id", (device, after_id))
    asm: dict[str, FrameAssembler] = defaultdict(FrameAssembler)
    out = []
    for r in rows:
        for f in asm[r["channel"]].feed(bytes.fromhex(r["hex"])):
            if f.packet_type in packet_types:
                out.append(f.payload)
    return out


def _truth(device: str) -> dict[int, tuple[int, list[float]]]:
    rows = db.query("SELECT ts, bpm, rr_ms FROM hr_samples WHERE device=? AND bpm IS NOT NULL", (device,))
    return {r["ts"]: (r["bpm"], json.loads(r["rr_ms"]) if r["rr_ms"] else []) for r in rows}


def probe(records: list[bytes], truth: dict[int, tuple[int, list[float]]]) -> dict:
    if not records or not truth:
        return {"ok": False, "reason": "need both stored history frames and live heart-rate data"}
    lo, hi = min(truth) - 15 * 86400, max(truth) + 86400
    width = min(len(r) for r in records)

    # 1. timestamp offset: the uint32 that is most often a plausible time
    ts_scores = []
    for off in range(0, width - 3):
        vals = [struct.unpack_from("<I", r, off)[0] for r in records]
        ok = sum(lo <= v <= hi for v in vals)
        ts_scores.append((ok / len(records), off))
    ts_scores.sort(reverse=True)
    share, ts_off = ts_scores[0]
    if share < 0.8:
        return {"ok": False, "reason": f"no consistent timestamp field (best offset {ts_off} plausible in {share:.0%})"}

    # 2. join records to live truth (allow ±1 s)
    joined = []
    for r in records:
        ts = struct.unpack_from("<I", r, ts_off)[0]
        t = truth.get(ts) or truth.get(ts - 1) or truth.get(ts + 1)
        if t:
            joined.append((r, t))
    if len(joined) < 30:
        return {"ok": False, "ts_offset": ts_off, "reason": f"only {len(joined)} records overlap live data; "
                "keep the strap near the collector longer, then probe again"}

    skip = set(range(ts_off, ts_off + 4))
    bpm_cands = []
    for off in range(width):
        if off in skip:
            continue
        hits = sum(abs(r[off] - t[0]) <= 2 for r, t in joined)
        bpm_cands.append((hits / len(joined), off))
    bpm_cands.sort(reverse=True)

    rr_cands = []
    for off in range(width - 1):
        if off in skip or off + 1 in skip:
            continue
        hits = n = 0
        for r, t in joined:
            v = struct.unpack_from("<H", r, off)[0]
            if not 300 <= v <= 2000:
                continue
            n += 1
            ref = st_mean(t[1]) if t[1] else 60000 / t[0]
            hits += abs(v - ref) <= 0.12 * ref
        rr_cands.append((hits / len(joined), off))
    rr_cands.sort(reverse=True)

    best_bpm, best_rr = bpm_cands[0], rr_cands[0] if rr_cands else (0, None)
    layout = {"packet_type": P.PT_HISTORICAL, "ts_offset": ts_off, "bpm_offset": best_bpm[1],
              "rr_offset": best_rr[1] if best_rr[0] >= 0.6 else None}
    return {"ok": best_bpm[0] >= 0.8, "records": len(records), "overlap": len(joined),
            "ts_offset": ts_off, "ts_plausible": round(share, 3),
            "bpm_candidates": [(o, round(s, 3)) for s, o in bpm_cands[:5]],
            "rr_candidates": [(o, round(s, 3)) for s, o in rr_cands[:5]],
            "layout": layout,
            "reason": None if best_bpm[0] >= 0.8 else "no byte matches live heart rate in 80% of records"}


def st_mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def load_layout() -> dict | None:
    try:
        return json.loads(LAYOUT_PATH.read_text())
    except (OSError, ValueError):
        return None


def decode(payload: bytes, layout: dict) -> tuple | None:
    """One history record -> (ts, bpm, rr_list|None, None) using a saved layout."""
    need = max(layout["ts_offset"] + 4, layout["bpm_offset"] + 1, (layout.get("rr_offset") or 0) + 2)
    if len(payload) < need or payload[0] != layout["packet_type"]:
        return None
    ts = struct.unpack_from("<I", payload, layout["ts_offset"])[0]
    bpm = payload[layout["bpm_offset"]]
    if not 25 <= bpm <= 230:
        return None
    rr = None
    if layout.get("rr_offset") is not None:
        v = struct.unpack_from("<H", payload, layout["rr_offset"])[0]
        rr = [float(v)] if 300 <= v <= 2000 else None
    return (ts, bpm, rr, None)


def redecode(device: str = DEVICE, incremental: bool = False) -> int:
    """Decode stored history frames into heart-rate samples (never overwriting live
    readings). incremental=True only looks at frames stored since the last run; the
    server does this hourly, which also covers uploads from the laptop collector."""
    layout = load_layout()
    if not layout:
        if incremental:
            return 0
        raise SystemExit("No saved layout yet. Run: python -m vitals.ble.history probe --save")
    after = db.kv_get(f"history_cursor:{device}", 0) if incremental else 0
    top = db.query("SELECT COALESCE(MAX(id),0) AS m FROM raw_frames WHERE device=?", (device,))[0]["m"]
    rows = [d for p in history_records(device, (layout["packet_type"],), after) if (d := decode(p, layout))]
    db.insert_samples(device, rows, prefer_existing=True)
    db.kv_set(f"history_cursor:{device}", top)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--save", action="store_true")
    p.add_argument("--type", type=int, default=P.PT_HISTORICAL)
    sub.add_parser("redecode")
    args = ap.parse_args()
    if args.cmd == "probe":
        res = probe(history_records(DEVICE, (args.type,)), _truth(DEVICE))
        print(json.dumps(res, indent=2))
        if args.save:
            if not res.get("ok"):
                raise SystemExit("Not saving: the match isn't strong enough yet.")
            LAYOUT_PATH.write_text(json.dumps(res["layout"], indent=2))
            print(f"saved {LAYOUT_PATH}")
    else:
        print(f"decoded {redecode()} history samples")


if __name__ == "__main__":
    main()

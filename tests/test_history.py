import os
import random
import struct
import tempfile


from loop import db  # noqa: E402
from loop.ble import history  # noqa: E402
from loop.ble.frames import build_frame  # noqa: E402


def make_record(ts: int, bpm: int, rr: int, r: random.Random) -> bytes:
    # Hypothetical layout: [47][seq][x][x] [ts u32 @4] [junk x4] [bpm @12] [x] [rr u16 @14] [junk...]
    body = bytearray([47, r.randrange(256), 1, 0]) + struct.pack("<I", ts) + bytes(r.randrange(256) for _ in range(4))
    body += bytes([bpm, r.randrange(256)]) + struct.pack("<H", rr) + bytes(r.randrange(256) for _ in range(10))
    return bytes(body)


def test_probe_finds_layout_and_redecodes():
    r = random.Random(5)
    t0 = 1_792_000_000
    truth, frames = [], []
    for i in range(400):
        ts, bpm = t0 + i, 55 + int(10 * abs(((i % 120) - 60) / 60))
        rr = round(60000 / bpm)
        if i % 2 == 0:  # live data overlaps half of the history
            truth.append((ts, bpm, [float(rr)], None))
        frame = build_frame(make_record(ts, bpm, rr, r))
        for k in range(0, len(frame), 20):  # stored as MTU-sized notifications
            frames.append((ts, "data", frame[k:k + 20].hex()))
    db.insert_samples("whoop", truth)
    db.insert_frames("whoop", frames)

    res = history.probe(history.history_records(), history._truth("whoop"))
    assert res["ok"], res
    assert res["layout"]["ts_offset"] == 4 and res["layout"]["bpm_offset"] == 12 and res["layout"]["rr_offset"] == 14

    history.LAYOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    history.LAYOUT_PATH.write_text(__import__("json").dumps(res["layout"]))
    assert history.redecode() == 400
    assert db.query("SELECT COUNT(*) n FROM hr_samples WHERE device='whoop'")[0]["n"] == 400
    history.LAYOUT_PATH.unlink()

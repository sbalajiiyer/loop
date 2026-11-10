import math
import random

from vitals.analytics import resp_rate, rmssd


def synth_rr(breaths_per_min: float, seconds: int = 300, hr: float = 58, seed: int = 1) -> list[float]:
    r, t, out = random.Random(seed), 0.0, []
    while t < seconds:
        x = 60000 / hr + 45 * math.sin(2 * math.pi * breaths_per_min / 60 * t) + r.gauss(0, 12)
        out.append(x)
        t += x / 1000
    return out


def test_breathing_rate_recovered():
    for bpm in (9, 12.5, 15, 19):
        assert abs(resp_rate(synth_rr(bpm)) - bpm) <= 1.0


def test_breathing_rate_rejects_noise():
    r = random.Random(3)
    assert resp_rate([1000 + r.gauss(0, 30) for _ in range(300)]) is None


def test_rmssd_ignores_artifacts():
    rr = synth_rr(14)
    clean = rmssd(rr)
    dirty = rr[:]
    dirty[50] = 2600  # missed beat
    dirty[120] = 250  # spurious beat
    assert abs(rmssd(dirty) - clean) / clean < 0.1

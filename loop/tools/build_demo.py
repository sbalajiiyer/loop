"""Bundle the dashboard with sample data into one self-contained HTML file
(for previewing without a Pi).

    LOOP_DB=data/demo.sqlite python -m loop.tools.build_demo out.html
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

from .. import config, server


def main(out: str) -> None:
    overview = server.overview(days=120)
    days = sorted({r["day"] for r in overview["daily"]})
    nights = {f"whoop:{d}": server.night("whoop", d) for d in days[-21:]}
    data = {"overview": overview, "compare": server.compare(days=60), "readiness": server.readiness(), "nights": nights,
            "summaries": {d: server.get_summary(d) for d in days[-21:]}, "alerts": server.get_alerts(),
            "journal": server.get_journal(120), "patterns": server.get_patterns()}
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    page = (config.ROOT / "web" / "app.html").read_text()
    marker = "<script>\n(() => {"
    assert marker in page
    page = page.replace(marker, f'<script id="demo-data" type="application/json">{blob}</script>\n{marker}', 1)
    with open(out, "w") as f:
        f.write(page)
    print(f"wrote {out} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "loop-preview.html")

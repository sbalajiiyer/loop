# Vitals Lab

A self-hosted dashboard for your WHOOP MG and Oura Ring 4. It runs on a Raspberry Pi
Zero 2 W, you open it from your iPhone or laptop over Tailscale, and nothing depends
on a subscription once the trial period is over.

```
 WHOOP MG ──Bluetooth──▶ Pi Zero 2 W (bedroom) ──▶ SQLite ──▶ dashboard
              (travel)   laptop collector ──Tailscale──┘          ▲
 WHOOP API (temporary baseline) ───────────────┐                  │
 Oura API  (baseline while membership lasts) ──┴──────────────────┘
                                                  iPhone / laptop via Tailscale
```

## What works today

| | Status |
|---|---|
| WHOOP MG heart rate + R-R intervals (Bluetooth) | Works: standard heart-rate profile |
| Our HRV, resting HR, sleep timing, strain, recovery | Computed from the above |
| Our breathing rate | Computed from the heartbeat rhythm (breathing speeds the heart up and down) |
| Sleep stages (ours) | Rough estimate from heart rate, labelled "est." |
| WHOOP MG stored-history offload | Requested and stored raw; `vitals.ble.history probe` works out the layout from your own data |
| WHOOP MG SpO₂, skin temp | Not decoded yet ("not yet" in the app) |
| WHOOP official API | Works once authorized (temporary baseline) |
| Oura official API | Works with your membership |
| Oura · Ours | Our formulas on Oura's heart-rate and 5-minute HRV data (needs the membership) |
| Oura Ring 4 Bluetooth | Not yet; added once community support is reliable |
| Auto-tuning | Our Recovery and Strain are fitted to the official scores daily; the fit is kept after you cancel |
| Journal, patterns, alerts, morning brief | In the app; optional phone notifications via ntfy |
| Claude connector | Ask Claude about your data from the desktop app |

Every raw Bluetooth frame is kept in the database, so when decoding improves, old
data can be re-processed. The strap is only ever sent a short allow-list of commands
(session start, heart-rate broadcast, history request/ack, battery). Nothing that
reboots, resets or changes firmware can be sent.

**Important while you trial this:** don't install WHOOP firmware updates once you
rely on the Pi, and close the WHOOP app on your phone while the Pi is collecting
(the strap accepts one connection at a time).

## Try it now (no Pi needed)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
VITALS_DB=data/demo.sqlite python -m vitals.tools.seed_demo
VITALS_DB=data/demo.sqlite uvicorn vitals.server:app --port 8800
# open http://localhost:8800
```

## Start now on your laptop (before the Pi arrives)

This gets the official WHOOP and Oura data flowing so the 30-night comparison and
auto-tuning start immediately. You move everything to the Pi later.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # set VITALS_BASE_URL=http://localhost:8800 and the API keys
uvicorn vitals.server:app --port 8800
```

In the WHOOP and Oura developer dashboards, register `http://localhost:8800/auth/whoop/callback`
and `http://localhost:8800/auth/oura/callback` now, and the Tailscale versions later (both
can be listed). Open http://localhost:8800 → Devices → Connect. The server only syncs while
it's running, but each sync catches up the last few days, so running it daily is enough.

**Moving to the Pi:** stop the server, copy `data/vitals.sqlite` and `.env` to the Pi,
change `VITALS_BASE_URL` to the Tailscale address, and start the Pi services. Your
connections and history carry over.

## Set up the Pi

1. **Flash** Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. In its settings,
   set the hostname to `vitals`, enable SSH, and add your Wi-Fi.
2. **Install:**
   ```bash
   sudo apt update && sudo apt install -y python3-venv bluez git
   sudo usermod -aG bluetooth pi
   # copy this folder to /home/pi/vitals, then:
   cd ~/vitals && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env
   ```
3. **Tailscale** (free personal plan):
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   sudo tailscale serve --bg 8800
   tailscale serve status     # shows https://vitals.<your-tailnet>.ts.net
   ```
   Put that address in `.env` as `VITALS_BASE_URL`. Install Tailscale on your iPhone
   and laptop and sign in with the same account. Nothing is exposed to the internet.
4. **Pair the strap:** close the WHOOP app on your phone, then
   ```bash
   .venv/bin/python -m vitals.ble.whoop_mg scan
   ```
   Put the address in `.env` as `WHOOP_ADDRESS`. If nothing shows up, put the strap in
   pairing mode and scan again.
5. **Start the services:**
   ```bash
   sudo cp deploy/*.service /etc/systemd/system/
   sudo systemctl enable --now vitals-server vitals-collector
   journalctl -u vitals-collector -f     # watch it connect
   ```
6. **Backups:** `chmod +x deploy/backup.sh`, add the cron line from the top of the
   script, and optionally run `rclone config` to add an offsite remote named `offsite`.
7. **On your iPhone:** open the Tailscale address in Safari, then Share → Add to Home
   Screen. It opens full-screen like an app.

## Connect the official APIs (trial-period baselines)

**WHOOP** (your wife's account, from your first day of wearing it):
1. At developer-dashboard.whoop.com, create an app. Redirect URL:
   `https://vitals.<tailnet>.ts.net/auth/whoop/callback`. Copy the client ID and secret into `.env`.
2. Set `WHOOP_IMPORT_FROM` to the first day you wore the strap.
3. Restart the server, open Devices → Connect WHOOP MG, and have your wife sign in and approve.
4. Ask her to update the account's height, weight and age to yours.

**Oura:**
1. At cloud.ouraring.com/oauth/applications, create an app with redirect
   `https://vitals.<tailnet>.ts.net/auth/oura/callback`. Copy the ID and secret into `.env`.
2. Restart the server, open Devices → Connect Oura Ring 4, and approve.

## Travelling more than 14 days

Run the same collector on your laptop. It uploads to the Pi over Tailscale and, if
the Pi can't be reached, saves batches locally and sends them later.

```bash
pip install -r requirements.txt
# put VITALS_INGEST_TOKEN (same value as on the Pi) and WHOOP_ADDRESS in .env
python -m vitals.ble.whoop_mg run --upload https://vitals.<tailnet>.ts.net
```

Leave it running with the strap nearby for an hour or so to drain the backlog. The
strap may need a one-time re-pair the first time it switches between Pi and laptop.

## Decoding the strap's stored history

Once the Pi has collected live heart rate *and* the strap has offloaded history
for a few days (both happen automatically), run:

```bash
.venv/bin/python -m vitals.ble.history probe          # shows what it found
.venv/bin/python -m vitals.ble.history probe --save   # only saves a strong match
.venv/bin/python -m vitals.ble.history redecode       # fills old data from stored frames
```

It lines up the stored records with the live heart rate to find where the time and
heart rate sit in each record. After saving, history is decoded automatically, which
fills the hours you were away from the Pi and makes daily Strain complete. Live
readings are never overwritten.

## Journal, alerts and notifications

- **Journal** (Today screen): tap tags for what you did that day. **Trends** shows how
  each tag shifts the following night once you have a week or so of entries.
- **Alerts:** two nights of raised resting HR with lowered HRV, three low-recovery days,
  the strap not syncing for 3+ days (critical at 10, before its 14-day memory fills),
  and failing official syncs.
- **Phone notifications (optional):** install the ntfy app, subscribe to a long random
  topic, and set `NTFY_URL=https://ntfy.sh/<that-topic>` in `.env`. You get new alerts
  and a morning brief after `BRIEF_HOUR`. The messages carry your summary numbers, so
  use a hard-to-guess topic or run your own ntfy server.

## Ask Claude about your data

On the computer where you use the Claude desktop app (with Tailscale on), add this
under Settings → Developer → Edit Config:

```json
"mcpServers": {
  "vitals": {
    "command": "python3",
    "args": ["-m", "vitals.mcp_server"],
    "cwd": "/path/to/vitals",
    "env": { "VITALS_URL": "https://vitals.<your-tailnet>.ts.net" }
  }
}
```

Then ask things like "How did I sleep this week?" or "Is alcohol hurting my recovery?".
It needs no extra packages and only reads from your Pi.

## Deciding when to cancel

The **Compare** tab tracks, night by night, how closely our numbers match the
official ones (and our WHOOP HRV against Oura's). A device shows **Ready** after 30
paired nights with at least 80% of nights inside the tolerance for HRV (±15%),
resting HR (±3 bpm) and sleep time (±30 min). Auto-tuning runs daily during this
period and its fitted numbers are kept, so cancelling doesn't undo it. Oura can't
reach Ready until Ring 4 Bluetooth works, because its "ours" still uses Oura's API.
Export your full history from the
official app before cancelling. Ignore WHOOP's own Recovery for the first ~3 weeks,
while it recalibrates from the previous wearer's baseline.

## Layout

```
vitals/ble/        Bluetooth: framing + CRCs, protocol constants, WHOOP MG collector, upload spooler
vitals/sources/    WHOOP and Oura official API sync (OAuth)
vitals/ble/history.py  stored-history layout finder + decoder
vitals/analytics.py  our sleep, HRV, RHR, breathing, recovery, strain, auto-tuning, agreement
vitals/insights.py   journal patterns, alerts, morning brief, ntfy push
vitals/mcp_server.py Claude desktop connector
vitals/server.py   FastAPI app: dashboard, data API, ingest, OAuth
web/app.html       the dashboard (single file, no build step)
deploy/            systemd units, backup script
tests/             framing tests (CLIENT_HELLO rebuilt byte for byte)
```

Protocol knowledge comes from the community reverse-engineering projects
b-nnett/goose, NOOP and dofek; none of their code is included here.

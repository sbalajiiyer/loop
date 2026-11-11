# Loop: project handoff

Self-hosted WHOOP MG + Oura Ring 4 dashboard. Work was done on 2026-10-23; this handoff
was written on 2026-11-10. Owner: Balaji (GitHub `sbalajiiyer`), Troy, Michigan, timezone
`America/Detroit`.

**Where things are**
- Name: the project is branded **Loop** (renamed from "Vitals Lab" on 2026-11-11). The Python package is `loop`,
  settings are `LOOP_*`, the services are `loop-server` and `loop-collector`, the database is `data/loop.sqlite`,
  and the Pi's hostname is `loop`.
- Code: a Git repository named `loop`. It is pushed to the private GitHub repo
  `sbalajiiyer/loop` (branch `main`), which is now the source of truth.
  Every file is also reproduced in full in Part 7 of this document.
- Live preview (sample data, no real data): https://claude.ai/artifact/PKFXL5e8Gt86P4P4UpWzRm
  (a single HTML file built by `loop/tools/build_demo.py`).
- Project name in Claude: "Whoop Oura Dash".

---

## 0. Update, 2026-11-10 (read this first; it overrides sections 1-6 where they differ)

**Direction changed.** The app is a WHOOP replacement first. The main screens show one number
per metric, from our own scores. Comparisons moved to a separate Compare screen.

**What changed in the code** (12 tests pass; still verified on synthetic data only)
- `web/app.html`: rebuilt. Tabs are Today, Sleep, Trends, Devices. The four filter chips are gone.
  - Devices → Display has two switches, remembered per browser: *Hide metrics with no data*
    (default on) and *Fill gaps with WHOOP's official values* (default on, values tagged "official").
  - Each ring shows data coverage ("15h 23m of 24h", "partial" under 70% for Strain, under 80% of the night for Recovery/Sleep).
  - *Tonight* panel: in-bed time and sleep target = need + 25% of the last 7 nights' debt (max 60 min)
    + 15/30 min after strain ≥10/≥14, working back from the median wake time of the last 14 days. Computed in the browser (`sleepPlan`).
  - *Workouts* panel: detected and logged workouts, heart-rate zone bars, a log form.
  - *Compare* (from Devices; hash `#compare`): three pairs only (ours vs WHOOP official, ours vs Oura official,
    WHOOP official vs Oura official) and "Ready to cancel WHOOP?". "Oura · Ours" is no longer shown anywhere in the UI
    (the server still computes it).
- `loop/analytics.py`: `detect_workouts`, `log_workout`, `delete_workout`, `recompute_day`; `strain_score` now adds
  logged-workout minutes the strap didn't hear at a typical effort (`LOGGED_EFFORT`) and records `extra.strain_estimated_min`.
- `loop/db.py`: `workouts.zones` column (JSON, minutes in zones 1-5) with an automatic migration; `workouts.source` is
  `official`, `detected` or `logged`.
- `loop/server.py`: `POST /api/workouts`, `DELETE /api/workouts/{id}`; overview workouts carry `zones` and local `day`.
- `loop/mcp_server.py`: `workouts` tool. `loop/tools/seed_demo.py`: seeds logged strength sessions.
- `tests/test_workouts.py`: detection, logging, strain accounting, removal.

**Review findings not yet acted on** (from the 2026-11-10 review)
1. Nothing has touched the real strap. Run `python -m loop.ble.whoop_mg run --no-history` on the laptop first and confirm
   that 0x2A37 delivers R-R intervals on the MG. Everything "ours" depends on it.
2. During the trial the collector should be read-only (no history ack, command 23), or the WHOOP app loses the data it
   needs to produce the official scores we compare against. Not implemented.
3. "Ready to cancel" checks HRV, RHR and sleep time only, and tuning is graded on the nights it was fitted on.
   Wanted: held-out validation, Recovery and Strain in the gate, a daytime-coverage gate.
4. Unknown: whether the strap keeps recording after the membership lapses; the membership renewal date.
5. Sleep stages are a heart-rate-only heuristic with no motion data; treat the hypnogram as rough.
6. Ideas not built: strap battery on Devices, collector-down alert, strain target, sleep consistency, normal-range bands, weekly summary.

**Where things are now**
- Code: `loop.zip` sent in the 2026-11-10 chat; every file is reproduced below. Still not in a Git host.
- Preview (sample data): https://claude.ai/artifact/PKFXL5e8Gt86P4P4UpWzRm

---

## 1. Goal and requirements

**Goal:** pull my own WHOOP and Oura data myself, see it in one high-quality dashboard
that gives the same kinds of outputs as the WHOOP app (Recovery, Strain, Sleep, HRV,
resting HR, trends), compare the two devices, and eventually stop paying for both
subscriptions.

**Requirements and constraints, as I stated them**
1. Must **not be tied to the WHOOP subscription**. Long term it must work with no WHOOP membership.
2. The WHOOP membership is **in my wife's name**, not mine. WHOOP doesn't allow transferring
   memberships. She **no longer wears the WHOOP**; the strap was lying idle and I'm now the
   wearer. She **will authorize** API access on her account.
3. I already paid the ~$350 and have the strap. Strap model: **WHOOP MG**.
4. I have an **Oura Ring 4** with an **active Oura membership in my name**. I will cancel it
   only once our dashboard proves reliable.
5. **No backfill** of old history needed.
6. As close to **real time** as possible.
7. **Low cost.** Azure was not mandatory; cheaper and simpler options were preferred.
8. **iPhone as the collector is not a requirement.** A Raspberry Pi plus a laptop is fine.
   It must be seamless at home; for trips longer than 14 days, I'll sync with a laptop.
9. Remote access: **Tailscale** (locked in).
10. A **front end is required** and must be high quality, with the same outputs as the
    original WHOOP app. We do our own design, not a copy of WHOOP's look.
11. Oura on the same dashboard, with **comparisons**, and the ability to **choose which data
    to show or hide** across WHOOP/Oura and Official/Ours.
12. The current four-chip device/source filter is good. I rejected a fancier picker.
13. Avoid the existing Pi-hole Pi if it would hurt Pi-hole. Decision: buy a second Pi Zero 2 W.

---

## 2. Architecture

```
 WHOOP MG strap ──Bluetooth LE──▶ Pi Zero 2 W (bedroom, new)                  ┌──────────────┐
                                  loop-collector.service (Python + bleak)     │              │
                                  writes straight to SQLite ─────────────────▶│  SQLite file │
 (travel >14 days)                                                            │  data/       │
 WHOOP MG ──Bluetooth──▶ Laptop collector (same Python code, --upload URL)    │  loop.       │
                         POST /api/ingest over Tailscale (bearer token);      │  sqlite      │
                         spools to ~/.loop-spool/*.json if Pi unreachable ───▶│  (WAL mode)  │
                                                                              │              │
 WHOOP API v2 (wife's account, temporary baseline) ─┐ polled every 30 min     │              │
 Oura API v2 (my membership)                       ─┴─ by loop-server ───────▶│              │
                                                                              └──────┬───────┘
 loop-server.service (FastAPI + uvicorn, 127.0.0.1:8800, on the Pi)                │
   background loop: API sync 30 min · history decode + recompute our scores 10 min · │
   push alerts/brief (ntfy, optional) 10 min · auto-tune vs official 24 h ◀───────────┘
   serves web/app.html (dashboard) + JSON API
        │
        ▼  `tailscale serve --bg 8800` → https://loop.<tailnet>.ts.net (private, HTTPS)
 iPhone (Safari, Add to Home Screen) · laptop browser · Claude desktop app (mcp_server.py, stdio)

 Nightly 03:15: deploy/backup.sh → data/backups/loop-YYYY-MM-DD.sqlite.gz (keep 14)
                → optional rclone remote "offsite" (Google Drive or Backblaze B2 free 10 GB)
```

**Components**

| Component | File(s) | What it does |
|---|---|---|
| WHOOP MG collector | `loop/ble/whoop_mg.py`, `frames.py`, `protocol.py`, `sinks.py` | Connects over BLE, sends CLIENT_HELLO, subscribes to standard HR (0x2A37) for beat-level HR + R-R, subscribes to custom channels fd4b0003/4/5/7 and stores every raw notification, requests stored history every 30 min, acks only after data is written. Flushes every 15 s; reconnects every 20 s if the strap is out of range. |
| History decoder | `loop/ble/history.py` | Works out the stored-history record layout by matching records against live HR. Saves `data/history_layout.json`, then decodes history automatically (never overwrites live readings). |
| Storage | `loop/db.py` | SQLite (WAL). Tables: `hr_samples`, `hrv_samples`, `raw_frames`, `daily`, `workouts`, `journal`, `kv`, `sync_log`. |
| Official API sync | `loop/sources/oauth.py`, `whoop_api.py`, `oura_api.py` | OAuth 2.0 authorization code flow, tokens stored in `kv`, auto refresh, 429 back-off. |
| Our scores | `loop/analytics.py` | Sleep detection, RHR, HRV, breathing rate, stage estimate, Recovery, Strain, auto-tuning, agreement and cancel-readiness stats. |
| Insights | `loop/insights.py` | Journal patterns, alerts, morning brief, ntfy push. |
| Backend | `loop/server.py` | FastAPI app, background loop, JSON API, ingest endpoint, OAuth routes. |
| Front end | `web/app.html` (+ `manifest.webmanifest`, `icon.svg`) | Single-file dashboard, no build step, five tabs. |
| Claude connector | `loop/mcp_server.py` | Hand-written JSON-RPC MCP server over stdio, no dependencies, reads the Pi's API. |
| Hosting | `deploy/loop-server.service`, `deploy/loop-collector.service` | systemd units on the Pi, user `pi`, path `/home/pi/loop`, venv `.venv`. Memory caps: server 200M, collector 120M. |
| Remote access | Tailscale | `tailscale serve --bg 8800`. Nothing is exposed to the public internet. |
| Backup | `deploy/backup.sh` | Consistent SQLite snapshot via the backup API, gzip, keep 14, optional rclone. |
| Demo tooling | `loop/tools/seed_demo.py`, `build_demo.py` | 60 days of synthetic data; bundles a self-contained preview HTML. |

**Data flow detail**
- Live BLE readings: `hr_samples(device='whoop', ts, bpm, rr_ms JSON, motion)` at 1 row per second while in range.
- Raw frames: `raw_frames(device, received, channel ∈ {cmd, events, data, extra}, hex)`, kept forever so decoders can be re-run.
- Official API → `daily(device, source='official', day, …)` and `workouts`.
- Oura API raw-ish data → `hr_samples(device='oura')` (heartrate endpoint plus the sleep `heart_rate` series) and `hrv_samples(device='oura')` (sleep `hrv` series, 5-minute RMSSD).
- `analytics.recompute(device, days=3)` every 10 min writes `daily(device, source='ours', …)`.
- The dashboard reads `/api/overview` (all `daily` rows for 120 days plus workouts and status) and renders client-side.

---

## 3. Decisions, reasons, rejected alternatives

| # | Decision | Reason | Rejected alternatives |
|---|---|---|---|
| 1 | **Primary WHOOP path = direct Bluetooth from the strap** | Only route not tied to the WHOOP subscription; the API, app and cloud scores all stop when the membership ends. | Official WHOOP API as the main path (tied to the wife's membership; kept only as a temporary baseline). Cancelling and relying on Whoop's CSV export. |
| 2 | **Official WHOOP API kept as a temporary baseline** | Gives ground truth to tune and verify our scores during the trial. Wife authorizes once. | Skipping it (Oura alone could validate HRV/RHR/sleep, but not Strain/Recovery). |
| 3 | **Only import WHOOP official data from `WHOOP_IMPORT_FROM` (set to 2026-10-23)** | The account's older history is the wife's. | Importing everything. |
| 4 | **Everything self-hosted on a Raspberry Pi** | $0/month and the simplest setup; data stays at home. | Azure Container Apps next to Aresa (~$0–2/month, more moving parts; was the first plan). Cloudflare/Supabase free tiers (limits too tight for 1 Hz data, would need downsampling). An always-on cloud container (~$10–15/month). |
| 5 | **Tailscale for remote access** | Free, private, provides the HTTPS needed for OAuth redirects, and works from the iPhone. | Public hosting. Tailscale Funnel (only suggested later, for an optional Claude endpoint; not used). |
| 6 | **Polling instead of WHOOP webhooks** | Webhooks need a public URL; local hosting has none. Each sync re-reads the last 3 days because scores get revised. | WHOOP webhooks (sleep/recovery/workout only; no webhooks for cycles/strain anyway). |
| 7 | **New Pi Zero 2 W in the bedroom** | The existing Pi-hole Pi is a Zero 2 W on Wi-Fi only (no Ethernet port), and Wi-Fi and Bluetooth share one chip, so syncs could slow DNS for the whole house. It sits near the router, and bedroom placement gives hours of overnight range. | Reusing the Pi-hole Pi as is. Adding a micro-USB Ethernet adapter (~$10–15) to the Pi-hole Pi. |
| 8 | **Laptop collector for trips longer than 14 days** | The strap stores about 14 days, then overwrites the oldest data. | iPhone as collector (Safari has no Web Bluetooth; sideloading needs Xcode with a 7-day expiry on a free Apple ID or a $99/yr developer account; iOS limits background work). |
| 9 | **Laptop collector built as the same Python CLI with `--upload` + disk spooling** | One codebase; works offline, uploads later. | The earlier plan of a Web Bluetooth "Sync" button in the web app (Chrome on laptop). **Not built.** |
| 10 | **Our own Python BLE implementation, written from community protocol documentation** | No usable Python library; keeps licensing clean. Protocol knowledge from b-nnett/goose, NOOP, dofek; no code copied. | Running NOOP itself (an app; Mac/Android builds; iPhone needs Xcode sideloading). |
| 11 | **Safety allow-list for strap commands** | Never risk bricking or erasing the strap. Only commands 3, 14, 22, 23, 26 can be sent. | Sending arbitrary commands. |
| 12 | **Store every raw BLE frame** | MG decoding is incomplete; old data can be re-decoded later. | Storing only decoded values. |
| 13 | **SQLite (WAL) single file** | Light enough for a Pi Zero, trivially backed up. Roughly 1.5 GB/year at 1 Hz, so a 32 GB card lasts over a decade. | Managed database (~$15+/month). |
| 14 | **Oura · Ours computed from Oura API data (interim)** | Ring 4 Bluetooth isn't reliably decodable yet (NOOP: Gen3 measured, Ring 5 reported, Ring 4 not confirmed; the ring may need a pairing key from the Oura app, which is unverified). Lets us test our formulas on ring data now. | Waiting for Ring 4 Bluetooth. Leaving Oura · Ours empty. |
| 15 | **Oura can't show "Ready to cancel" until Ring 4 Bluetooth exists** | Oura · Ours still depends on the API, which needs the membership. | Letting it pass on API-derived agreement alone. |
| 16 | **Validate our WHOOP scores against Oura as well as against WHOOP** | WHOOP's own Recovery/HRV baseline is skewed for ~3 weeks after the wearer changes. | WHOOP official only. |
| 17 | **Cancel-readiness rule:** 30 paired nights, at least 80% of nights within tolerance on at least 3 checks | A clear, measurable bar. Tolerances: HRV ±15%, RHR ±3 bpm, sleep ±30 min (plus HRV vs Oura for WHOOP). | Subjective judgement. |
| 18 | **Device/source filter = four chips: WHOOP, Oura, Ours, Official** (remembered in localStorage) | I said the current picker is good. | A 2×2 matrix picker with presets (started, then reverted at my request). |
| 19 | **Own visual design** | High quality with WHOOP-equivalent outputs, without imitating WHOOP's look. | Copying WHOOP's UI. |
| 20 | **Auto-tuning against official scores, kept after cancelling** | Makes our Recovery/Strain track the official ones; the fit survives the subscription. | Fixed formulas. |
| 21 | **Optional push notifications via ntfy** | Free, no account; works with the iPhone app. | No notifications; a paid push service. |
| 22 | **Claude connector as a hand-rolled stdio MCP server** | The `mcp` Python package 2.x renamed FastMCP and changed its APIs; a dependency-free implementation is more robust. | Using the `mcp` package. |
| 23 | **No CSV backfill** | Not needed (my words). Still export history from each official app before cancelling it. | Backfilling from exports. |
| 24 | **Live heart rate on the dashboard** | Real-time requirement; it comes from the BLE heart-rate stream when in range. | API-only data (scored summaries only, minutes behind). |

---

## 4. Data sources

### 4a. WHOOP MG over Bluetooth (primary, subscription-free)

**GATT** (from community reverse engineering; see `protocol.py`)
- Custom service `fd4b0001-cce1-4033-93ce-002d5875f58a`
- Write commands: `fd4b0002-…` · notify: `fd4b0003-…` (cmd responses), `fd4b0004-…` (events), `fd4b0005-…` (data), `fd4b0007-…` (extra)
- Standard Heart Rate service `0x180D`, measurement `0x2A37` (beat-level HR + R-R)
- Battery level `0x2A19`

**Frame format ("puffin", WHOOP 5.0/MG):**
`0xAA 0x01 | length u16 LE (=len(payload)+4) | role1 role2 (00 01) | CRC16-Modbus of bytes 0..5 (LE) | payload | CRC32 (zlib/IEEE) of payload (LE)`.
Payload for commands: `[packet_type=35][sequence][command][data…]`.

**CLIENT_HELLO** (static, also triggers bonding): `aa0108000001e67123019101363e5c8d`. Verified: our `build_frame` reproduces it byte for byte (test).

**Packet types:** 35 command, 36 command response, 38 puffin command response, 40 realtime, 43 realtime raw, 47 historical, 48 event, 49 metadata, 56 puffin metadata.

**Commands** (marked VERIFY: reported by other projects, not yet confirmed on this strap): 3 toggle realtime HR, 14 toggle generic HR profile (turns on 0x2A37), 22 send historical data, 23 historical data result (ack with trim cursor; payload `struct.pack("<BII", 1, trim, 0)`), 26 get battery. Metadata sub-types (VERIFY): 1 history start, 2 history end, 3 history complete. Trim cursor offset inside a HISTORY_END record: `META_TRIM_OFFSET = 10` (VERIFY).

**Session flow:** connect, write CLIENT_HELLO (with response), subscribe to custom channels, send cmd 14 with data `0x01`, subscribe to 0x2A37, then loop: request history every 30 min; on HISTORY_END, flush to storage first, then ack. Flush every 15 s. Reconnect every 20 s.

**Metrics we get:** HR (bpm) and R-R intervals while in range (works today). From these we compute HRV (RMSSD), resting HR, sleep timing, a stage estimate, breathing rate, Recovery and Strain. Stored history (14 days) is pulled raw; decoding its layout is pending real data. **Not decoded for MG:** SpO₂, skin temperature (breathing rate is now computed by us instead).

### 4b. WHOOP official API v2 (temporary baseline, wife's account)
- Authorize: `https://api.prod.whoop.com/oauth/oauth2/auth` · Token: `https://api.prod.whoop.com/oauth/oauth2/token`
- Scopes: `offline read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement` (the refresh request also sends `scope=offline`)
- Base: `https://api.prod.whoop.com/developer`
- Endpoints: `/v2/activity/sleep`, `/v2/recovery`, `/v2/cycle`, `/v2/activity/workout`; params `limit=25`, `start=<ISO>`, `nextToken`; response `records` + `next_token`
- Redirect URI: `{LOOP_BASE_URL}/auth/whoop/callback`
- Sync: every 30 min (and on "Sync now"); window = max(`WHOOP_IMPORT_FROM`, last cursor − 3 days)
- Day assignment: sleep → local date of sleep end; recovery → via `sleep_id`; cycle → local date of (cycle start + 6 h)
- Metrics: recovery_score, resting_heart_rate, hrv_rmssd_milli, spo2_percentage, skin_temp_celsius, user_calibrating; sleep stage times (light, slow-wave = deep, REM, awake, in bed), sleep performance %, efficiency %, respiratory rate, consistency, disturbances; cycle strain, kilojoule → kcal (÷4.184), step_count; workouts (sport, strain, avg/max HR, kcal)
- Developer app: register at `https://developer-dashboard.whoop.com` (signed in as the wife's account)

### 4c. Oura official API v2 (my membership)
- Authorize: `https://cloud.ouraring.com/oauth/authorize` · Token: `https://api.ouraring.com/oauth/token`. OAuth only; personal access tokens were deprecated in late 2025.
- Scopes: `email personal daily heartrate workout session spo2Daily`
- Base: `https://api.ouraring.com/v2/usercollection`
- Endpoints: `daily_readiness`, `sleep`, `daily_sleep`, `daily_activity`, `daily_spo2` (params `start_date`, `end_date`, `next_token`), and `heartrate` (params `start_datetime`, `end_datetime`)
- Redirect URI: `{LOOP_BASE_URL}/auth/oura/callback`
- Sync: every 30 min; window = max(`OURA_IMPORT_FROM`=2026-01-01, last cursor − 3 days) to tomorrow
- Metrics: readiness score → `recovery`, temperature_deviation → `skin_temp`; long_sleep: total/deep/REM/light/awake durations, time in bed, efficiency, average_hrv, lowest_heart_rate → `rhr`, average_breath → `resp_rate`, bedtime start/end; sleep score; activity score, steps, total calories; SpO₂ average
- For Oura · Ours: heart-rate samples (about 1 per 5 minutes during the day, plus the sleep `heart_rate` 5-min series) and the sleep `hrv` 5-min RMSSD series. **No beat-to-beat data exists in Oura's API.**
- Developer app: `https://cloud.ouraring.com/oauth/applications`

### 4d. Oura Ring 4 over Bluetooth: not built (not reliably possible yet).

---

## 5. Dashboard design (built in `web/app.html`)

**Look:** phone-first instrument panel with a sticky filter bar on top, a bottom tab bar, and
two columns on desktop (≥860 px). Light and dark themes. Fonts: Bricolage Grotesque
(display), Instrument Sans (body), JetBrains Mono (numbers). Device colors (validated for
colour-blind separation): WHOOP `#B8740F` light / `#C4851F` dark, Oura `#6A55D8` light /
`#8270E6` dark. Official = dashed line, Ours = solid. Status colours: good `#1F8F66`, warn
`#A9781A`, bad `#C8463A` (light theme). Installable as a home-screen app (manifest + icon).

**Global filter:** chips WHOOP, Oura, Ours, Official → up to four series:
WHOOP · Ours, WHOOP · Official, Oura · Ours, Oura · Official. Remembered per browser.

**Tabs**
1. **Today**
   - Alerts banner (latest day only)
   - Day picker (‹ ›)
   - Recovery tier pill: High ≥67, Moderate ≥34, Low <34
   - Morning brief
   - Three concentric-ring gauges (Recovery /100, Strain /21, Sleep % of need), one ring per visible series
   - Vitals table: HRV, resting HR, sleep, breathing rate, SpO₂, skin temperature, with the change vs your 30-day average; "not yet" for undecoded metrics
   - Live heart rate (polls `/api/live` every 5 s; pulsing dot; 90 s chart)
   - "Night and day" (sleep time, bed → wake, workouts)
   - Journal tag chips plus custom tags
2. **Sleep:** totals per series, a hypnogram (estimated stages in 5-min blocks with the overnight HR trace, WHOOP · Ours), stage breakdown stacked bars, timing table (bedtime, wake, in bed).
3. **Trends:** metric selector (Recovery, HRV, RHR, Sleep, Strain, Breathing rate, SpO₂), 7/30/90-day line chart with crosshair tooltip, averages table (last 7 days vs previous 7), and "What changes your next night" (journal tag effects with clear / possible / no clear effect).
4. **Compare:**
   - Pair selector: Our WHOOP vs WHOOP official; Our WHOOP vs Oura official; WHOOP official vs Oura official; Our Oura vs Oura official; Our formulas: WHOOP vs Oura.
   - Metric tiles (HRV, RHR, Sleep, Recovery, Strain, Breathing, Bedtime, Wake), each showing % of nights within tolerance, average difference and correlation r.
   - Night-by-night scatter with an agreement line.
   - "Ready to cancel?" panel per membership: progress toward 30 nights plus a checklist.
5. **Devices:** per device, in-range status, last reading, minutes covered in the last 24 h, official API connect/disconnect, last sync, "Our scores tuned on N nights ± X recovery pts", decoding notes, and a Sync now button.

**Tolerances used in Compare:** HRV ±15%, RHR ±3 bpm, breathing ±1 rpm, sleep ±30 min,
Recovery ±15 pts, Strain ±2.5, bedtime/wake ±30 min.

---

## 6. Current status (as of 2026-11-10; no work since 2026-10-23)

**Built and working (verified with 11 automated tests and on 60 days of synthetic data only, no real data yet)**
- BLE framing, CRCs and fragment reassembly; CLIENT_HELLO reproduced byte for byte
- Collector program (scan / run / run --upload / --no-history), upload spooling, command allow-list
- SQLite schema and storage
- WHOOP and Oura OAuth and sync code (field mapping tested against sample API payloads)
- Our scoring: sleep detection, RHR, HRV, breathing rate, stage estimate, Recovery, Strain
- Auto-tuning. On sample data, nights where our Recovery was within 15 points of WHOOP's went from 22% to 86%; breathing rate was within 1 rpm 98% of the time.
- Agreement statistics and cancel-readiness logic
- Oura · Ours from API data
- Journal, patterns, alerts, morning brief, ntfy push
- FastAPI server and all endpoints
- Full dashboard (5 tabs), with the preview published
- Claude MCP connector (tested against the local sample server)
- systemd units, backup script, README

**Partly done / unverified**
- **BLE collector has never touched a real strap.** Commands 14/22/23, metadata sub-types, trim offset and ack format are unverified on the MG.
- **Stored-history decoding:** the layout finder works on synthetic records but needs a few days of real raw frames plus live HR. Until it's solved, our daily Strain only covers time spent near the Pi or laptop.
- **Sleep stages** are a heart-rate-only heuristic, labelled "est."
- **Oura · Ours HRV** summarizes Oura's own 5-min readings; Oura · Ours Strain is rough (sparse daytime HR).
- **Auto-tuning** needs 14 non-calibrating paired nights (Recovery) and 10 well-covered days (Strain).

**Not started**
- Deployment anywhere (laptop mode or Pi)
- Creating the WHOOP and Oura developer apps and connecting them
- Tailscale setup
- Oura Ring 4 Bluetooth collector
- MG SpO₂ / skin temperature decoding
- Web Bluetooth "Sync" button for the laptop (replaced by the Python CLI)
- Git repository
- rclone offsite backup
- ntfy topic

---

## 7. All code, configs, schemas and commands

The full source of every file follows after Part 10, under "Part 7 (continued)". Key commands:

```bash
# set up (laptop or Pi)
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # fastapi uvicorn httpx bleak
pip install pytest                       # tests only
python -m pytest -q tests                # 11 tests

# demo with sample data
LOOP_DB=data/demo.sqlite python -m loop.tools.seed_demo
LOOP_DB=data/demo.sqlite uvicorn loop.server:app --port 8800      # http://localhost:8800
LOOP_DB=data/demo.sqlite python -m loop.tools.build_demo loop-preview.html

# real server
cp .env.example .env                     # fill in
uvicorn loop.server:app --host 127.0.0.1 --port 8800

# strap
python -m loop.ble.whoop_mg scan --seconds 20
python -m loop.ble.whoop_mg run                       # Pi: stores locally
python -m loop.ble.whoop_mg run --no-history          # live HR only (safest first test)
python -m loop.ble.whoop_mg run --upload https://loop.<tailnet>.ts.net   # laptop
LOGLEVEL=DEBUG python -m loop.ble.whoop_mg run

# history decoding (after a few days of real data)
python -m loop.ble.history probe
python -m loop.ble.history probe --save
python -m loop.ble.history redecode

# Pi
sudo apt update && sudo apt install -y python3-venv bluez git
sudo usermod -aG bluetooth pi
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
sudo tailscale serve --bg 8800 && tailscale serve status
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl enable --now loop-server loop-collector
journalctl -u loop-collector -f
chmod +x deploy/backup.sh   # crontab: 15 3 * * * /home/pi/loop/deploy/backup.sh

# ingest token
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**HTTP API** (server on port 8800)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/manifest.webmanifest`, `/static/*` | PWA files |
| GET | `/api/overview?days=120` | `daily` rows, workouts, status, profile |
| GET | `/api/night?device=&day=` | Hypnogram + 5-min HR curve |
| GET | `/api/live` | Last 90 s of HR per device |
| GET | `/api/compare?days=60` | Agreement stats for 5 pairs × 8 metrics |
| GET | `/api/readiness` | Cancel-readiness per device |
| GET | `/api/status` | BLE coverage, API connection, recent syncs, calibration |
| POST | `/api/sync` | Sync APIs now + recompute |
| GET/POST | `/api/journal` | List / set tag `{day, tag, on}` |
| GET | `/api/patterns` | Tag effects |
| GET | `/api/alerts` | Current alerts |
| GET | `/api/summary?day=` | Morning brief |
| POST | `/api/ingest` | Laptop upload, header `Authorization: Bearer <LOOP_INGEST_TOKEN>` |
| GET | `/auth/{whoop\|oura}/start`, `/auth/{…}/callback` | OAuth |
| POST | `/api/disconnect/{name}` | Remove a token |

**Background timings:** API sync 30 min, recompute and history decode 10 min, auto-tune 24 h,
loop tick 30 s.

**Scoring constants:**
- Night window: 18:00 the previous evening to 14:00. Needs at least 180 min of coverage and an asleep block of at least 2 h. Gaps under 30 min are bridged.
- Sleep threshold = p10 + 0.4·(p80 − p10) of the 7-min rolling-median HR.
- Default Recovery = logistic of 0.65·z_hrv − 0.35·z_rhr + 2.0·(sleep_ratio − 0.9), against a 30-night baseline; "calibrating" for the first 7 nights.
- Strain = 21·(1 − e^(−TRIMP/k)), default k = 122.
- Max HR: `LOOP_MAX_HR`, else 208 − 0.7·age (Tanaka).
- Tuning: ridge regression with λ = 3, blended in by min(1, n/30).

---

## 8. Hardware and accounts

| Item | Status |
|---|---|
| WHOOP MG strap | **Owned.** Registered to my wife's WHOOP account and membership; she no longer wears it, and I am the wearer. |
| Oura Ring 4 | **Owned**, with an active Oura membership in my name. |
| Existing Raspberry Pi Zero 2 W | **Owned.** Runs Pi-hole on Wi-Fi near the router. **Not used** for this project. |
| New Raspberry Pi Zero 2 W (plain, not "WH") | **To buy** (~$15). Decided on 2026-10-23; whether it has been bought is unknown. |
| MicroSD 32 GB, SanDisk or Samsung, A1/A2 | To buy (~$8) |
| Official Raspberry Pi micro-USB power supply 5V 2.5A | To buy (~$8) |
| Case (optional, official Zero case) | To buy (~$5) |
| Total one-time | **≈ $35** (a CanaKit Zero 2 W kit is an alternative). Buy from Adafruit, PiShop.us, CanaKit or Micro Center; avoid marked-up marketplace sellers. |
| Laptop (travel collector, laptop mode) | Owned; OS not recorded. |
| iPhone | Owned; viewer only via Tailscale + Safari "Add to Home Screen". |
| Placement | New Pi in the **bedroom** (Bluetooth range about 10 m, reduced by walls). |

**Accounts to set up**
1. **WHOOP developer app**: developer-dashboard.whoop.com, signed in as the wife's account. Redirect URIs: `http://localhost:8800/auth/whoop/callback` (laptop mode) and `https://loop.<tailnet>.ts.net/auth/whoop/callback` (Pi). Copy the client ID/secret into `.env`. She approves on the Connect screen.
2. **WHOOP profile**: wife updates the account's height, weight and age to mine (her call).
3. **Oura developer app**: cloud.ouraring.com/oauth/applications (my account), with the same two redirect URIs but `/auth/oura/callback`.
4. **Tailscale**: free personal plan. Install on the Pi, laptop and iPhone with the same account. Pi hostname `loop`.
5. **ntfy** (optional): the app plus a long random topic → `NTFY_URL=https://ntfy.sh/<topic>`.
6. **rclone** (optional): remote named `offsite` (Google Drive or Backblaze B2).
7. **Claude desktop app**: add the `loop` MCP server config (README).

**Costs:** hardware ≈ $35 one-time; $0/month. WHOOP and Oura memberships continue until each passes the readiness test.

---

## 9. Known problems, risks, open questions

**Risks**
1. **Unverified BLE commands.** If command numbers, the metadata sub-type or the trim offset are wrong on the MG, history requests may do nothing, or a wrong ack could make the strap discard un-offloaded data. **Mitigation:** first run with `--no-history` (live HR only), check the logs, and enable history only after confirming the frames look right. Raw frames are always stored.
2. **WHOOP firmware updates** could break the protocol. Don't install them once relying on the Pi.
3. **One BLE connection at a time.** The WHOOP phone app and the collector will fight over the strap; close or disable the WHOOP app (or Bluetooth on the phone for it) while collecting. Switching between Pi and laptop may need a one-time re-pair.
4. **Daytime coverage.** Until stored history is decoded, we only see HR while near the Pi or laptop, so Strain is partial.
5. **WHOOP terms** may expect the account holder to be the wearer; it's my wife's account, so her call. Her membership end date is the cutoff for the WHOOP baseline.
6. **WHOOP's baseline recalibration** takes about 3 weeks for a new wearer. Its Recovery is excluded from tuning while it reports calibrating.
7. **Oura Ring 4 Bluetooth** may never become practical (the ring may require a key set up during pairing with the Oura app; unverified). If so, keep the Oura membership, or drop Oura.
8. **SD card failure**: nightly backup, and optionally offsite.
9. **Container loss**: the code lives only in the zip and in this document. Put it in a private Git repository.
10. **The ntfy topic** carries summary numbers; use a long random topic or a self-hosted server.
11. **Open-source scores are approximations**, not WHOOP's or Oura's algorithms.

**Open questions**
- My age, max HR and sleep need. `.env` defaults are `LOOP_AGE=45` (placeholder, not my real age), `LOOP_MAX_HR=0` (estimate), `LOOP_SLEEP_NEED_HOURS=8`.
- The exact first day I wore the strap. `WHOOP_IMPORT_FROM` defaults to 2026-10-23.
- When the wife's WHOOP membership ends.
- Has the new Pi Zero 2 W been bought?
- Laptop OS (bleak supports macOS, Windows and Linux; the systemd units are Pi-only).
- Does the MG need "Heart Rate Broadcast" switched on in the WHOOP app for 0x2A37, or does command 14 enable it?
- Strap firmware version.

---

## 10. Next steps, in order

1. **Save the code somewhere durable:** create a private GitHub repo (e.g. `sbalajiiyer/loop`) from `loop.zip` or from Part 7 of this document. Don't commit `.env` or `data/`.
2. **Laptop mode (starts the 30-night clock):**
   - Create the Oura developer app and the WHOOP developer app (wife signs in), with the localhost redirect URIs.
   - `cp .env.example .env`, then set `LOOP_BASE_URL=http://localhost:8800`, the client IDs/secrets, `WHOOP_IMPORT_FROM` = first day worn, and my age and sleep need.
   - `uvicorn loop.server:app --port 8800`, open Devices → Connect Oura, then Connect WHOOP (wife approves).
   - Wife updates the WHOOP profile to my height, weight and age.
   - Run the server daily; each sync catches up 3 days.
3. **First strap test on the laptop:** close the WHOOP phone app, run `python -m loop.ble.whoop_mg scan`, set `WHOOP_ADDRESS`, then `run --no-history`. Confirm live HR appears on Today. Then try history with `LOGLEVEL=DEBUG` and check the logs.
4. **Buy the Pi kit** (≈ $35) if not already done.
5. **Set up the Pi** (README "Set up the Pi"): flash Raspberry Pi OS Lite 64-bit with hostname `loop`, install packages, Tailscale, `tailscale serve --bg 8800`, pair the strap, systemd services, backup cron, then Add to Home Screen on the iPhone.
6. **Move from laptop to Pi:** copy `data/loop.sqlite` and `.env`, change `LOOP_BASE_URL` to the Tailscale address, and add the Tailscale redirect URIs in both developer dashboards. Generate `LOOP_INGEST_TOKEN` and set it on the Pi and the laptop.
7. **After a few days of real data:** `python -m loop.ble.history probe`, then `--save` when it reports a strong match, then `redecode`.
8. **Optional:** ntfy notifications, rclone offsite backup, Claude desktop connector.
9. **After 14 nights:** check auto-tuning on Devices ("tuned on N nights"). After 30 nights, check Compare → "Ready to cancel?".
10. **When WHOOP shows Ready:** export WHOOP history from the app, then the wife cancels. Oura stays until Ring 4 Bluetooth is possible.
11. **Later:** Oura Ring 4 Bluetooth collector when community support is reliable; MG SpO₂ and skin-temperature decoding; optional Web Bluetooth sync button.

---

# Part 7 (continued): full source of every file

Project layout:

```
loop/
  .env.example
  README.md
  requirements.txt
  deploy/backup.sh
  deploy/loop-collector.service
  deploy/loop-server.service
  tests/conftest.py  test_analytics.py  test_frames.py  test_history.py  test_insights.py  test_sources.py  test_workouts.py
  loop/__init__.py  config.py  db.py  analytics.py  insights.py  server.py  mcp_server.py
  loop/ble/__init__.py  frames.py  protocol.py  whoop_mg.py  sinks.py  history.py
  loop/sources/__init__.py  oauth.py  whoop_api.py  oura_api.py
  loop/tools/__init__.py  seed_demo.py  build_demo.py
  web/app.html  manifest.webmanifest  icon.svg
```

The SQLite schema is the `SCHEMA` string in `loop/db.py`. The settings file template is
`.env.example`.

## `.env.example`

```bash
# Copy to .env and fill in. Never commit .env.

# Your Pi's Tailscale HTTPS address (shown by `tailscale serve status`)
LOOP_BASE_URL=https://loop.your-tailnet.ts.net
LOOP_TZ=America/Detroit

# Long random string; the laptop collector uses it to upload to the Pi.
# Generate with:  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
LOOP_INGEST_TOKEN=

# Used by our own strain and sleep scores
LOOP_AGE=45
LOOP_MAX_HR=0
LOOP_SLEEP_NEED_HOURS=8

# WHOOP strap Bluetooth address (from: python -m loop.ble.whoop_mg scan)
WHOOP_ADDRESS=

# WHOOP official API (developer-dashboard.whoop.com). Redirect URL to register:
#   https://loop.your-tailnet.ts.net/auth/whoop/callback
WHOOP_CLIENT_ID=
WHOOP_CLIENT_SECRET=
# First day you wore the strap; earlier data on the account is not imported.
WHOOP_IMPORT_FROM=2026-10-23

# Oura official API (cloud.ouraring.com/oauth/applications). Redirect URL to register:
#   https://loop.your-tailnet.ts.net/auth/oura/callback
OURA_CLIENT_ID=
OURA_CLIENT_SECRET=
OURA_IMPORT_FROM=2026-01-01

# Optional phone notifications via ntfy (install the ntfy app, subscribe to the same topic).
# Use a long random topic name, or your own ntfy server. Messages contain summary numbers.
NTFY_URL=
BRIEF_HOUR=8
```

## `requirements.txt`

```text
fastapi>=0.110
uvicorn>=0.29
httpx>=0.27
bleak>=0.22
```

## `deploy/loop-server.service`

```ini
[Unit]
Description=Loop dashboard server
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/loop
ExecStart=/home/pi/loop/.venv/bin/uvicorn loop.server:app --host 127.0.0.1 --port 8800 --workers 1
Restart=always
RestartSec=5
MemoryMax=200M

[Install]
WantedBy=multi-user.target
```

## `deploy/loop-collector.service`

```ini
[Unit]
Description=Loop WHOOP Bluetooth collector
After=bluetooth.target
Requires=bluetooth.target

[Service]
User=pi
WorkingDirectory=/home/pi/loop
ExecStart=/home/pi/loop/.venv/bin/python -m loop.ble.whoop_mg run
Restart=always
RestartSec=10
MemoryMax=120M

[Install]
WantedBy=multi-user.target
```

## `deploy/backup.sh`

```bash
#!/usr/bin/env bash
# Nightly backup: consistent SQLite snapshot, keep 14 locally, copy offsite if rclone is set up.
# Cron (crontab -e):  15 3 * * * /home/pi/loop/deploy/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/backups
snap="data/backups/loop-$(date +%F).sqlite"
python3 - "$snap" <<'EOF'
import sqlite3, sys
src = sqlite3.connect("data/loop.sqlite")
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
dst.close(); src.close()
EOF
gzip -f "$snap"
ls -1t data/backups/*.gz | tail -n +15 | xargs -r rm --
# Offsite: run `rclone config` once and name the remote "offsite" (Google Drive or Backblaze B2).
if command -v rclone >/dev/null && rclone listremotes | grep -q '^offsite:'; then
  rclone copy data/backups offsite:loop-backups --max-age 48h
fi
```

## `loop/__init__.py`

```python
"""Loop: a self-hosted dashboard for WHOOP and Oura data."""
```

## `loop/config.py`

```python
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


DB_PATH = Path(get("LOOP_DB", str(ROOT / "data" / "loop.sqlite")))
# Public base URL of the dashboard (your Tailscale HTTPS name), used for OAuth redirects.
BASE_URL = get("LOOP_BASE_URL", "http://localhost:8800").rstrip("/")
# Shared secret the laptop collector uses when uploading to the Pi.
INGEST_TOKEN = get("LOOP_INGEST_TOKEN", "")
TIMEZONE = get("LOOP_TZ", "America/Detroit")

# Personal profile used by our own scores.
MAX_HR = int(get("LOOP_MAX_HR", "0") or 0)        # 0 = estimate from age
AGE = int(get("LOOP_AGE", "45") or 45)
SLEEP_NEED_HOURS = float(get("LOOP_SLEEP_NEED_HOURS", "8") or 8)

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
```

## `loop/db.py`

```python
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
```

## `loop/ble/__init__.py` (empty file)

```python

```

## `loop/ble/protocol.py`

```python
"""WHOOP 5.0 / MG GATT layout and command numbers.

Everything here comes from community reverse engineering, not WHOOP. Values marked
VERIFY have been reported to work by other projects but are not yet confirmed on
your strap; the collector stores every raw frame so nothing is lost if one is off.
"""

# Custom service and characteristics (fd4b0001 family)
SERVICE = "fd4b0001-cce1-4033-93ce-002d5875f58a"
CHAR_CMD_TO_STRAP = "fd4b0002-cce1-4033-93ce-002d5875f58a"
CHAR_CMD_FROM_STRAP = "fd4b0003-cce1-4033-93ce-002d5875f58a"
CHAR_EVENTS = "fd4b0004-cce1-4033-93ce-002d5875f58a"
CHAR_DATA = "fd4b0005-cce1-4033-93ce-002d5875f58a"
CHAR_EXTRA = "fd4b0007-cce1-4033-93ce-002d5875f58a"
NOTIFY_CHARS = {
    "cmd": CHAR_CMD_FROM_STRAP,
    "events": CHAR_EVENTS,
    "data": CHAR_DATA,
    "extra": CHAR_EXTRA,
}

# Standard Bluetooth services (reliable ground truth)
HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

# Static session-start frame (type 35 command 0x91), confirmed byte-for-byte by
# several projects; writing it with response also triggers bonding.
CLIENT_HELLO = bytes.fromhex("aa0108000001e67123019101363e5c8d")

# Packet types (first payload byte)
PT_COMMAND = 35
PT_COMMAND_RESPONSE = 36
PT_PUFFIN_COMMAND_RESPONSE = 38
PT_REALTIME = 40          # ~1 Hz live record: HR, R-R, timestamp
PT_REALTIME_RAW = 43
PT_HISTORICAL = 47        # stored history records
PT_EVENT = 48
PT_METADATA = 49          # history start / end / complete markers
PT_PUFFIN_METADATA = 56

# Command numbers (shared with 4.0, re-framed on the puffin transport). VERIFY
CMD_TOGGLE_REALTIME_HR = 3
CMD_TOGGLE_GENERIC_HR_PROFILE = 14   # turns on standard 0x2A37 broadcast
CMD_SEND_HISTORICAL_DATA = 22
CMD_HISTORICAL_DATA_RESULT = 23      # ack with trim cursor
CMD_GET_BATTERY_LEVEL = 26

# Only these commands may ever be sent. Nothing that reboots, resets, changes
# firmware or erases data is allowed through.
ALLOWED_COMMANDS = {
    CMD_TOGGLE_REALTIME_HR,
    CMD_TOGGLE_GENERIC_HR_PROFILE,
    CMD_SEND_HISTORICAL_DATA,
    CMD_HISTORICAL_DATA_RESULT,
    CMD_GET_BATTERY_LEVEL,
}

# Metadata sub-types inside PT_METADATA / PT_PUFFIN_METADATA. VERIFY
META_HISTORY_START = 1
META_HISTORY_END = 2
META_HISTORY_COMPLETE = 3
```

## `loop/ble/frames.py`

```python
"""WHOOP 5.0 / MG ("puffin") frame envelope.

Layout, from community reverse engineering (b-nnett/goose, NOOP, dofek) and
checked against the published CLIENT_HELLO test vector in tests/:

    0      1      2..3        4      5      6..7         8..N-4    N-4..N
    0xAA   0x01   length(LE)  role1  role2  crc16(LE)    payload   crc32(LE)

    length = len(payload) + 4
    crc16  = CRC16-Modbus over bytes 0..5
    crc32  = CRC32 (zlib / IEEE) over payload

Payload (commands):  [packet_type][sequence][command][data...]
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SOF = 0xAA
VERSION = 0x01
HEADER_LEN = 8


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def build_frame(payload: bytes, role1: int = 0x00, role2: int = 0x01) -> bytes:
    head = struct.pack("<BBHBB", SOF, VERSION, len(payload) + 4, role1, role2)
    return head + struct.pack("<H", crc16_modbus(head)) + payload + struct.pack("<I", crc32(payload))


@dataclass
class Frame:
    role1: int
    role2: int
    payload: bytes

    @property
    def packet_type(self) -> int:
        return self.payload[0] if self.payload else -1


class FrameAssembler:
    """Reassembles MTU-sized notification fragments into CRC-valid frames.

    Bytes that don't form a valid frame are skipped one at a time until the
    stream re-synchronises, so a dropped fragment costs one frame, not the session.
    """

    MAX_LEN = 4096

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad_frames = 0

    def feed(self, chunk: bytes) -> list[Frame]:
        self.buf.extend(chunk)
        out: list[Frame] = []
        while True:
            start = self.buf.find(bytes([SOF, VERSION]))
            if start < 0:
                # keep a trailing 0xAA in case its 0x01 arrives next
                self.buf = self.buf[-1:] if self.buf[-1:] == bytes([SOF]) else bytearray()
                return out
            if start:
                del self.buf[:start]
            if len(self.buf) < HEADER_LEN:
                return out
            length, role1, role2, hcrc = struct.unpack_from("<HBBH", self.buf, 2)
            if crc16_modbus(bytes(self.buf[:6])) != hcrc or not (4 <= length <= self.MAX_LEN):
                self.bad_frames += 1
                del self.buf[:1]
                continue
            total = HEADER_LEN + length
            if len(self.buf) < total:
                return out
            payload = bytes(self.buf[HEADER_LEN:total - 4])
            (pcrc,) = struct.unpack_from("<I", self.buf, total - 4)
            if crc32(payload) != pcrc:
                self.bad_frames += 1
                del self.buf[:1]
                continue
            out.append(Frame(role1, role2, payload))
            del self.buf[:total]


def parse_standard_hr(data: bytes) -> tuple[int, list[float]]:
    """Bluetooth SIG Heart Rate Measurement (0x2A37): returns (bpm, rr_ms list)."""
    flags = data[0]
    i = 1
    if flags & 0x01:
        bpm = struct.unpack_from("<H", data, i)[0]
        i += 2
    else:
        bpm = data[i]
        i += 1
    if flags & 0x08:  # energy expended present
        i += 2
    rr = []
    if flags & 0x10:
        while i + 1 < len(data):
            rr.append(round(struct.unpack_from("<H", data, i)[0] * 1000 / 1024, 1))
            i += 2
    return bpm, rr
```

## `loop/ble/sinks.py`

```python
"""Where collected data goes: straight into the local database (on the Pi), or
uploaded over Tailscale to the Pi (when the collector runs on your laptop)."""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("loop.sink")


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
```

## `loop/ble/whoop_mg.py`

```python
"""Collector for a WHOOP 5.0 / MG strap, run on the Pi or a laptop.

What it does on every connection:
  1. Starts the session with the static CLIENT_HELLO frame (also bonds).
  2. Subscribes to the standard heart-rate profile (0x2A37): beat-level HR and
     R-R intervals while you're in range. This part is reliable today and is
     what overnight HRV, resting HR and sleep timing are computed from.
  3. Subscribes to the strap's custom channels and stores every frame verbatim.
  4. Asks the strap to offload its stored history (up to 14 days), and only
     acknowledges a batch after it has been written to storage, so the strap
     never discards data we haven't kept.

Usage:
  python -m loop.ble.whoop_mg scan                 # find your strap's address
  python -m loop.ble.whoop_mg run                  # on the Pi (writes locally)
  python -m loop.ble.whoop_mg run --upload https://loop.<tailnet>.ts.net
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import struct
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

from . import protocol as P
from .frames import FrameAssembler, build_frame, parse_standard_hr
from .history import decode as decode_history, load_layout
from .sinks import HttpSink, LocalSink

log = logging.getLogger("loop.whoop")
DEVICE = "whoop"
FLUSH_SECONDS = 15
HISTORY_EVERY_SECONDS = 30 * 60
RECONNECT_SECONDS = 20
META_TRIM_OFFSET = 10  # VERIFY: byte offset of the trim cursor inside a HISTORY_END record


class WhoopSession:
    def __init__(self, address: str, sink, want_history: bool = True) -> None:
        self.address = address
        self.sink = sink
        self.want_history = want_history
        self.asm = {name: FrameAssembler() for name in P.NOTIFY_CHARS}
        self.samples: dict[int, tuple] = {}
        self.frames: list[tuple] = []
        self.seq = 0
        self.client: BleakClient | None = None
        self.pending_ack: int | None = None
        self.last_history = 0.0
        self.history_records = 0
        self.history_samples: list[tuple] = []
        self.layout = load_layout()  # set once `python -m loop.ble.history probe --save` has run

    # ---- outgoing -------------------------------------------------------
    async def send(self, command: int, data: bytes = b"") -> None:
        if command not in P.ALLOWED_COMMANDS:
            raise ValueError(f"command {command} is not on the allow-list")
        self.seq = (self.seq + 1) & 0xFF
        frame = build_frame(bytes([P.PT_COMMAND, self.seq, command]) + data)
        await self.client.write_gatt_char(P.CHAR_CMD_TO_STRAP, frame, response=True)

    # ---- incoming -------------------------------------------------------
    def on_standard_hr(self, _char, data: bytearray) -> None:
        try:
            bpm, rr = parse_standard_hr(bytes(data))
        except Exception:  # noqa: BLE001
            return
        ts = int(time.time())
        prev = self.samples.get(ts)
        if prev and prev[2]:
            rr = list(prev[2]) + rr
        self.samples[ts] = (ts, bpm, rr or None, None)

    def make_custom_handler(self, name: str):
        def handler(_char, data: bytearray) -> None:
            now = int(time.time())
            self.frames.append((now, name, bytes(data).hex()))
            for frame in self.asm[name].feed(bytes(data)):
                self.on_frame(frame)
        return handler

    def on_frame(self, frame) -> None:
        pt = frame.packet_type
        if pt == P.PT_HISTORICAL:
            self.history_records += 1
            if self.layout and (s := decode_history(frame.payload, self.layout)):
                self.history_samples.append(s)
        elif pt in (P.PT_METADATA, P.PT_PUFFIN_METADATA) and len(frame.payload) > 2:
            meta = frame.payload[2]
            if meta == P.META_HISTORY_END and len(frame.payload) >= META_TRIM_OFFSET + 4:
                (trim,) = struct.unpack_from("<I", frame.payload, META_TRIM_OFFSET)
                self.pending_ack = trim  # acked in the main loop after flushing
            elif meta == P.META_HISTORY_COMPLETE:
                log.info("history offload complete (%d records this pass)", self.history_records)
                self.history_records = 0

    # ---- storage --------------------------------------------------------
    def flush(self) -> None:
        samples = sorted(self.samples.values())
        frames, self.frames = self.frames, []
        self.samples = {}
        if samples or frames:
            self.sink.write(DEVICE, samples, frames)
            log.info("stored %d HR seconds, %d raw frames", len(samples), len(frames))
        if self.history_samples:
            hist, self.history_samples = self.history_samples, []
            write_history = getattr(self.sink, "write_history", None)
            if write_history:
                write_history(DEVICE, hist)
            log.info("decoded %d history samples", len(hist))

    # ---- main loop ------------------------------------------------------
    async def run_once(self) -> None:
        async with BleakClient(self.address, timeout=30) as client:
            self.client = client
            log.info("connected to %s", self.address)
            await client.write_gatt_char(P.CHAR_CMD_TO_STRAP, P.CLIENT_HELLO, response=True)
            for name, uuid in P.NOTIFY_CHARS.items():
                try:
                    await client.start_notify(uuid, self.make_custom_handler(name))
                except Exception as e:  # noqa: BLE001
                    log.debug("no %s channel: %s", name, e)
            await self.send(P.CMD_TOGGLE_GENERIC_HR_PROFILE, b"\x01")
            await client.start_notify(P.HR_MEASUREMENT, self.on_standard_hr)

            last_flush = time.monotonic()
            while client.is_connected:
                await asyncio.sleep(1)
                if self.want_history and time.time() - self.last_history > HISTORY_EVERY_SECONDS:
                    self.last_history = time.time()
                    log.info("requesting stored history")
                    await self.send(P.CMD_SEND_HISTORICAL_DATA)
                if self.pending_ack is not None:
                    self.flush()                      # durable first...
                    trim, self.pending_ack = self.pending_ack, None
                    await self.send(P.CMD_HISTORICAL_DATA_RESULT, struct.pack("<BII", 1, trim, 0))  # ...then ack
                if time.monotonic() - last_flush > FLUSH_SECONDS:
                    self.flush()
                    last_flush = time.monotonic()
        self.flush()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
                log.info("disconnected")
            except Exception as e:  # noqa: BLE001
                log.info("strap not reachable (%s); retrying in %ss", type(e).__name__, RECONNECT_SECONDS)
            self.flush()
            await asyncio.sleep(RECONNECT_SECONDS)


async def scan(seconds: float) -> None:
    print(f"Scanning {seconds:.0f}s. Put the strap in pairing mode if nothing shows up.")
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    for dev, adv in found.values():
        uuids = [u.lower() for u in adv.service_uuids or []]
        if (dev.name and "whoop" in dev.name.lower()) or P.SERVICE in uuids:
            print(f"  WHOOP  {dev.address}  {dev.name}  rssi={adv.rssi}")
    print("Put the address in .env as WHOOP_ADDRESS=...")


def main() -> None:
    from .. import config
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--seconds", type=float, default=20)
    r = sub.add_parser("run")
    r.add_argument("--address", default=config.get("WHOOP_ADDRESS"))
    r.add_argument("--upload", help="Pi dashboard URL; when set, data is uploaded instead of stored locally")
    r.add_argument("--no-history", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")

    if args.cmd == "scan":
        asyncio.run(scan(args.seconds))
        return
    if not args.address:
        raise SystemExit("Set WHOOP_ADDRESS in .env (run the scan command first)")
    sink = (HttpSink(args.upload, config.INGEST_TOKEN, Path.home() / ".loop-spool")
            if args.upload else LocalSink())
    asyncio.run(WhoopSession(args.address, sink, not args.no_history).run_forever())


if __name__ == "__main__":
    main()
```

## `loop/ble/history.py`

```python
"""Working out the WHOOP MG stored-history record layout from your own data.

The strap's live heart-rate broadcast (0x2A37) is reliable, so it serves as ground
truth. History records are stored raw. This tool lines the two up:

  1. reassemble every stored history frame (packet type 47 by default)
  2. find the byte offset holding a unix timestamp in the range we have live data for
  3. for every other offset, check how often its value matches the live heart rate
     at that timestamp (8-bit) and whether a 16-bit field tracks the R-R interval
  4. print the best candidates; --save writes data/history_layout.json, after which
     the collector decodes history frames as they arrive and `redecode` fills old data

    python -m loop.ble.history probe
    python -m loop.ble.history probe --save
    python -m loop.ble.history redecode

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
        raise SystemExit("No saved layout yet. Run: python -m loop.ble.history probe --save")
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
```

## `loop/sources/__init__.py` (empty file)

```python

```

## `loop/sources/oauth.py`

```python
"""Small OAuth 2.0 authorization-code helper shared by the WHOOP and Oura sources.
Tokens live in the local database and refresh themselves."""
from __future__ import annotations

import secrets
import time
import urllib.parse
from dataclasses import dataclass

import httpx

from .. import config, db


@dataclass
class Provider:
    name: str              # 'whoop' | 'oura'
    authorize_url: str
    token_url: str
    scopes: str
    client_id: str
    client_secret: str

    @property
    def redirect_uri(self) -> str:
        return f"{config.BASE_URL}/auth/{self.name}/callback"

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        return bool(db.kv_get(f"token:{self.name}"))

    def start(self) -> str:
        state = secrets.token_urlsafe(16)
        db.kv_set(f"oauth_state:{self.name}", state)
        q = urllib.parse.urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": self.scopes, "state": state})
        return f"{self.authorize_url}?{q}"

    def _save(self, tok: dict) -> None:
        old = db.kv_get(f"token:{self.name}") or {}
        tok.setdefault("refresh_token", old.get("refresh_token"))
        tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 60
        db.kv_set(f"token:{self.name}", tok)

    def finish(self, code: str, state: str) -> None:
        if not state or state != db.kv_get(f"oauth_state:{self.name}"):
            raise ValueError("OAuth state mismatch; start the connection again")
        db.kv_del(f"oauth_state:{self.name}")
        r = httpx.post(self.token_url, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret}, timeout=30)
        r.raise_for_status()
        self._save(r.json())

    def access_token(self) -> str:
        tok = db.kv_get(f"token:{self.name}")
        if not tok:
            raise RuntimeError(f"{self.name} is not connected")
        if time.time() >= tok.get("expires_at", 0):
            data = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                    "client_id": self.client_id, "client_secret": self.client_secret}
            if self.name == "whoop":
                data["scope"] = "offline"
            r = httpx.post(self.token_url, data=data, timeout=30)
            r.raise_for_status()
            self._save(r.json())
            tok = db.kv_get(f"token:{self.name}")
        return tok["access_token"]

    def disconnect(self) -> None:
        db.kv_del(f"token:{self.name}")

    def get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(4):
            r = httpx.get(url, params=params, timeout=30,
                          headers={"Authorization": f"Bearer {self.access_token()}"})
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}
```

## `loop/sources/whoop_api.py`

```python
"""Official WHOOP API v2 (temporary baseline while the membership lasts).

Imports only data on or after WHOOP_IMPORT_FROM, so the account's earlier
history (from its previous wearer) never enters the dashboard."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from .. import config, db
from .oauth import Provider

BASE = "https://api.prod.whoop.com/developer"

provider = Provider(
    name="whoop",
    authorize_url="https://api.prod.whoop.com/oauth/oauth2/auth",
    token_url="https://api.prod.whoop.com/oauth/oauth2/token",
    scopes="offline read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement",
    client_id=config.WHOOP_CLIENT_ID,
    client_secret=config.WHOOP_CLIENT_SECRET,
)


def _collection(path: str, start: str) -> list[dict]:
    out, token = [], None
    while True:
        params = {"limit": 25, "start": start}
        if token:
            params["nextToken"] = token
        page = provider.get(f"{BASE}{path}", params)
        out += page.get("records", [])
        token = page.get("next_token")
        if not token:
            return out


def _local(iso: str, offset: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    sign = -1 if offset.startswith("-") else 1
    hh, mm = offset.lstrip("+-").split(":")
    return dt.astimezone(timezone(sign * timedelta(hours=int(hh), minutes=int(mm))))


def _ms_to_min(v) -> float | None:
    return round(v / 60000, 1) if v is not None else None


def sync() -> str:
    now = int(time.time())
    floor = config.WHOOP_IMPORT_FROM + "T00:00:00.000Z"
    since = db.kv_get("cursor:whoop")
    # Re-read the last 3 days every time: scores get revised after more data syncs.
    start = max(floor, since or floor)
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")) - timedelta(days=3)
    start = max(floor, start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    sleeps = _collection("/v2/activity/sleep", start)
    sleep_day = {}
    for s in sleeps:
        if s.get("nap") or s.get("score_state") != "SCORED":
            continue
        end = _local(s["end"], s["timezone_offset"])
        day = end.date().isoformat()
        sleep_day[s["id"]] = day
        sc, st = s["score"], s["score"]["stage_summary"]
        light, deep, rem = (_ms_to_min(st.get(k)) or 0 for k in
                            ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
                             "total_rem_sleep_time_milli"))
        db.upsert_daily("whoop", "official", day, {
            "sleep_min": round(light + deep + rem, 1), "light_min": light, "deep_min": deep, "rem_min": rem,
            "in_bed_min": _ms_to_min(st.get("total_in_bed_time_milli")),
            "awake_min": _ms_to_min(st.get("total_awake_time_milli")),
            "sleep_perf": sc.get("sleep_performance_percentage"),
            "efficiency": sc.get("sleep_efficiency_percentage"),
            "resp_rate": sc.get("respiratory_rate"),
            "bedtime": int(_local(s["start"], s["timezone_offset"]).timestamp()),
            "waketime": int(end.timestamp()),
            "extra": {"sleep_consistency": sc.get("sleep_consistency_percentage"),
                      "disturbances": st.get("disturbance_count")},
        }, now)

    for r in _collection("/v2/recovery", start):
        day = sleep_day.get(r.get("sleep_id"))
        if not day or r.get("score_state") != "SCORED":
            continue
        sc = r["score"]
        db.upsert_daily("whoop", "official", day, {
            "recovery": sc.get("recovery_score"), "rhr": sc.get("resting_heart_rate"),
            "hrv": sc.get("hrv_rmssd_milli"), "spo2": sc.get("spo2_percentage"),
            "skin_temp": sc.get("skin_temp_celsius"), "calibrating": int(bool(sc.get("user_calibrating"))),
        }, now)

    for c in _collection("/v2/cycle", start):
        if c.get("score_state") != "SCORED":
            continue
        # A cycle starts when you fall asleep; it belongs to the day you then live.
        day = (_local(c["start"], c["timezone_offset"]) + timedelta(hours=6)).date().isoformat()
        sc = c["score"]
        db.upsert_daily("whoop", "official", day, {
            "strain": sc.get("strain"),
            "calories": round(sc["kilojoule"] / 4.184) if sc.get("kilojoule") else None,
            "steps": c.get("step_count"),
        }, now)

    workouts = _collection("/v2/activity/workout", start)
    with db.tx() as conn:
        for w in workouts:
            sc = w.get("score") or {}
            conn.execute(
                "INSERT OR REPLACE INTO workouts(device,source,id,start,end,sport,strain,avg_hr,max_hr,calories) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("whoop", "official", w["id"],
                 int(datetime.fromisoformat(w["start"].replace("Z", "+00:00")).timestamp()),
                 int(datetime.fromisoformat(w["end"].replace("Z", "+00:00")).timestamp()),
                 w.get("sport_name"), sc.get("strain"), sc.get("average_heart_rate"),
                 sc.get("max_heart_rate"), round(sc["kilojoule"] / 4.184) if sc.get("kilojoule") else None))

    db.kv_set("cursor:whoop", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    return f"{len(sleeps)} sleeps, {len(workouts)} workouts"
```

## `loop/sources/oura_api.py`

```python
"""Official Oura API v2 (baseline while the Oura membership is active)."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from .. import config, db
from .oauth import Provider

BASE = "https://api.ouraring.com/v2/usercollection"

provider = Provider(
    name="oura",
    authorize_url="https://cloud.ouraring.com/oauth/authorize",
    token_url="https://api.ouraring.com/oauth/token",
    scopes="email personal daily heartrate workout session spo2Daily",
    client_id=config.OURA_CLIENT_ID,
    client_secret=config.OURA_CLIENT_SECRET,
)


def _collection(name: str, start: str, end: str, by_datetime: bool = False) -> list[dict]:
    out, token = [], None
    while True:
        params = ({"start_datetime": start + "T00:00:00Z", "end_datetime": end + "T00:00:00Z"} if by_datetime
                  else {"start_date": start, "end_date": end})
        if token:
            params["next_token"] = token
        page = provider.get(f"{BASE}/{name}", params)
        out += page.get("data", [])
        token = page.get("next_token")
        if not token:
            return out


def _min(seconds) -> float | None:
    return round(seconds / 60, 1) if seconds is not None else None


def _ts(iso: str | None) -> int | None:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()) if iso else None


def _expand(sample: dict | None) -> list[tuple[int, float]]:
    """Oura 'PublicSample' {interval, items, timestamp} -> [(unix_ts, value)]."""
    if not sample or not sample.get("items"):
        return []
    t0, step = _ts(sample["timestamp"]), float(sample.get("interval") or 300)
    return [(int(t0 + i * step), v) for i, v in enumerate(sample["items"]) if v is not None]


def import_raw(sleeps: list[dict], start: str, end: str) -> int:
    """Raw-ish inputs for our own Oura scores: heart-rate samples (day and night)
    and Oura's 5-minute HRV readings during sleep. Oura publishes no beat-to-beat data."""
    hr = {}
    for s in _collection("heartrate", start, end, by_datetime=True):
        if s.get("bpm"):
            hr[_ts(s["timestamp"])] = s["bpm"]
    hrv = []
    for s in sleeps:
        for ts, v in _expand(s.get("heart_rate")):
            hr.setdefault(ts, round(v))
        hrv += _expand(s.get("hrv"))
    db.insert_samples("oura", [(ts, bpm, None, None) for ts, bpm in sorted(hr.items())])
    db.insert_hrv("oura", hrv)
    return len(hr)


def sync() -> str:
    now = int(time.time())
    since = db.kv_get("cursor:oura") or config.OURA_IMPORT_FROM
    start = max(config.OURA_IMPORT_FROM, (date.fromisoformat(since) - timedelta(days=3)).isoformat())
    end = (date.today() + timedelta(days=1)).isoformat()

    for r in _collection("daily_readiness", start, end):
        db.upsert_daily("oura", "official", r["day"], {
            "recovery": r.get("score"), "skin_temp": r.get("temperature_deviation"),
            "extra": {"readiness_contributors": r.get("contributors")}}, now)

    sleeps = _collection("sleep", start, end)
    for s in sleeps:
        if s.get("type") != "long_sleep":
            continue
        db.upsert_daily("oura", "official", s["day"], {
            "sleep_min": _min(s.get("total_sleep_duration")), "in_bed_min": _min(s.get("time_in_bed")),
            "deep_min": _min(s.get("deep_sleep_duration")), "rem_min": _min(s.get("rem_sleep_duration")),
            "light_min": _min(s.get("light_sleep_duration")), "awake_min": _min(s.get("awake_time")),
            "efficiency": s.get("efficiency"), "hrv": s.get("average_hrv"),
            "rhr": s.get("lowest_heart_rate"), "resp_rate": s.get("average_breath"),
            "bedtime": _ts(s.get("bedtime_start")), "waketime": _ts(s.get("bedtime_end")),
            "sleep_perf": round(100 * (s.get("total_sleep_duration") or 0) / 3600 / config.SLEEP_NEED_HOURS),
        }, now)

    for d in _collection("daily_sleep", start, end):
        db.upsert_daily("oura", "official", d["day"], {"extra": {"sleep_score": d.get("score")}}, now)

    for a in _collection("daily_activity", start, end):
        db.upsert_daily("oura", "official", a["day"], {
            "calories": a.get("total_calories"), "steps": a.get("steps"),
            "extra": {"activity_score": a.get("score")}}, now)

    try:
        for s in _collection("daily_spo2", start, end):
            avg = (s.get("spo2_percentage") or {}).get("average")
            if avg:
                db.upsert_daily("oura", "official", s["day"], {"spo2": avg}, now)
    except Exception:  # noqa: BLE001  (SpO2 scope may not be granted)
        pass

    n_hr = import_raw(sleeps, start, end)

    db.kv_set("cursor:oura", date.today().isoformat())
    return f"{len(sleeps)} sleep periods, {n_hr} heart-rate samples"
```

## `loop/analytics.py`

```python
"""Our own scores, computed from raw beat-level data (no vendor algorithms).

Per local day D (the day you wake up on):
  sleep      longest low-heart-rate block in the night window (D-1 18:00 to D 14:00)
  rhr        lowest 5-minute average heart rate while asleep
  hrv        median RMSSD of 5-minute windows while asleep (cleaned R-R intervals)
  stages     rough estimate from heart-rate level and variability (labelled "est.")
  recovery   0-100 from tonight's HRV and RHR against your own 30-night baseline,
             plus sleep vs need
  strain     0-21, from heart-rate reserve minutes over the day (Banister TRIMP,
             compressed onto a 0-21 curve); workouts you log count at a typical
             effort for minutes the strap wasn't heard
  workouts   detected as sustained stretches of raised heart rate

All of these are approximations. The point of the trial period is to measure how
closely they track the official numbers (see agreement()).
"""
from __future__ import annotations

import json
import math
import statistics as st
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, db

TZ = ZoneInfo(config.TIMEZONE)
MIN_COVERAGE_MIN = 180  # need 3 h of overnight data to score a night


# ---------------------------------------------------------------- helpers
def _minute_series(device: str, start: int, end: int) -> dict[int, dict]:
    rows = db.query("SELECT ts, bpm, rr_ms, motion FROM hr_samples WHERE device=? AND ts>=? AND ts<? ORDER BY ts",
                    (device, start, end))
    mins: dict[int, dict] = defaultdict(lambda: {"bpm": [], "rr": [], "motion": []})
    for r in rows:
        m = mins[r["ts"] // 60 * 60]
        if r["bpm"]:
            m["bpm"].append(r["bpm"])
        if r["rr_ms"]:
            m["rr"] += json.loads(r["rr_ms"])
        if r["motion"] is not None:
            m["motion"].append(r["motion"])
    out = {t: {"bpm": st.fmean(v["bpm"]) if v["bpm"] else None, "rr": v["rr"],
               "motion": st.fmean(v["motion"]) if v["motion"] else None} for t, v in sorted(mins.items())}
    # Sparse sources (Oura samples ~1 min in 5) are carried forward across short
    # gaps so they can be scored like dense ones. Gaps over 6 minutes stay gaps.
    filled, prev = {}, None
    for t in sorted(out):
        if prev is not None and out[prev]["bpm"] and 60 < t - prev <= FILL_GAP:
            for g in range(prev + 60, t, 60):
                filled[g] = {"bpm": out[prev]["bpm"], "rr": [], "motion": None}
        prev = t
    out.update(filled)
    return dict(sorted(out.items()))


FILL_GAP = 6 * 60


def _vendor_hrv(device: str, start: int, end: int) -> float | None:
    rows = db.query("SELECT rmssd FROM hrv_samples WHERE device=? AND ts>=? AND ts<?", (device, start, end))
    vals = [r["rmssd"] for r in rows if r["rmssd"]]
    return st.median(vals) if len(vals) >= 6 else None


def clean_rr(rr: list[float]) -> list[float]:
    """Drop physiologically impossible beats and ectopic jumps (>20% change)."""
    out: list[float] = []
    for x in rr:
        if not 300 <= x <= 2000:
            continue
        if out and abs(x - out[-1]) > 0.2 * out[-1]:
            continue
        out.append(x)
    return out


def rmssd(rr: list[float]) -> float | None:
    rr = clean_rr(rr)
    if len(rr) < 30:
        return None
    d = [(b - a) ** 2 for a, b in zip(rr, rr[1:])]
    return math.sqrt(st.fmean(d))


def resp_rate(rr: list[float]) -> float | None:
    """Breaths per minute from respiratory sinus arrhythmia: heartbeats speed up
    on each breath in and slow on each breath out, so the R-R series oscillates at
    the breathing rate. Resample the beat series at 2 Hz and find the dominant
    frequency between 7 and 27 breaths/min. Returns None when no clear peak."""
    rr = clean_rr(rr)
    if len(rr) < 120:  # ~2 minutes of beats
        return None
    t, times = 0.0, []
    for x in rr:
        t += x / 1000
        times.append(t)
    fs, n = 2.0, int(times[-1] * 2.0)
    series, j = [], 0
    for k in range(n):
        tk = times[0] + k / fs
        while j < len(times) - 2 and times[j + 1] < tk:
            j += 1
        t0, t1 = times[j], times[j + 1]
        w = 0 if t1 == t0 else min(1, max(0, (tk - t0) / (t1 - t0)))
        series.append(rr[j] + w * (rr[j + 1] - rr[j]))
    mean = st.fmean(series)
    series = [v - mean for v in series]
    best_f, best_p, powers = None, 0.0, []
    for i in range(24, 91):            # 0.120 .. 0.450 Hz in 0.005 Hz steps
        f = i * 0.005
        c = s = 0.0
        for k, v in enumerate(series):
            a = 2 * math.pi * f * k / fs
            c += v * math.cos(a)
            s += v * math.sin(a)
        p = c * c + s * s
        powers.append(p)
        if p > best_p:
            best_f, best_p = f, p
    if not best_f:
        return None
    # A real breathing rhythm concentrates power near one frequency; noise spreads it out.
    i = powers.index(best_p)
    near = sum(powers[max(0, i - 4):i + 5])
    if near < 0.45 * sum(powers):
        return None
    return round(best_f * 60, 1)


def _rolling_median(vals: list[float | None], half: int) -> list[float | None]:
    out = []
    for i in range(len(vals)):
        w = [v for v in vals[max(0, i - half):i + half + 1] if v is not None]
        out.append(st.median(w) if len(w) >= 3 else None)
    return out


def _local_midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())


# ---------------------------------------------------------------- nightly
def score_night(device: str, day: date) -> dict | None:
    start = _local_midnight(day) - 6 * 3600     # 18:00 the evening before
    end = _local_midnight(day) + 14 * 3600      # 14:00 on the day
    series = _minute_series(device, start, end)
    hr_minutes = [(t, v) for t, v in series.items() if v["bpm"]]
    if len(hr_minutes) < MIN_COVERAGE_MIN:
        return None

    times = [t for t, _ in hr_minutes]
    smooth = _rolling_median([v["bpm"] for _, v in hr_minutes], 7)
    vals = sorted(x for x in smooth if x)
    p10 = vals[int(0.10 * (len(vals) - 1))]
    p80 = vals[int(0.80 * (len(vals) - 1))]
    threshold = p10 + 0.4 * (p80 - p10)

    # Longest asleep block, bridging gaps (awakenings or missing data) under 30 min.
    blocks, cur = [], None
    for t, s, (_, v) in zip(times, smooth, hr_minutes):
        moving = v["motion"] is not None and v["motion"] > 0.15
        asleep = s is not None and s <= threshold and not moving
        if asleep:
            if cur and t - cur[1] <= 30 * 60:
                cur[1] = t
            else:
                cur = [t, t]
                blocks.append(cur)
    if not blocks:
        return None
    bed, wake = max(blocks, key=lambda b: b[1] - b[0])
    wake += 60
    if wake - bed < 2 * 3600:
        return None

    sleep_mins = [(t, v) for t, v in hr_minutes if bed <= t < wake]
    in_bed_min = (wake - bed) / 60

    # 5-minute epochs: HR, RMSSD, variability
    epochs = []
    for e0 in range(bed, wake, 300):
        chunk = [v for t, v in sleep_mins if e0 <= t < e0 + 300]
        bpms = [c["bpm"] for c in chunk if c["bpm"]]
        if len(bpms) < 3:
            continue
        rr = [x for c in chunk for x in c["rr"]]
        epochs.append({"t": e0, "hr": st.fmean(bpms), "sd": st.pstdev(bpms), "rmssd": rmssd(rr), "rr": rr})
    if not epochs:
        return None

    # Breathing rate from the calmest third of the night (every 3rd epoch keeps it cheap on a Pi Zero).
    calm = sorted(epochs, key=lambda e: e["hr"])[: max(1, len(epochs) // 3)]
    rates = [r for e in calm[::3] if (r := resp_rate(e["rr"])) is not None]
    resp = round(st.median(rates), 1) if len(rates) >= 3 else None

    rhr = min(e["hr"] for e in epochs)
    hrvs = [e["rmssd"] for e in epochs if e["rmssd"]]
    hrv = st.median(hrvs) if hrvs else None
    hrv_source = "beats"
    if hrv is None:  # no beat-to-beat data (Oura): use the vendor's 5-minute readings
        hrv, hrv_source = _vendor_hrv(device, bed, wake), "vendor_5min"

    # Rough staging: awake above threshold+5, deep = calm + lowest HR, REM = higher, more variable HR.
    hr_sorted = sorted(e["hr"] for e in epochs)
    q20 = hr_sorted[int(0.2 * (len(hr_sorted) - 1))]
    q55 = hr_sorted[int(0.55 * (len(hr_sorted) - 1))]
    sd_med = st.median(e["sd"] for e in epochs)
    stages = {"awake": 0.0, "light": 0.0, "deep": 0.0, "rem": 0.0}
    hypnogram = []
    for e in epochs:
        if e["hr"] > threshold + 5:
            s = "awake"
        elif e["hr"] <= q20 and e["sd"] <= sd_med:
            s = "deep"
        elif e["hr"] >= q55 and e["sd"] > sd_med:
            s = "rem"
        else:
            s = "light"
        stages[s] += 5
        hypnogram.append([e["t"], s])
    covered = len(epochs) * 5
    gap = max(0.0, in_bed_min - covered)
    stages["light"] += gap  # unobserved minutes inside the block count as light
    sleep_min = in_bed_min - stages["awake"]

    return {
        "rhr": round(rhr, 1), "hrv": round(hrv, 1) if hrv else None,
        "sleep_min": round(sleep_min, 1), "in_bed_min": round(in_bed_min, 1),
        "deep_min": stages["deep"], "rem_min": stages["rem"], "light_min": round(stages["light"], 1),
        "awake_min": stages["awake"], "efficiency": round(100 * sleep_min / in_bed_min, 1),
        "sleep_perf": round(100 * sleep_min / 60 / config.SLEEP_NEED_HOURS),
        "bedtime": bed, "waketime": wake, "resp_rate": resp,
        "extra": {"hypnogram": hypnogram, "coverage_min": len(sleep_mins), "stages_estimated": True,
                  "hrv_source": hrv_source},
    }


DEFAULT_RECOVERY = (0.0, 0.65, -0.35, 2.0)   # intercept, z_hrv, z_rhr, sleep term (logit space)
DEFAULT_STRAIN_K = 122.0


def _recovery_from(features: dict, coef) -> float:
    b0, b1, b2, b3 = coef
    raw = b0 + b1 * features["z_hrv"] + b2 * features["z_rhr"] + b3 * features["sleep_term"]
    return 100 / (1 + math.exp(-(1.3 * raw + 0.45)))


def recovery_score(device: str, day: date, night: dict) -> tuple[float | None, int]:
    prior = db.query(
        "SELECT hrv, rhr FROM daily WHERE device=? AND source='ours' AND day<? AND day>=? AND hrv IS NOT NULL",
        (device, day.isoformat(), (day - timedelta(days=30)).isoformat()))
    if night.get("hrv") is None:
        return None, 1
    calibrating = int(len(prior) < 7)
    if len(prior) < 4:
        ln_mu, ln_sd, rhr_mu, rhr_sd = math.log(night["hrv"]), 0.25, night["rhr"], 4.0
    else:
        lns = [math.log(p["hrv"]) for p in prior]
        ln_mu, ln_sd = st.fmean(lns), max(st.pstdev(lns), 0.08)
        rhrs = [p["rhr"] for p in prior]
        rhr_mu, rhr_sd = st.fmean(rhrs), max(st.pstdev(rhrs), 1.5)
    z_hrv = (math.log(night["hrv"]) - ln_mu) / ln_sd
    z_rhr = (night["rhr"] - rhr_mu) / rhr_sd
    sleep_ratio = min(night["sleep_min"] / 60 / config.SLEEP_NEED_HOURS, 1.1)
    features = {"z_hrv": round(z_hrv, 4), "z_rhr": round(z_rhr, 4), "sleep_term": round(sleep_ratio - 0.9, 4)}
    night.setdefault("extra", {})["features"] = features
    score = _recovery_from(features, DEFAULT_RECOVERY)
    cal = (db.kv_get(f"calib:{device}") or {}).get("recovery")
    if cal:  # blend toward the tuned model as evidence grows
        w = min(1.0, cal["n"] / 30)
        score = w * _recovery_from(features, cal["coef"]) + (1 - w) * score
    return round(max(1, min(99, score))), calibrating


# ---------------------------------------------------------------- daily strain
# Typical effort (fraction of heart-rate reserve) assumed for a logged workout's
# minutes when the strap wasn't heard. Used only to fill those minutes.
LOGGED_EFFORT = {"strength": 0.45, "run": 0.70, "cycle": 0.65, "walk": 0.35, "sport": 0.60, "other": 0.50}
WORKOUT_HRR = 0.45        # a minute counts as "working" at or above this share of heart-rate reserve
WORKOUT_MIN_MINUTES = 10  # shortest stretch reported as a workout
WORKOUT_BRIDGE = 4 * 60   # easier minutes (or gaps) up to this long don't split a workout


def _hrr(bpm: float, rest: float, mx: float) -> float:
    return max(0.0, min(1.0, (bpm - rest) / max(mx - rest, 1)))


def _trimp_minute(hrr: float) -> float:
    return hrr * 0.64 * math.exp(1.92 * hrr) if hrr >= 0.3 else hrr * 0.15


def _zone(bpm: float, mx: float) -> int:
    pct = bpm / mx
    return 0 if pct < 0.5 else min(5, int((pct - 0.5) / 0.1) + 1)


def _strain_k(device: str) -> float:
    return (db.kv_get(f"calib:{device}") or {}).get("strain_k") or DEFAULT_STRAIN_K


def _day_minutes(device: str, day: date) -> tuple[int, int, dict[int, float]]:
    start, end = _local_midnight(day), _local_midnight(day + timedelta(days=1))
    return start, end, {t: v["bpm"] for t, v in _minute_series(device, start, end).items() if v["bpm"]}


def _logged(device: str, start: int, end: int) -> list[dict]:
    return db.query("SELECT * FROM workouts WHERE device=? AND source='logged' AND start<? AND end>? ORDER BY start",
                    (device, end, start))


def _rest(bpms: list[float], rhr: float | None) -> float:
    return rhr or (st.quantiles(bpms, n=20)[0] if len(bpms) >= 20 else 60.0)


def strain_score(device: str, day: date, rhr: float | None) -> dict | None:
    start, end, mins = _day_minutes(device, day)
    logged = _logged(device, start, end)
    bpms = list(mins.values())
    if len(bpms) < 60 and not logged:
        return None
    rest, mx = _rest(bpms, rhr), config.max_hr()
    trimp, zone_min = 0.0, [0] * 6
    for b in bpms:
        trimp += _trimp_minute(_hrr(b, rest, mx))
        zone_min[_zone(b, mx)] += 1
    # Logged workouts: minutes the strap wasn't heard count at a typical effort for that activity.
    estimated = 0
    for w in logged:
        effort = LOGGED_EFFORT.get((w["sport"] or "").lower(), LOGGED_EFFORT["other"])
        for t in range(max(w["start"], start) // 60 * 60, min(w["end"], end), 60):
            if t not in mins:
                trimp += _trimp_minute(effort)
                estimated += 1
    strain = 21 * (1 - math.exp(-trimp / _strain_k(device)))
    return {"strain": round(strain, 1),
            "extra": {"zone_minutes": zone_min, "strain_coverage_min": len(bpms), "trimp": round(trimp, 2),
                      "strain_estimated_min": estimated}}


# ---------------------------------------------------------------- workouts
def _window_stats(mins: dict[int, float], a: int, b: int, rest: float, mx: float, k: float) -> dict | None:
    """Heart-rate summary for the minutes in [a, b)."""
    bpms = [v for t, v in mins.items() if a <= t < b]
    if not bpms:
        return None
    zones, trimp = [0] * 5, 0.0
    for v in bpms:
        trimp += _trimp_minute(_hrr(v, rest, mx))
        z = _zone(v, mx)
        if z:
            zones[z - 1] += 1
    return {"avg_hr": round(st.fmean(bpms), 1), "max_hr": round(max(bpms)), "zones": zones,
            "strain": round(21 * (1 - math.exp(-trimp / k)), 1), "covered": len(bpms)}


def detect_workouts(device: str, day: date, rhr: float | None = None) -> list[dict]:
    """Find workouts in a day's heart rate and store them (source 'detected').

    A workout is a stretch of at least WORKOUT_MIN_MINUTES minutes at or above
    WORKOUT_HRR of heart-rate reserve; easier minutes or gaps shorter than
    WORKOUT_BRIDGE don't split it. A detected stretch that mostly overlaps a
    workout you logged is dropped (yours wins, and gets the heart-rate numbers).
    Only time the strap was heard can be detected.
    """
    start, end, mins = _day_minutes(device, day)
    logged = _logged(device, start, end)
    bpms = list(mins.values())
    rest, mx, k = _rest(bpms, rhr), config.max_hr(), _strain_k(device)

    blocks: list[list[int]] = []
    cur = None
    for t in sorted(mins):
        if _hrr(mins[t], rest, mx) >= WORKOUT_HRR:
            if cur and t - cur[1] <= WORKOUT_BRIDGE:
                cur[1] = t
            else:
                cur = [t, t]
                blocks.append(cur)
    found = []
    for a, b in blocks:
        b += 60
        working = sum(1 for t, v in mins.items() if a <= t < b and _hrr(v, rest, mx) >= WORKOUT_HRR)
        if working < WORKOUT_MIN_MINUTES:
            continue
        if any(min(b, w["end"]) - max(a, w["start"]) >= 0.5 * (b - a) for w in logged):
            continue
        s = _window_stats(mins, a, b, rest, mx, k)
        found.append({"id": f"d{a}", "start": a, "end": b, **s})

    with db.tx() as c:
        c.execute("DELETE FROM workouts WHERE device=? AND source='detected' AND start>=? AND start<?",
                  (device, start, end))
        for w in found:
            c.execute("INSERT OR REPLACE INTO workouts(device,source,id,start,end,sport,strain,avg_hr,max_hr,zones) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (device, "detected", w["id"], w["start"], w["end"], "Workout", w["strain"], w["avg_hr"],
                       w["max_hr"], json.dumps(w["zones"])))
        for w in logged:  # give logged workouts their heart-rate numbers when at least half was heard
            s = _window_stats(mins, w["start"], w["end"], rest, mx, k)
            half = s and s["covered"] * 60 >= 0.5 * (w["end"] - w["start"])
            c.execute("UPDATE workouts SET strain=?, avg_hr=?, max_hr=?, zones=? WHERE device=? AND source='logged' AND id=?",
                      (s["strain"] if half else None, s["avg_hr"] if half else None, s["max_hr"] if half else None,
                       json.dumps(s["zones"]) if half else None, device, w["id"]))
    return found


def log_workout(device: str, day: str, sport: str, start_hhmm: str, minutes: int) -> dict:
    """Record a workout the user did (kept even when heart rate missed it)."""
    d = date.fromisoformat(day)
    hh, mm = (int(x) for x in start_hhmm.split(":")[:2])
    if not (0 <= hh < 24 and 0 <= mm < 60) or not 5 <= int(minutes) <= 600:
        raise ValueError("start must be HH:MM and minutes between 5 and 600")
    sport = sport.strip()[:30] or "Other"
    begin = int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ).timestamp())
    wid = f"l{begin}-{int(time.time() * 1000) % 100000}"
    with db.tx() as c:
        c.execute("INSERT INTO workouts(device,source,id,start,end,sport) VALUES(?,?,?,?,?,?)",
                  (device, "logged", wid, begin, begin + int(minutes) * 60, sport))
    recompute_day(device, d)
    return db.query("SELECT * FROM workouts WHERE device=? AND source='logged' AND id=?", (device, wid))[0]


def delete_workout(device: str, wid: str) -> bool:
    rows = db.query("SELECT start FROM workouts WHERE device=? AND source='logged' AND id=?", (device, wid))
    if not rows:
        return False
    with db.tx() as c:
        c.execute("DELETE FROM workouts WHERE device=? AND source='logged' AND id=?", (device, wid))
    recompute_day(device, datetime.fromtimestamp(rows[0]["start"], TZ).date())
    return True


# ---------------------------------------------------------------- auto-tuning
def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination for a small dense system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            raise ValueError("singular")
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def recalibrate(device: str, days: int = 90) -> dict:
    """Fit our Recovery model and Strain scale to the official numbers.

    Recovery: ridge regression in logit space of the official score on our nightly
    features (HRV and RHR vs baseline, sleep vs need), shrunk toward the default
    model. Nights the official app marks as calibrating are skipped, so WHOOP's
    first weeks on its previous wearer's baseline don't teach us anything wrong.
    Strain: the TRIMP scale that best matches official strain on well-covered days.
    The fitted numbers are kept after the membership ends.
    """
    since = (datetime.now(TZ).date() - timedelta(days=days)).isoformat()
    rows = db.query(
        "SELECT o.extra AS extra, f.recovery AS target, f.strain AS ostrain, o.day AS day FROM daily o "
        "JOIN daily f ON f.day=o.day AND f.device=o.device AND f.source='official' "
        "WHERE o.device=? AND o.source='ours' AND o.day>=? AND COALESCE(f.calibrating,0)=0", (device, since))
    X, y, strain_pairs = [], [], []
    for r in rows:
        extra = json.loads(r["extra"]) if r["extra"] else {}
        f = extra.get("features")
        if f and r["target"] is not None:
            p = min(max(r["target"], 2), 98) / 100
            y.append((math.log(p / (1 - p)) - 0.45) / 1.3)
            X.append([1.0, f["z_hrv"], f["z_rhr"], f["sleep_term"]])
        if extra.get("trimp") is not None and r["ostrain"] is not None and extra.get("strain_coverage_min", 0) >= 600:
            strain_pairs.append((extra["trimp"], r["ostrain"]))

    out = db.kv_get(f"calib:{device}") or {}
    if len(X) >= 14:
        lam = 3.0  # pull toward the default coefficients when data is thin or noisy
        prior = list(DEFAULT_RECOVERY)
        a = [[sum(x[i] * x[j] for x in X) + (lam if i == j else 0) for j in range(4)] for i in range(4)]
        b = [sum(x[i] * t for x, t in zip(X, y)) + lam * prior[i] for i in range(4)]
        try:
            coef = _solve(a, b)
            pred = [100 / (1 + math.exp(-(1.3 * sum(c * v for c, v in zip(coef, x)) + 0.45))) for x in X]
            actual = [100 / (1 + math.exp(-(1.3 * t + 0.45))) for t in y]
            out["recovery"] = {"coef": [round(c, 4) for c in coef], "n": len(X),
                               "mae": round(st.fmean(abs(p - q) for p, q in zip(pred, actual)), 1)}
        except ValueError:
            pass
    if len(strain_pairs) >= 10:
        def err(k: float) -> float:
            return sum((21 * (1 - math.exp(-t / k)) - s) ** 2 for t, s in strain_pairs)
        best = min((k for k in range(40, 401, 2)), key=err)
        out["strain_k"] = float(best)
        out["strain_n"] = len(strain_pairs)
    out["updated"] = int(time.time())
    db.kv_set(f"calib:{device}", out)
    return out


# ---------------------------------------------------------------- driver
WORKOUT_DEVICES = ("whoop",)  # detection needs dense heart rate; Oura's API samples are too sparse


def recompute_day(device: str, day: date) -> bool:
    """Score one day (night, strain, workouts). Returns True when the night was scored."""
    now = int(time.time())
    night = score_night(device, day)
    rhr = None
    if night:
        rec, cal = recovery_score(device, day, night)
        night.update({"recovery": rec, "calibrating": cal})
        rhr = night["rhr"]
        db.upsert_daily(device, "ours", day.isoformat(), night, now)
    strain = strain_score(device, day, rhr)
    if strain:
        existing = db.query("SELECT extra FROM daily WHERE device=? AND source='ours' AND day=?",
                            (device, day.isoformat()))
        extra = json.loads(existing[0]["extra"]) if existing and existing[0]["extra"] else {}
        extra.update(strain["extra"])
        db.upsert_daily(device, "ours", day.isoformat(), {"strain": strain["strain"], "extra": extra}, now)
    if device in WORKOUT_DEVICES:
        detect_workouts(device, day, rhr)
    return bool(night)


def recompute(device: str, days: int = 3, today: date | None = None) -> int:
    today = today or datetime.now(TZ).date()
    return sum(recompute_day(device, today - timedelta(days=i)) for i in range(days - 1, -1, -1))


# ---------------------------------------------------------------- agreement
TOLERANCE = {"hrv": ("pct", 15), "rhr": ("abs", 3), "resp_rate": ("abs", 1), "sleep_min": ("abs", 30), "recovery": ("abs", 15),
             "strain": ("abs", 2.5), "bedtime": ("abs", 1800), "waketime": ("abs", 1800)}


def agreement(a: tuple[str, str], b: tuple[str, str], metric: str, days: int = 60) -> dict:
    since = (datetime.now(TZ).date() - timedelta(days=days)).isoformat()
    rows = db.query(
        f"SELECT x.day, x.{metric} AS a, y.{metric} AS b FROM daily x JOIN daily y ON x.day=y.day "
        f"WHERE x.device=? AND x.source=? AND y.device=? AND y.source=? AND x.day>=? "
        f"AND x.{metric} IS NOT NULL AND y.{metric} IS NOT NULL ORDER BY x.day",
        (a[0], a[1], b[0], b[1], since))
    n = len(rows)
    if n == 0:
        return {"metric": metric, "n": 0}
    diffs = [r["a"] - r["b"] for r in rows]
    kind, tol = TOLERANCE.get(metric, ("abs", 0))
    within = sum(1 for r in rows if (abs(r["a"] - r["b"]) <= tol if kind == "abs"
                                       else abs(r["a"] - r["b"]) <= tol / 100 * abs(r["b"])))
    r_val = None
    if n >= 3:
        xa, xb = [r["a"] for r in rows], [r["b"] for r in rows]
        sa, sb = st.pstdev(xa), st.pstdev(xb)
        if sa and sb:
            ma, mb = st.fmean(xa), st.fmean(xb)
            r_val = round(sum((p - ma) * (q - mb) for p, q in zip(xa, xb)) / (n * sa * sb), 2)
    return {"metric": metric, "n": n, "mean_diff": round(st.fmean(diffs), 2),
            "mae": round(st.fmean(abs(d) for d in diffs), 2), "r": r_val,
            "within_pct": round(100 * within / n), "tolerance": f"{'±' + str(tol)}{'%' if kind == 'pct' else ''}",
            "pairs": [[r["day"], r["a"], r["b"]] for r in rows]}


def readiness(device: str) -> dict:
    """Is 'ours' good enough to cancel this device's membership?"""
    checks = {m: agreement((device, "ours"), (device, "official"), m, days=45) for m in ("hrv", "rhr", "sleep_min")}
    if device == "whoop":  # WHOOP's own baseline is unreliable early; also cross-check against Oura
        checks["hrv_vs_oura"] = agreement(("whoop", "ours"), ("oura", "official"), "hrv", days=45)
    nights = max((c["n"] for c in checks.values()), default=0)
    passing = [k for k, c in checks.items() if c["n"] >= 30 and c.get("within_pct", 0) >= 80]
    # Ours only replaces the membership when it comes from our own Bluetooth collection.
    own = db.query("SELECT COUNT(*) AS n FROM raw_frames WHERE device=?", (device,))[0]["n"] > 0 or device == "whoop"
    blocked = None if own else "Our Oura scores still use Oura's API data. Cancelling needs direct Bluetooth support for Ring 4."
    return {"device": device, "nights": nights, "target_nights": 30, "checks": checks, "blocked": blocked,
            "ready": not blocked and nights >= 30 and len(passing) >= 3}
```

## `loop/insights.py`

```python
"""Journal patterns, alerts and the morning brief."""
from __future__ import annotations

import json
import statistics as st
import time
import urllib.request
from datetime import date, datetime, timedelta

from . import analytics, config, db

PREFERRED = [("whoop", "ours"), ("whoop", "official"), ("oura", "official"), ("oura", "ours")]


def _daily(device: str, source: str, since: str) -> dict[str, dict]:
    return {r["day"]: r for r in db.query(
        "SELECT * FROM daily WHERE device=? AND source=? AND day>=?", (device, source, since))}


def preferred_series() -> tuple[str, str] | None:
    for d, s in PREFERRED:
        if db.query("SELECT 1 FROM daily WHERE device=? AND source=? AND recovery IS NOT NULL LIMIT 1", (d, s)):
            return d, s
    return None


# ------------------------------------------------------------------ journal
def journal(days: int = 120) -> list[dict]:
    since = (datetime.now(analytics.TZ).date() - timedelta(days=days)).isoformat()
    return db.query("SELECT day, tag FROM journal WHERE day>=? ORDER BY day", (since,))


def set_tag(day: str, tag: str, on: bool) -> None:
    date.fromisoformat(day)
    tag = tag.strip()[:40]
    if not tag:
        raise ValueError("empty tag")
    with db.tx() as c:
        if on:
            c.execute("INSERT OR IGNORE INTO journal(day, tag) VALUES(?,?)", (day, tag))
        else:
            c.execute("DELETE FROM journal WHERE day=? AND tag=?", (day, tag))


def patterns(device: str, source: str, days: int = 120) -> list[dict]:
    """Effect of each tag on the night that follows it (a tag on day D is compared
    on the night ending D+1), against nights after untagged days."""
    since = (datetime.now(analytics.TZ).date() - timedelta(days=days)).isoformat()
    rows = _daily(device, source, since)
    tags: dict[str, set[str]] = {}
    for j in journal(days):
        tags.setdefault(j["tag"], set()).add(j["day"])
    out = []
    for tag, tdays in tags.items():
        res = {"tag": tag, "metrics": {}}
        for m in ("recovery", "hrv", "rhr", "sleep_min"):
            with_, without = [], []
            for day in rows:
                prev = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
                v = rows[day].get(m)
                if v is None:
                    continue
                if prev in tdays:
                    with_.append(v)
                else:
                    without.append(v)
            if len(with_) >= 4 and len(without) >= 4:
                a, b = st.fmean(with_), st.fmean(without)
                sd = st.pstdev(with_ + without) or 1
                effect = (a - b) / sd
                res["metrics"][m] = {"with": round(a, 1), "without": round(b, 1), "diff": round(a - b, 1),
                                     "pct": round(100 * (a - b) / b) if b else None, "n": len(with_),
                                     "strength": "clear" if abs(effect) >= 0.5 and len(with_) >= 6
                                     else "possible" if abs(effect) >= 0.3 else "none"}
        res["n"] = len(tdays)
        if res["metrics"]:
            out.append(res)
    out.sort(key=lambda r: -abs((r["metrics"].get("recovery") or r["metrics"].get("hrv") or {}).get("diff", 0)))
    return out


# ------------------------------------------------------------------ alerts
def _baseline(rows: dict[str, dict], field: str, before: str, n: int = 30) -> float | None:
    vals = [r[field] for d, r in rows.items() if d < before and r.get(field) is not None][-n:]
    return st.fmean(vals) if len(vals) >= 7 else None


def alerts() -> list[dict]:
    out = []
    today = datetime.now(analytics.TZ).date()
    since = (today - timedelta(days=45)).isoformat()

    # Body signals: two nights of raised resting HR and lowered HRV often precede illness.
    pref = preferred_series()
    if pref:
        rows = dict(sorted(_daily(*pref, since).items()))
        days = list(rows)[-2:]
        if len(days) == 2:
            rb, hb = _baseline(rows, "rhr", days[0]), _baseline(rows, "hrv", days[0])
            if rb and hb and all((rows[d].get("rhr") or 0) >= rb + 3 and (rows[d].get("hrv") or 1e9) <= 0.85 * hb
                                 for d in days):
                out.append({"id": f"strain-signal-{days[-1]}", "level": "warn", "title": "Resting heart rate up, HRV down",
                            "body": "Two nights in a row. This often comes before getting sick or after heavy load. "
                                    "Consider an easier day and an early night."})
        recs = [rows[d].get("recovery") for d in list(rows)[-3:]]
        if len(recs) == 3 and all(r is not None and r < 34 for r in recs):
            out.append({"id": f"low-streak-{list(rows)[-1]}", "level": "warn", "title": "Three low-recovery days",
                        "body": "Recovery has been under 34% for three days. Sleep and lighter training help most."})

    # Strap memory: it holds about 14 days; warn well before it fills.
    last = db.query("SELECT MAX(ts) AS t FROM hr_samples WHERE device='whoop'")[0]["t"]
    has_ble = db.query("SELECT 1 FROM raw_frames WHERE device='whoop' LIMIT 1")
    if last and has_ble:
        age = (time.time() - last) / 86400
        if age > 10:
            out.append({"id": "strap-critical", "level": "critical", "title": f"Strap hasn't synced in {int(age)} days",
                        "body": "Its memory holds about 14 days. Bring it near the Pi or run the laptop collector soon."})
        elif age > 3:
            out.append({"id": "strap-warn", "level": "info", "title": f"Strap hasn't synced in {int(age)} days",
                        "body": "No data is lost yet. It will catch up next time it's near the Pi or your laptop."})

    # Official API sync failing
    for d in ("whoop", "oura"):
        if not db.kv_get(f"token:{d}"):
            continue
        recent = db.query("SELECT ok FROM sync_log WHERE device=? ORDER BY at DESC LIMIT 3", (d,))
        if len(recent) == 3 and not any(r["ok"] for r in recent):
            out.append({"id": f"sync-{d}", "level": "warn", "title": f"{'WHOOP' if d == 'whoop' else 'Oura'} sync is failing",
                        "body": "Open Devices to reconnect. The membership may have ended or access was removed."})
    return out


# ------------------------------------------------------------------ morning brief
def summary(day: str | None = None) -> dict:
    pref = preferred_series()
    if not pref:
        return {"day": day, "headline": "No scores yet", "items": [], "series": None}
    today = date.fromisoformat(day) if day else datetime.now(analytics.TZ).date()
    since = (today - timedelta(days=45)).isoformat()
    rows = _daily(*pref, since)
    r = rows.get(today.isoformat())
    if not r:
        return {"day": today.isoformat(), "headline": "Last night isn't scored yet", "items": [], "series": list(pref)}
    items = []
    rec = r.get("recovery")
    tier = "high" if rec is not None and rec >= 67 else "moderate" if rec is not None and rec >= 34 else "low"
    advice = {"high": "a good day to push", "moderate": "train as planned, keep intensity in check",
              "low": "keep today light and prioritise sleep"}[tier]
    headline = f"Recovery {round(rec)}%: {advice}." if rec is not None else "Recovery not available."
    hb, rb = _baseline(rows, "hrv", today.isoformat()), _baseline(rows, "rhr", today.isoformat())
    if r.get("hrv") and hb:
        pct = round(100 * (r["hrv"] - hb) / hb)
        items.append(f"HRV {round(r['hrv'])} ms, {abs(pct)}% {'above' if pct >= 0 else 'below'} your 30-day average.")
    if r.get("rhr") and rb:
        d = round(r["rhr"] - rb)
        items.append(f"Resting HR {round(r['rhr'])} bpm" + (f", {abs(d)} {'above' if d > 0 else 'below'} usual." if d else ", right at your usual."))
    if r.get("sleep_min"):
        need = config.SLEEP_NEED_HOURS * 60
        gap = round(r["sleep_min"] - need)
        h, m = divmod(int(r["sleep_min"]), 60)
        items.append(f"Slept {h}h {m:02d}m" + (f", {abs(gap)} min {'over' if gap > 0 else 'short of'} your need." if abs(gap) >= 10 else ", right on your need."))
    y = rows.get((today - timedelta(days=1)).isoformat())
    if y and y.get("strain") is not None:
        items.append(f"Yesterday's strain {y['strain']:.1f}.")
    ytags = [j["tag"] for j in db.query("SELECT tag FROM journal WHERE day=?", ((today - timedelta(days=1)).isoformat(),))]
    if ytags:
        known = {p["tag"]: p for p in patterns(*pref)}
        for t in ytags:
            eff = (known.get(t, {}).get("metrics") or {}).get("hrv")
            if eff and eff["strength"] != "none":
                items.append(f"You logged {t.lower()} yesterday; it usually moves your HRV by {eff['pct']:+d}%.")
    n_alerts = len(alerts())
    if n_alerts:
        items.append(f"{n_alerts} alert{'s' if n_alerts > 1 else ''} to look at.")
    return {"day": today.isoformat(), "headline": headline, "items": items, "series": list(pref), "tier": tier}


# ------------------------------------------------------------------ push (optional)
def push(title: str, body: str) -> None:
    """Send a notification through ntfy (set NTFY_URL to https://ntfy.sh/<long-random-topic>
    or your own ntfy server). Messages carry only the summary numbers."""
    url = config.get("NTFY_URL")
    if not url:
        return
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={"Title": title, "Tags": "heart"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:  # noqa: BLE001
        pass


def push_new() -> None:
    """Called by the server loop: new alerts once each, and the morning brief once a day."""
    if not config.get("NTFY_URL"):
        return
    sent = set(db.kv_get("pushed", []))
    for a in alerts():
        if a["id"] not in sent:
            push(a["title"], a["body"])
            sent.add(a["id"])
    now = datetime.now(analytics.TZ)
    key = f"brief-{now.date()}"
    if key not in sent and now.hour >= int(config.get("BRIEF_HOUR", "8")):
        s = summary()
        if s["items"]:
            push("Morning brief", s["headline"] + "\n" + "\n".join(s["items"]))
            sent.add(key)
    db.kv_set("pushed", sorted(sent)[-200:])
```

## `loop/server.py`

```python
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
```

## `loop/mcp_server.py`

```python
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
```

## `loop/tools/__init__.py` (empty file)

```python

```

## `loop/tools/seed_demo.py`

```python
"""Fill a throwaway database with ~60 days of realistic synthetic data, so the
dashboard and scoring can be tried before the Pi and strap are set up.

    LOOP_DB=data/demo.sqlite python -m loop.tools.seed_demo
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta

from .. import analytics, db

R = random.Random(7)
DAYS = 60


def truth_for(i: int) -> dict:
    """Ground truth for night i (shared by every 'device' so they roughly agree)."""
    weekly = math.sin(i / 7 * 2 * math.pi)
    stress = 1 if R.random() < 0.12 else 0
    hrv = math.exp(math.log(46) + 0.12 * weekly - 0.35 * stress + R.gauss(0, 0.12))
    rhr = 53 + 2.2 * stress - 0.8 * weekly + R.gauss(0, 1.3)
    sleep_h = max(4.8, min(9.2, 7.3 + R.gauss(0, 0.7) - 0.8 * stress))
    bed = 23.1 + R.gauss(0, 0.6)
    workout = R.random() < 0.55
    return {"hrv": hrv, "rhr": rhr, "sleep_h": sleep_h, "bed": bed, "workout": workout,
            "resp": round(14.6 + R.gauss(0, 0.5) + 0.8 * stress, 1)}


def gen_samples(day, t: dict, prev_t: dict) -> list[tuple]:
    tz = analytics.TZ
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    bed = midnight - timedelta(hours=24 - t["bed"])
    wake = bed + timedelta(hours=t["sleep_h"] / 0.93)
    rows = []

    breath_hz = t.get("resp", 14.5) / 60
    clock = {"t": 0.0}

    def beat_rr(hr: float, rmssd: float, seconds: int) -> list[float]:
        base = 60000 / hr
        n = max(1, round(seconds * hr / 60))
        out, x = [], base
        for _ in range(n):
            x = base + 0.5 * (x - base) + R.gauss(0, rmssd / 1.6)
            v = x + 0.8 * rmssd * math.sin(2 * math.pi * breath_hz * clock["t"])  # breathing rhythm
            clock["t"] += v / 1000
            out.append(round(max(320, min(1900, v)), 1))
        return out

    # Overnight at 5 s resolution with stage-like cycles.
    ts = int(bed.timestamp()) - 3600
    end = int(wake.timestamp()) + 1800
    while ts < end:
        in_sleep = bed.timestamp() <= ts < wake.timestamp()
        if in_sleep:
            phase = (ts - bed.timestamp()) / 5400 * 2 * math.pi  # ~90 min cycles
            hr = t["rhr"] + 3.5 + 3.2 * math.sin(phase) + R.gauss(0, 1.1)
            rm = t["hrv"] * (1.15 - 0.25 * math.sin(phase))
            if R.random() < 0.004:
                hr += 14
        else:
            hr = t["rhr"] + 18 + R.gauss(0, 3)
            rm = t["hrv"] * 0.5
        rows.append((ts, round(hr), beat_rr(hr, rm, 5), None))
        ts += 5

    # Daytime: only the hours at home are in range of the Pi (morning + evening),
    # plus a workout on some days.
    for h0, h1 in ((wake.hour + 1, 9), (17, 22)):
        ts = int((midnight + timedelta(hours=h0)).timestamp())
        stop = int((midnight + timedelta(hours=h1)).timestamp())
        while ts < stop:
            hr = t["rhr"] + 24 + 6 * math.sin(ts / 1500) + R.gauss(0, 4)
            rows.append((ts, round(hr), beat_rr(hr, t["hrv"] * 0.4, 5), None))
            ts += 5
    if t["workout"]:
        ts = int((midnight + timedelta(hours=18, minutes=10)).timestamp())
        for k in range(0, 50 * 60, 5):
            ramp = min(1, k / 300, (3000 - k) / 300)
            hr = t["rhr"] + 20 + 85 * ramp + R.gauss(0, 4)
            rows.append((ts + k, round(hr), beat_rr(hr, 8, 5), None))
    return rows


def main() -> None:
    db.conn()
    now = int(time.time())
    today = datetime.now(analytics.TZ).date()
    truths = [truth_for(i) for i in range(DAYS + 1)]
    # Journal tags on day i shape the night that follows (truths[i - 1]).
    effects = {"Alcohol": (.22, .78, 3.0, -.2), "Late meal": (.25, .93, 1.5, -.3), "Caffeine after 2pm": (.2, .96, .5, -.45),
               "Meditation": (.3, 1.07, -.5, .1), "Keto day": (.6, 1.02, -.3, 0), "Hard workout": (.0, .9, 1.5, .2)}
    entries = []
    for i in range(DAYS, 0, -1):
        for tag, (p, hrv_f, rhr_d, sleep_d) in effects.items():
            hit = truths[i]["workout"] if tag == "Hard workout" else R.random() < p
            if hit:
                entries.append(((today - timedelta(days=i)).isoformat(), tag))
                nxt = truths[i - 1]
                nxt["hrv"] *= hrv_f
                nxt["rhr"] += rhr_d
                nxt["sleep_h"] = max(4.5, nxt["sleep_h"] + sleep_d)
    with db.tx() as c:
        c.executemany("INSERT OR IGNORE INTO journal(day, tag) VALUES(?,?)", entries)
    print("generating samples ...")
    for i in range(DAYS, -1, -1):
        day = today - timedelta(days=i)
        t = truths[i]
        bed_ts = int((datetime(day.year, day.month, day.day, tzinfo=analytics.TZ)
                      - timedelta(hours=24 - t["bed"])).timestamp())
        wake_ts = bed_ts + int(t["sleep_h"] / 0.93 * 3600)
        rows = [s for s in gen_samples(day, t, truths[min(i + 1, DAYS)]) if s[0] <= now]
        if i <= DAYS - 3:  # strap worn from 3 days into the window
            db.insert_samples("whoop", rows)
        # Oura API style: ~1 heart-rate sample per 5 minutes, no beats, plus 5-minute HRV in sleep
        db.insert_samples("oura", [(ts, bpm - 1, None, None) for ts, bpm, _, _ in rows if ts % 300 < 5])
        db.insert_hrv("oura", [(ts, round(t["hrv"] * 0.92 * R.uniform(.75, 1.25), 1))
                               for ts in range(bed_ts, min(wake_ts, now), 300)])
        sleep_min = t["sleep_h"] * 60

        def stages(scale: float) -> dict:
            s = sleep_min * scale
            return {"sleep_min": round(s), "deep_min": round(s * R.uniform(.16, .22)),
                    "rem_min": round(s * R.uniform(.20, .26)), "in_bed_min": round(s / 0.92),
                    "awake_min": round(s / 0.92 - s)}

        rec_base = 50 + 32 * math.tanh((math.log(t["hrv"]) - math.log(46)) / 0.2) - 4 * (t["rhr"] - 53)
        # WHOOP official (baselines settle after ~3 weeks on a new wearer)
        settle = 1.0 if i < DAYS - 24 else 0.6
        w = stages(R.uniform(.96, 1.03))
        w["light_min"] = w["sleep_min"] - w["deep_min"] - w["rem_min"]
        db.upsert_daily("whoop", "official", day.isoformat(), {
            **w, "recovery": round(max(5, min(99, rec_base * settle + 50 * (1 - settle) + R.gauss(0, 6)))),
            "hrv": round(t["hrv"] * R.uniform(.92, 1.08), 1), "rhr": round(t["rhr"] + R.gauss(0, .8)),
            "strain": round(min(20.5, (13.5 if t["workout"] else 7.2) + R.gauss(0, 1.6)), 1),
            "sleep_perf": round(100 * sleep_min / 480), "efficiency": round(R.uniform(88, 95), 1),
            "resp_rate": round(t["resp"] + R.gauss(0, .3), 1), "spo2": round(R.uniform(95, 98), 1),
            "skin_temp": round(33.6 + R.gauss(0, .3), 2), "bedtime": bed_ts + R.randint(-300, 300),
            "waketime": wake_ts + R.randint(-300, 300), "calories": round(2350 + (550 if t["workout"] else 0)),
            "calibrating": int(i > DAYS - 21)}, now)
        # Oura official
        o = stages(R.uniform(.93, 1.05))
        o["light_min"] = o["sleep_min"] - o["deep_min"] - o["rem_min"]
        db.upsert_daily("oura", "official", day.isoformat(), {
            **o, "recovery": round(max(10, min(98, 0.7 * rec_base + 25 + R.gauss(0, 5)))),
            "hrv": round(t["hrv"] * R.uniform(.85, 1.0), 1), "rhr": round(t["rhr"] - 1 + R.gauss(0, .9)),
            "sleep_perf": round(100 * sleep_min / 480), "efficiency": round(R.uniform(86, 94)),
            "resp_rate": round(t["resp"] - 0.2 + R.gauss(0, .3), 1), "spo2": round(R.uniform(95, 98), 1),
            "skin_temp": round(R.gauss(0, .25), 2), "bedtime": bed_ts + R.randint(-600, 600),
            "waketime": wake_ts + R.randint(-600, 600), "steps": R.randint(5200, 12800),
            "calories": round(2300 + (500 if t["workout"] else 0)),
            "extra": {"sleep_score": R.randint(68, 90), "activity_score": R.randint(70, 95)}}, now)

    # Lunchtime strength sessions on some rest days: out of range of the Pi, so only the log knows.
    with db.tx() as c:
        for i in range(DAYS - 3, 0, -1):
            if not truths[i]["workout"] and i % 3 == 0:
                day = today - timedelta(days=i)
                begin = int(datetime(day.year, day.month, day.day, 12, 15, tzinfo=analytics.TZ).timestamp())
                c.execute("INSERT OR REPLACE INTO workouts(device,source,id,start,end,sport) VALUES(?,?,?,?,?,?)",
                          ("whoop", "logged", f"l{begin}", begin, begin + 45 * 60, "Strength"))
    print("scoring ...")
    analytics.recompute("whoop", days=DAYS + 1, today=today)
    analytics.recompute("oura", days=DAYS + 1, today=today)
    print("tuning ...")
    for d in ("whoop", "oura"):
        analytics.recalibrate(d)
        analytics.recompute(d, days=DAYS + 1, today=today)
    with db.tx() as c:
        c.execute("INSERT INTO sync_log(device,source,at,ok,detail) VALUES('whoop','official',?,1,'demo')", (now,))
        c.execute("INSERT INTO sync_log(device,source,at,ok,detail) VALUES('oura','official',?,1,'demo')", (now,))
    print("done")


if __name__ == "__main__":
    main()
```

## `loop/tools/build_demo.py`

```python
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
```

## `web/app.html`

```html
<title>Loop</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=Instrument+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
/* Layout: a phone-first instrument panel showing one number per metric. One column of
   panels, tab bar at the bottom; two columns on desktop. Comparisons sit on a separate screen. */
:root {
  --bg: #EEF1F5;
  --panel: #FFFFFF;
  --panel-2: #F5F7FA;
  --ink: #141A24;
  --ink-2: #4A5566;
  --ink-3: #7B8697;
  --line: #DCE2EA;
  --accent: #2F6FDB;
  --whoop: #B8740F;
  --oura: #6A55D8;
  --good: #1F8F66;
  --warn: #A9781A;
  --bad: #C8463A;
  --track: #E3E8EF;
  --sleep: #5B62C9;
  --shadow: 0 1px 2px rgba(20, 26, 36, .06), 0 4px 16px rgba(20, 26, 36, .04);
  --f-display: "Bricolage Grotesque", "Segoe UI", system-ui, sans-serif;
  --f-body: "Instrument Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --f-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0C111A; --panel: #141B27; --panel-2: #1A2331; --ink: #E8EDF4; --ink-2: #A9B4C4;
    --ink-3: #758197; --line: #253042; --accent: #6C9BF0; --whoop: #C4851F; --oura: #8270E6;
    --good: #3CB98A; --warn: #D6A23E; --bad: #E2675B; --track: #222C3B; --sleep: #8F95EE;
    --shadow: 0 1px 2px rgba(0,0,0,.3); color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0C111A; --panel: #141B27; --panel-2: #1A2331; --ink: #E8EDF4; --ink-2: #A9B4C4;
  --ink-3: #758197; --line: #253042; --accent: #6C9BF0; --whoop: #C4851F; --oura: #8270E6;
  --good: #3CB98A; --warn: #D6A23E; --bad: #E2675B; --track: #222C3B; --sleep: #8F95EE;
  --shadow: 0 1px 2px rgba(0,0,0,.3); color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { background: var(--bg); color: var(--ink); font: 15px/1.45 var(--f-body); -webkit-font-smoothing: antialiased; }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 30; background: var(--bg); }
button { font: inherit; color: inherit; }
.num, .mono { font-family: var(--f-mono); font-variant-numeric: tabular-nums; }
h1, h2, h3 { font-family: var(--f-display); margin: 0; text-wrap: balance; letter-spacing: -.01em; }
.eyebrow { font-size: 11px; letter-spacing: .09em; text-transform: uppercase; color: var(--ink-3); font-weight: 600; }
.muted { color: var(--ink-3); }

/* Top bar */
.top { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 20; background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); border-bottom: 1px solid var(--line); }
.top-in { max-width: 1120px; margin: 0 auto; padding-block: 10px; padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px)); display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center; }
.brand { display: flex; align-items: baseline; gap: 8px; margin-right: auto; }
.brand h1 { font-size: 20px; }
.brand .demo { font-size: 11px; padding: 2px 7px; border-radius: 99px; background: var(--panel-2); color: var(--ink-2); border: 1px solid var(--line); }
.filters { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.filters .sep { width: 1px; height: 20px; background: var(--line); margin: 0 4px; }
.chip { display: inline-flex; align-items: center; gap: 7px; height: 32px; padding: 0 12px; border-radius: 99px; border: 1px solid var(--line);
  background: var(--panel); cursor: pointer; font-size: 13px; font-weight: 500; color: var(--ink-2); }
.chip[aria-pressed="true"] { color: var(--ink); border-color: color-mix(in srgb, var(--ink) 30%, var(--line)); }
.chip[aria-pressed="false"] { opacity: .55; }
.chip:focus-visible, .tab:focus-visible, .btn:focus-visible, select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.swatch { width: 14px; height: 4px; border-radius: 2px; flex: none; }
.swatch.dash { background: none !important; border-top: 2px dashed; height: 0; }

/* Main */
main { max-width: 1120px; margin: 0 auto; padding-block: 16px 110px; padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px)); }
.view { display: grid; gap: 14px; }
.grid2 { display: grid; gap: 14px; grid-template-columns: 1fr; }
@media (min-width: 860px) { .grid2 { grid-template-columns: 1fr 1fr; } .span2 { grid-column: 1 / -1; } }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 16px; box-shadow: var(--shadow); min-width: 0; }
.panel-h { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.panel-h h2 { font-size: 17px; }
.daybar { display: flex; align-items: center; gap: 8px; }
.daybar h2 { font-size: 24px; }
.iconbtn { width: 34px; height: 34px; border-radius: 10px; border: 1px solid var(--line); background: var(--panel); cursor: pointer; display: grid; place-items: center; }
.iconbtn:disabled { opacity: .35; cursor: default; }
.btn { height: 36px; padding: 0 14px; border-radius: 10px; border: 1px solid var(--line); background: var(--panel); cursor: pointer; font-weight: 600; font-size: 13px; }
.btn.primary { background: var(--ink); color: var(--panel); border-color: var(--ink); }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.seg button { border: 0; background: var(--panel); padding: 6px 12px; cursor: pointer; font-size: 13px; color: var(--ink-2); }
.seg button[aria-pressed="true"] { background: var(--panel-2); color: var(--ink); font-weight: 600; }
select { height: 34px; border-radius: 10px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); padding: 0 10px; font: inherit; font-size: 13px; }

/* Rings */
.rings { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.ring { display: grid; justify-items: center; gap: 6px; text-align: center; min-width: 0; }
.ring svg { width: 100%; max-width: 168px; height: auto; }
.ring .label { font-size: 12px; font-weight: 600; color: var(--ink-2); letter-spacing: .04em; text-transform: uppercase; }
.ring-legend { display: grid; gap: 3px; width: 100%; max-width: 170px; }
.ring-legend div { display: flex; justify-content: space-between; gap: 6px; font-size: 12px; color: var(--ink-2); }
.ring-legend .num { color: var(--ink); }
.tier { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 99px; background: var(--panel-2); border: 1px solid var(--line); }
.tier i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }

/* Vitals table */
.vitals { width: 100%; border-collapse: collapse; font-size: 14px; }
.vitals th, .vitals td { padding: 10px 6px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
.vitals th:first-child, .vitals td:first-child { text-align: left; }
.vitals thead th { font-size: 11px; color: var(--ink-3); font-weight: 600; letter-spacing: .06em; text-transform: uppercase; }
.vitals tbody tr:last-child td { border-bottom: 0; }
.delta { font-size: 11px; display: block; color: var(--ink-3); }
.delta.up { color: var(--good); } .delta.down { color: var(--bad); }
.na { color: var(--ink-3); font-size: 12px; }
.scroll-x { overflow-x: auto; }

/* Live */
.live { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.live .bpm { font-family: var(--f-display); font-size: 44px; font-weight: 700; line-height: 1; }
.pulse { width: 10px; height: 10px; border-radius: 50%; background: var(--bad); box-shadow: 0 0 0 0 color-mix(in srgb, var(--bad) 50%, transparent); animation: pulse 1.2s infinite; }
.pulse.off { background: var(--ink-3); animation: none; }
@keyframes pulse { 70% { box-shadow: 0 0 0 10px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
@media (prefers-reduced-motion: reduce) { .pulse { animation: none; } }

/* Charts */
.chart { position: relative; width: 100%; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.chart text { fill: var(--ink-3); font: 11px var(--f-mono); }
.tip { position: absolute; pointer-events: none; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; box-shadow: var(--shadow);
  padding: 8px 10px; font-size: 12px; min-width: 150px; z-index: 5; display: none; }
.tip .row { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
.tip .row span:first-child { display: flex; align-items: center; gap: 6px; color: var(--ink-2); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12px; color: var(--ink-2); margin-top: 8px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }

/* Stage bars */
.stages { display: grid; gap: 10px; }
.stage-row { display: grid; grid-template-columns: 120px 1fr; gap: 10px; align-items: center; font-size: 12px; }
.stage-row .who { color: var(--ink-2); display: flex; align-items: center; gap: 6px; min-width: 0; }
.stackbar { display: flex; height: 14px; border-radius: 4px; overflow: hidden; gap: 2px; background: var(--panel); }
.stackbar div { height: 100%; }
.st-awake { background: color-mix(in srgb, var(--ink-3) 45%, transparent); }
.st-light { background: color-mix(in srgb, var(--accent) 40%, var(--panel)); }
.st-rem { background: color-mix(in srgb, var(--accent) 70%, var(--panel)); }
.st-deep { background: var(--accent); }

/* Stat cards */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
.stat { background: var(--panel-2); border-radius: 12px; padding: 12px; min-width: 0; }
.stat .v { font-family: var(--f-display); font-size: 26px; font-weight: 700; }
.stat .k { font-size: 12px; color: var(--ink-2); }
.stat .s { font-size: 11px; color: var(--ink-3); margin-top: 2px; }

/* Readiness */
.progress { height: 8px; background: var(--track); border-radius: 99px; overflow: hidden; }
.progress div { height: 100%; background: var(--accent); border-radius: 99px; }
.checklist { display: grid; gap: 8px; margin-top: 12px; }
.check { display: flex; gap: 10px; align-items: flex-start; font-size: 13px; }
.check b { font-weight: 600; }
.mark { width: 18px; height: 18px; border-radius: 50%; flex: none; display: grid; place-items: center; font-size: 11px; color: var(--panel); margin-top: 1px; }

/* Devices */
.dev { display: grid; gap: 12px; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; font-size: 13px; }
.kv dt { color: var(--ink-3); } .kv dd { margin: 0; text-align: right; }
.note { font-size: 13px; color: var(--ink-2); background: var(--panel-2); border-radius: 10px; padding: 10px 12px; }

/* Tab bar */
.tabs { position: fixed; left: 0; right: 0; bottom: 0; z-index: 20; background: color-mix(in srgb, var(--panel) 92%, transparent);
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-top: 1px solid var(--line);
  padding-bottom: env(safe-area-inset-bottom, 0px); padding-left: max(8px, env(safe-area-inset-left, 0px)); padding-right: max(8px, env(safe-area-inset-right, 0px)); }
.tabs-in { max-width: 560px; margin: 0 auto; display: grid; grid-template-columns: repeat(4, 1fr); }
.tab { display: grid; justify-items: center; gap: 2px; padding: 8px 0 9px; border: 0; background: none; cursor: pointer; color: var(--ink-3); font-size: 11px; font-weight: 600; }
.tab svg { width: 22px; height: 22px; stroke: currentColor; fill: none; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.tab[aria-current="page"] { color: var(--ink); }
.alerts { display: grid; gap: 8px; }
.alert { display: flex; gap: 12px; align-items: flex-start; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--line);
  background: var(--panel); font-size: 13px; color: var(--ink-2); box-shadow: var(--shadow); }
.alert b { color: var(--ink); display: block; margin-bottom: 2px; }
.alert-mark { width: 22px; height: 22px; border-radius: 50%; flex: none; display: grid; place-items: center; font-weight: 700; font-size: 12px; color: var(--panel); background: var(--ink-3); }
.alert.warn .alert-mark { background: var(--warn); } .alert.critical .alert-mark { background: var(--bad); }
.alert.critical { border-color: color-mix(in srgb, var(--bad) 45%, var(--line)); }
.brief:empty { display: none; }
.brief { background: var(--panel-2); border-radius: 12px; padding: 12px 14px; margin-bottom: 14px; }
.brief-head { font-family: var(--f-display); font-size: 17px; font-weight: 700; margin: 4px 0 6px; text-wrap: balance; }
.brief ul { margin: 0; padding-left: 18px; color: var(--ink-2); font-size: 13px; display: grid; gap: 2px; }
.eff { display: block; font-size: 11px; }
.eff.clear { font-weight: 700; color: var(--ink); } .eff.possible { color: var(--ink-2); } .eff.none { color: var(--ink-3); }
.empty { padding: 28px 12px; text-align: center; color: var(--ink-3); font-size: 13px; }

.ring .label { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; justify-content: center; }
.src { font: 500 10px var(--f-mono); letter-spacing: .02em; text-transform: lowercase; color: var(--ink-3); border: 1px solid var(--line); border-radius: 99px; padding: 1px 6px; vertical-align: middle; white-space: nowrap; }
.hidden-note { margin: 12px 0 0; font-size: 12px; color: var(--ink-3); }
.linkbtn { border: 0; background: none; padding: 0; color: var(--accent); cursor: pointer; font-size: 12px; text-decoration: underline; }
.linkrow { display: flex; justify-content: space-between; align-items: center; gap: 12px; text-align: left; cursor: pointer; width: 100%; }
.setrow { display: flex; justify-content: space-between; align-items: center; gap: 14px; padding: 10px 0; border-bottom: 1px solid var(--line); font-size: 14px; }
.setrow:last-child { border-bottom: 0; padding-bottom: 0; }
.switch { flex: none; width: 46px; height: 28px; border-radius: 99px; border: 1px solid var(--line); background: var(--track); cursor: pointer; position: relative; padding: 0; }
.switch i { position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 50%; background: var(--panel); box-shadow: var(--shadow); transition: transform .15s; }
.switch[aria-checked="true"] { background: var(--good); border-color: var(--good); }
.switch[aria-checked="true"] i { transform: translateX(18px); }
.switch:focus-visible, .linkbtn:focus-visible, .linkrow:focus-visible, .iconbtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .switch i { transition: none; } }
.cov { font: 11px var(--f-mono); color: var(--ink-3); line-height: 1.5; }
.src.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 50%, var(--line)); }
.tonight-main { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.bigtime { font-family: var(--f-display); font-size: 30px; font-weight: 700; line-height: 1.15; }
.kv dt { min-width: 0; }
.wk { padding: 10px 0; border-bottom: 1px solid var(--line); display: grid; gap: 5px; }
.wk-top { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
.wk-form { display: grid; grid-template-columns: 1fr 1fr 80px; gap: 8px; margin-top: 14px; }
.wk-form .btn { grid-column: 1 / -1; }
.wk-form input, .wk-form select { height: 36px; min-width: 0; width: 100%; border-radius: 10px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); padding: 0 10px; font: inherit; font-size: 13px; }
.wk-form input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.zones { height: 10px; }
.z1 { background: color-mix(in srgb, var(--accent) 30%, var(--panel)); } .z2 { background: color-mix(in srgb, var(--accent) 60%, var(--panel)); }
.z3 { background: var(--accent); } .z4 { background: var(--warn); } .z5 { background: var(--bad); }
</style>

<header class="top">
  <div class="top-in">
    <div class="brand"><h1>Loop</h1><span class="demo" id="demoBadge" hidden>Sample data</span></div>
  </div>
</header>

<main>
  <section class="view" id="view-today" data-view="today"></section>
  <section class="view" id="view-sleep" data-view="sleep" hidden></section>
  <section class="view" id="view-trends" data-view="trends" hidden></section>
  <section class="view" id="view-compare" data-view="compare" hidden></section>
  <section class="view" id="view-devices" data-view="devices" hidden></section>
</main>

<nav class="tabs" aria-label="Sections">
  <div class="tabs-in">
    <button class="tab" data-tab="today"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>Today</button>
    <button class="tab" data-tab="sleep"><svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>Sleep</button>
    <button class="tab" data-tab="trends"><svg viewBox="0 0 24 24"><path d="M3 17l5-5 4 3 8-8"/><path d="M15 7h5v5"/></svg>Trends</button>
    <button class="tab" data-tab="devices"><svg viewBox="0 0 24 24"><rect x="7" y="3" width="10" height="18" rx="3"/><path d="M10 8h4"/></svg>Devices</button>
  </div>
</nav>

<script>
(() => {
"use strict";
// ------------------------------------------------------------------ state
const SERIES = [
  { key: "whoop:ours", device: "whoop", source: "ours", name: "WHOOP · Ours", color: "var(--whoop)", dash: false },
  { key: "whoop:official", device: "whoop", source: "official", name: "WHOOP · Official", color: "var(--whoop)", dash: true },
  { key: "oura:ours", device: "oura", source: "ours", name: "Oura · Ours", color: "var(--oura)", dash: false },
  { key: "oura:official", device: "oura", source: "official", name: "Oura · Official", color: "var(--oura)", dash: true },
];
const store = {
  get(k, d) { try { const v = localStorage.getItem("loop:" + k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("loop:" + k, JSON.stringify(v)); } catch {} },
};
const S = {
  filters: store.get("filters", { whoop: true, oura: true, ours: true, official: true }),
  view: "today", day: null, range: store.get("range", 30), metric: store.get("metric", "recovery"),
  pair: store.get("pair", "whoop_ours_vs_official"), cmpMetric: "hrv",
  data: null, compare: null, readiness: null, nights: {}, demo: null,
  journal: { entries: [], presets: [] }, patterns: null, alerts: [], summaries: {},
};
const DEMO_EL = document.getElementById("demo-data");
if (DEMO_EL) S.demo = JSON.parse(DEMO_EL.textContent);

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const visible = () => SERIES.filter(s => S.filters[s.device] && S.filters[s.source] && hasSeries(s.key));
const hasSeries = key => !!(S.idx && S.idx[key] && Object.keys(S.idx[key]).length);
const row = (key, day) => (S.idx[key] || {})[day];
const fmt = (v, d = 0) => v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(d);
const hm = min => min == null ? "—" : `${Math.floor(min / 60)}h ${String(Math.round(min % 60)).padStart(2, "0")}m`;
const clock = ts => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "—";
const dayLabel = d => new Date(d + "T12:00:00").toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
const shortDay = d => new Date(d + "T12:00:00").toLocaleDateString([], { month: "short", day: "numeric" });
const addDays = (d, n) => { const x = new Date(d + "T12:00:00"); x.setDate(x.getDate() + n); return x.toISOString().slice(0, 10); };
const ago = ts => { if (!ts) return "never"; const s = Date.now() / 1000 - ts; if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago"; if (s < 86400) return Math.round(s / 3600) + " h ago"; return Math.round(s / 86400) + " days ago"; };
const swatch = s => `<span class="swatch${s.dash ? " dash" : ""}" style="${s.dash ? "border-color" : "background"}:${s.color}"></span>`;
function tier(rec) {
  if (rec == null) return "";
  const [c, l] = rec >= 67 ? ["var(--good)", "High"] : rec >= 34 ? ["var(--warn)", "Moderate"] : ["var(--bad)", "Low"];
  return `<span class="tier"><i style="background:${c}"></i>${l} recovery</span>`;
}

// ------------------------------------------------------------------ data
async function api(path) {
  if (S.demo) {
    if (path.startsWith("/api/overview")) return S.demo.overview;
    if (path.startsWith("/api/compare")) return S.demo.compare;
    if (path.startsWith("/api/readiness")) return S.demo.readiness;
    if (path.startsWith("/api/night")) { const q = new URLSearchParams(path.split("?")[1]); return S.demo.nights[q.get("device") + ":" + q.get("day")] || { hypnogram: [], hr: [] }; }
    if (path.startsWith("/api/live")) return demoLive();
    if (path.startsWith("/api/summary")) { const q = new URLSearchParams(path.split("?")[1]); return S.demo.summaries[q.get("day")] || { headline: "Last night isn't scored yet", items: [] }; }
    if (path.startsWith("/api/alerts")) return S.demo.alerts;
    if (path.startsWith("/api/journal")) return S.demo.journal;
    if (path.startsWith("/api/patterns")) return S.demo.patterns;
    return {};
  }
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(path + " → " + r.status);
  return r.json();
}
function demoLive() {
  const now = Math.floor(Date.now() / 1000), out = [];
  for (let t = now - 88; t <= now; t += 2) out.push({ ts: t, bpm: Math.round(61 + 3 * Math.sin(t / 9) + Math.sin(t / 3)) });
  return { whoop: out, oura: [] };
}
function index(daily) {
  const idx = {};
  for (const r of daily) (idx[r.device + ":" + r.source] ||= {})[r.day] = r;
  S.idx = idx;
  const days = [...new Set(daily.map(r => r.day))].sort();
  S.days = days;
  if (!S.day || !days.includes(S.day)) S.day = days[days.length - 1] || new Date().toISOString().slice(0, 10);
}
async function load() {
  S.data = await api("/api/overview?days=120");
  index(S.data.daily);
  render();
  Promise.all([api("/api/compare?days=60"), api("/api/readiness")]).then(([c, r]) => { S.compare = c; S.readiness = r; if (S.view === "compare") render(); }).catch(() => {});
  Promise.all([api("/api/journal?days=120"), api("/api/patterns"), api("/api/alerts")]).then(([j, p, a]) => {
    S.journal = j; S.patterns = p; S.alerts = a.alerts || []; if (S.view === "today" || S.view === "trends") render();
  }).catch(() => {});
}
async function setTag(day, tag, on) {
  const list = S.journal.entries;
  if (on) list.push({ day, tag }); else S.journal.entries = list.filter(e => !(e.day === day && e.tag === tag));
  if (S.demo) return;
  try {
    await fetch("/api/journal", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ day, tag, on }) });
    S.patterns = await api("/api/patterns");
  } catch {}
}

// ------------------------------------------------------------------ charts
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs = {}, parent) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); parent && parent.appendChild(e); return e; }
function niceTicks(lo, hi, n = 4) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const step0 = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(step0)), norm = step0 / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const t = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) t.push(+v.toFixed(6));
  return { ticks: t, lo: Math.min(lo, t[0]), hi: Math.max(hi, t[t.length - 1]) };
}

function ringSVG(arcs, max, center, sub) {
  const size = 160, c = size / 2, svg = el("svg", { viewBox: `0 0 ${size} ${size}`, role: "img" });
  const n = Math.max(arcs.length, 1), w = n > 2 ? 7 : n > 1 ? 10 : 13, gap = 3;
  el("title", {}, svg).textContent = `${center} ${sub || ""}`;
  arcs.forEach((a, i) => {
    const r = c - 8 - i * (w + gap), circ = 2 * Math.PI * r;
    el("circle", { cx: c, cy: c, r, fill: "none", stroke: "var(--track)", "stroke-width": w }, svg);
    if (a.value != null) {
      const frac = Math.max(0.01, Math.min(1, a.value / max));
      el("circle", { cx: c, cy: c, r, fill: "none", stroke: a.color, "stroke-width": w, "stroke-linecap": "round",
        "stroke-dasharray": `${frac * circ} ${circ}`, transform: `rotate(-90 ${c} ${c})`, opacity: a.dash ? .55 : 1 }, svg);
    }
  });
  const t = el("text", { x: c, y: c + 4, "text-anchor": "middle", fill: "var(--ink)", style: "font: 700 30px var(--f-display); fill: var(--ink)" }, svg);
  t.textContent = center;
  if (sub) { const s = el("text", { x: c, y: c + 24, "text-anchor": "middle", style: "font: 11px var(--f-mono); fill: var(--ink-3)" }, svg); s.textContent = sub; }
  return svg;
}

function lineChart(host, series, { yFmt = v => v, height = 230, xIsTime = false, yMin = null, yMax = null } = {}) {
  host.innerHTML = "";
  host.classList.add("chart");
  const pts = series.flatMap(s => s.points);
  if (!pts.length) { host.innerHTML = `<div class="empty">No data for this range yet.</div>`; return; }
  const W = Math.max(320, host.clientWidth || 640), H = height, m = { l: 44, r: 14, t: 10, b: 26 };
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const yt = niceTicks(yMin ?? Math.min(...ys), yMax ?? Math.max(...ys));
  const X = v => m.l + (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0)) * (W - m.l - m.r);
  const Y = v => m.t + (1 - (v - yt.lo) / (yt.hi - yt.lo)) * (H - m.t - m.b);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, host);
  for (const t of yt.ticks) {
    el("line", { x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), stroke: "var(--line)", "stroke-width": 1 }, svg);
    el("text", { x: m.l - 8, y: Y(t) + 4, "text-anchor": "end" }, svg).textContent = yFmt(t);
  }
  const nx = Math.min(6, Math.max(2, Math.floor((W - m.l - m.r) / (xIsTime && x1 - x0 < 600 ? 150 : 90))));
  for (let i = 0; i <= nx; i++) {
    const v = x0 + (x1 - x0) * i / nx;
    const lbl = xIsTime ? new Date(v * 1000).toLocaleTimeString([], x1 - x0 < 7200 ? { hour: "numeric", minute: "2-digit", second: x1 - x0 < 600 ? "2-digit" : undefined } : { hour: "numeric" })
      : shortDay(new Date(v).toISOString().slice(0, 10));
    el("text", { x: X(v), y: H - 6, "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle" }, svg).textContent = lbl;
  }
  for (const s of series) {
    if (!s.points.length) continue;
    let d = "", prev = null;
    for (const p of s.points) { const gapBreak = prev && (p[0] - prev[0]) > s.gap; d += (d && !gapBreak ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1); prev = p; }
    el("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": s.dash ? "5 4" : "none" }, svg);
    const last = s.points[s.points.length - 1];
    el("circle", { cx: X(last[0]), cy: Y(last[1]), r: 4, fill: s.color, stroke: "var(--panel)", "stroke-width": 2 }, svg);
  }
  // hover crosshair + tooltip
  const cross = el("line", { y1: m.t, y2: H - m.b, stroke: "var(--ink-3)", "stroke-width": 1, "stroke-dasharray": "2 3", opacity: 0 }, svg);
  const dots = series.map(s => el("circle", { r: 4.5, fill: s.color, stroke: "var(--panel)", "stroke-width": 2, opacity: 0 }, svg));
  const tip = document.createElement("div"); tip.className = "tip"; host.appendChild(tip);
  const hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" }, svg);
  const allX = [...new Set(xs)].sort((a, b) => a - b);
  function move(ev) {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width;
    const xv = x0 + (px - m.l) / (W - m.l - m.r) * (x1 - x0);
    let best = allX[0]; for (const v of allX) if (Math.abs(v - xv) < Math.abs(best - xv)) best = v;
    cross.setAttribute("x1", X(best)); cross.setAttribute("x2", X(best)); cross.setAttribute("opacity", 1);
    let rows = "";
    series.forEach((s, i) => {
      const p = s.points.find(q => q[0] === best);
      if (p) { dots[i].setAttribute("cx", X(p[0])); dots[i].setAttribute("cy", Y(p[1])); dots[i].setAttribute("opacity", 1);
        rows += `<div class="row"><span>${swatch(s)}${esc(s.name)}</span><b class="num">${esc(yFmt(p[1], true))}</b></div>`; }
      else dots[i].setAttribute("opacity", 0);
    });
    const head = xIsTime ? new Date(best * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : dayLabel(new Date(best).toISOString().slice(0, 10));
    tip.innerHTML = `<div class="eyebrow" style="margin-bottom:4px">${esc(head)}</div>${rows}`;
    tip.style.display = "block";
    const left = (X(best) / W) * r.width;
    tip.style.left = Math.min(Math.max(0, left - tip.offsetWidth / 2), r.width - tip.offsetWidth) + "px";
    tip.style.top = "-6px"; tip.style.transform = "translateY(-100%)";
  }
  function leave() { tip.style.display = "none"; cross.setAttribute("opacity", 0); dots.forEach(d => d.setAttribute("opacity", 0)); }
  hit.addEventListener("pointermove", move); hit.addEventListener("pointerdown", move); hit.addEventListener("pointerleave", leave);
  const lg = document.createElement("div"); lg.className = "legend";
  lg.innerHTML = series.filter(s => s.points.length).map(s => `<span>${swatch(s)}${esc(s.name)}</span>`).join("");
  host.appendChild(lg);
}

function hypnogramSVG(host, hyp, hr) {
  host.innerHTML = ""; host.classList.add("chart");
  if (!hyp.length) { host.innerHTML = `<div class="empty">No overnight data from our collector for this night.</div>`; return; }
  const W = Math.max(320, host.clientWidth || 640), H = 200, m = { l: 52, r: 12, t: 8, b: 24 };
  const levels = ["awake", "rem", "light", "deep"], bandH = 22;
  const t0 = hyp[0][0], t1 = hyp[hyp.length - 1][0] + 300;
  const X = t => m.l + (t - t0) / (t1 - t0) * (W - m.l - m.r);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, host);
  el("title", {}, svg).textContent = "Estimated sleep stages and heart rate";
  levels.forEach((lv, i) => {
    const y = m.t + i * bandH;
    el("text", { x: m.l - 8, y: y + 15, "text-anchor": "end" }, svg).textContent = lv === "rem" ? "REM" : lv[0].toUpperCase() + lv.slice(1);
    el("line", { x1: m.l, x2: W - m.r, y1: y + bandH, y2: y + bandH, stroke: "var(--line)" }, svg);
  });
  const cls = { awake: "var(--ink-3)", light: "color-mix(in srgb, var(--accent) 40%, var(--panel))", rem: "color-mix(in srgb, var(--accent) 70%, var(--panel))", deep: "var(--accent)" };
  for (const [t, s] of hyp) {
    const i = levels.indexOf(s);
    el("rect", { x: X(t), y: m.t + i * bandH + 3, width: Math.max(1, X(t + 300) - X(t) - 1), height: bandH - 6, rx: 2, fill: cls[s] }, svg);
  }
  // heart-rate trace underneath
  const top = m.t + levels.length * bandH + 12, bot = H - m.b;
  const pts = hr.filter(p => p.t >= t0 && p.t <= t1);
  if (pts.length > 1) {
    const lo = Math.min(...pts.map(p => p.bpm)), hi = Math.max(...pts.map(p => p.bpm));
    const Y = v => bot - (v - lo) / Math.max(hi - lo, 1) * (bot - top);
    el("path", { d: pts.map((p, i) => (i ? "L" : "M") + X(p.t).toFixed(1) + " " + Y(p.bpm).toFixed(1)).join(""), fill: "none", stroke: "var(--whoop)", "stroke-width": 1.6 }, svg);
    el("text", { x: m.l - 8, y: Y(lo) + 4, "text-anchor": "end" }, svg).textContent = Math.round(lo);
    el("text", { x: m.l - 8, y: Y(hi) + 4, "text-anchor": "end" }, svg).textContent = Math.round(hi);
  }
  const span = t1 - t0, step = span > 6 * 3600 ? 7200 : 3600;
  for (let t = Math.ceil(t0 / step) * step; t < t1; t += step)
    el("text", { x: X(t), y: H - 6, "text-anchor": "middle" }, svg).textContent = new Date(t * 1000).toLocaleTimeString([], { hour: "numeric" });
  const lg = document.createElement("div"); lg.className = "legend";
  lg.innerHTML = `<span><span class="swatch st-deep"></span>Deep</span><span><span class="swatch st-rem"></span>REM</span><span><span class="swatch st-light"></span>Light</span><span><span class="swatch st-awake"></span>Awake</span><span>${swatch(SERIES[0])}Heart rate (bpm)</span>`;
  host.appendChild(lg);
}

function scatterSVG(host, pairs, a, b, unitFmt) {
  host.innerHTML = ""; host.classList.add("chart");
  if (!pairs.length) { host.innerHTML = `<div class="empty">No nights where both sources have this metric yet.</div>`; return; }
  const W = Math.max(300, Math.min(host.clientWidth || 420, 520)), H = W * .78, m = { l: 44, r: 12, t: 10, b: 34 };
  const vals = pairs.flatMap(p => [p[1], p[2]]);
  const t = niceTicks(Math.min(...vals), Math.max(...vals));
  const X = v => m.l + (v - t.lo) / (t.hi - t.lo) * (W - m.l - m.r), Y = v => H - m.b - (v - t.lo) / (t.hi - t.lo) * (H - m.t - m.b);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, host);
  for (const v of t.ticks) {
    el("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: "var(--line)" }, svg);
    el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, svg).textContent = unitFmt(v);
    el("text", { x: X(v), y: H - m.b + 16, "text-anchor": "middle" }, svg).textContent = unitFmt(v);
  }
  el("line", { x1: X(t.lo), y1: Y(t.lo), x2: X(t.hi), y2: Y(t.hi), stroke: "var(--ink-3)", "stroke-dasharray": "4 4" }, svg);
  const tip = document.createElement("div"); tip.className = "tip"; host.appendChild(tip);
  for (const p of pairs) {
    const c = el("circle", { cx: X(p[2]), cy: Y(p[1]), r: 4.5, fill: a.color, "fill-opacity": .75, stroke: "var(--panel)", "stroke-width": 1.5 }, svg);
    const hitc = el("circle", { cx: X(p[2]), cy: Y(p[1]), r: 12, fill: "transparent" }, svg);
    const show = () => { tip.innerHTML = `<div class="eyebrow" style="margin-bottom:4px">${esc(dayLabel(p[0]))}</div>
      <div class="row"><span>${swatch(a)}${esc(a.name)}</span><b class="num">${esc(unitFmt(p[1], true))}</b></div>
      <div class="row"><span>${swatch(b)}${esc(b.name)}</span><b class="num">${esc(unitFmt(p[2], true))}</b></div>`;
      tip.style.display = "block"; const r = svg.getBoundingClientRect();
      tip.style.left = Math.min(Math.max(0, X(p[2]) / W * r.width - 80), r.width - tip.offsetWidth) + "px";
      tip.style.top = (Y(p[1]) / H * r.height - 12) + "px"; tip.style.transform = "translateY(-100%)"; c.setAttribute("r", 6); };
    const hide = () => { tip.style.display = "none"; c.setAttribute("r", 4.5); };
    hitc.addEventListener("pointerenter", show); hitc.addEventListener("pointerdown", show); hitc.addEventListener("pointerleave", hide);
  }
  el("text", { x: W - m.r, y: H - 2, "text-anchor": "end" }, svg).textContent = b.name + " →";
  el("text", { x: m.l, y: m.t + 2, "text-anchor": "start", dy: "-0.2em" }, svg).textContent = "↑ " + a.name;
  const lg = document.createElement("div"); lg.className = "legend";
  lg.innerHTML = `<span>Dashed line: perfect agreement. Each dot is one night.</span>`;
  host.appendChild(lg);
}

// ------------------------------------------------------------------ metric definitions
const METRICS = {
  recovery: { label: "Recovery", unit: "%", d: 0, fmt: v => fmt(v) + "%" },
  hrv: { label: "HRV", unit: "ms", d: 0, fmt: v => fmt(v) + " ms" },
  rhr: { label: "Resting HR", unit: "bpm", d: 0, fmt: v => fmt(v) + " bpm" },
  sleep_min: { label: "Sleep", unit: "", d: 0, fmt: v => hm(v) },
  strain: { label: "Strain", unit: "", d: 1, fmt: v => fmt(v, 1) },
  resp_rate: { label: "Resp. rate", unit: "rpm", d: 1, fmt: v => fmt(v, 1) + " rpm" },
  spo2: { label: "SpO₂", unit: "%", d: 1, fmt: v => fmt(v, 1) + "%" },
  bedtime: { label: "Bedtime", fmt: v => clock(v) },
  waketime: { label: "Wake time", fmt: v => clock(v) },
};
const NOT_DECODED = { "whoop:ours": ["spo2", "skin_temp"], "oura:ours": ["resp_rate", "spo2", "skin_temp"] };
function baseline(key, day, field, n = 30) {
  const vals = []; for (let i = 1; i <= n; i++) { const r = row(key, addDays(day, -i)); if (r && r[field] != null) vals.push(r[field]); }
  return vals.length >= 5 ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}

// ------------------------------------------------------------------ product series
// The main app shows one number per metric: our own WHOOP score, optionally filled from
// WHOOP's official value (tagged) when ours is missing. Comparisons live on their own screen.
const OURS = "whoop:ours", OFFICIAL = "whoop:official";
S.prefs = Object.assign({ hideEmpty: true, fallback: true }, store.get("prefs", {}));
if (S.demo) try { S.demo.overview.status.whoop.bluetooth.last_sample = Math.floor(Date.now() / 1000) - 40; } catch {}
function val(day, f) {
  const a = row(OURS, day), b = row(OFFICIAL, day);
  if (a && a[f] != null) return { v: a[f], src: "ours" };
  if (S.prefs.fallback && b && b[f] != null) return { v: b[f], src: "official" };
  return { v: null, src: null };
}
const anyData = f => S.days.some(d => val(d, f).v != null);
const shown = f => !S.prefs.hideEmpty || anyData(f);
const srcTag = x => x.src === "official" ? ` <span class="src" title="Filled from WHOOP's official value because ours is missing">official</span>` : "";
function avgBefore(day, f, n = 30) {
  const vals = []; for (let i = 1; i <= n; i++) { const x = val(addDays(day, -i), f).v; if (x != null) vals.push(x); }
  return vals.length >= 5 ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}
const LABELS = { recovery: "Recovery", strain: "Strain", sleep_perf: "Sleep", hrv: "HRV", rhr: "Resting HR", sleep_min: "Sleep", resp_rate: "Resp. rate",
  spo2: "SpO₂", skin_temp: "Skin temp", calories: "Calories", steps: "Steps", efficiency: "Efficiency", in_bed_min: "In bed", awake_min: "Awake",
  bedtime: "Bedtime", waketime: "Wake time", stages: "Sleep stages" };
function hiddenNote(fields) {
  if (!S.prefs.hideEmpty) return "";
  const gone = fields.filter(f => !anyData(f));
  if (!gone.length) return "";
  return `<p class="hidden-note">Hidden, no data: ${gone.map(f => esc(LABELS[f] || f)).join(", ")}. <button class="linkbtn" data-go="devices">Display settings</button></p>`;
}
// How much data is behind a day's scores. Strain only counts minutes the strap was heard.
function coverage(day) {
  const r = row(OURS, day), ex = (r && r.extra) || {}, latest = day === S.days[S.days.length - 1];
  let total = 1440;
  if (latest) { const mid = new Date(day + "T00:00:00").getTime() / 1000; total = Math.max(60, Math.min(1440, Math.round(((S.data.generated || Date.now() / 1000) - mid) / 60))); }
  const sm = ex.strain_coverage_min, nm = ex.coverage_min, bed = r && r.in_bed_min;
  return { latest, estimated: ex.strain_estimated_min || 0,
    strain: sm == null ? null : { min: Math.min(sm, total), total, partial: sm / total < 0.7 },
    night: nm == null || !bed ? null : { min: Math.min(nm, bed), total: bed, partial: nm / bed < 0.8 } };
}
// Tonight's sleep target: base need, plus a quarter of the last week's debt (max 1 h), plus extra after a hard day.
function sleepPlan() {
  const end = S.days[S.days.length - 1], need = ((S.data.profile || {}).sleep_need_h || 8) * 60;
  let debt = 0, nights = 0; const wakes = [], effs = [];
  for (let i = 0; i < 14; i++) { const d = addDays(end, -i), s = val(d, "sleep_min").v, w = val(d, "waketime").v, e = val(d, "efficiency").v;
    if (i < 7 && s != null) { debt += Math.max(0, need - s); nights++; }
    if (w) { const t = new Date(w * 1000); wakes.push(t.getHours() * 60 + t.getMinutes()); }
    if (e) effs.push(e); }
  if (nights < 3 || wakes.length < 3) return null;
  wakes.sort((a, b) => a - b);
  const wake = wakes[Math.floor(wakes.length / 2)], payback = Math.min(60, Math.round(debt * 0.25 / 5) * 5);
  const st = val(end, "strain"), cov = coverage(end).strain;
  const extra = st.v == null ? 0 : st.v >= 14 ? 30 : st.v >= 10 ? 15 : 0;
  const target = need + payback + extra;
  const eff = Math.max(85, Math.min(97, effs.length ? effs.reduce((a, b) => a + b, 0) / effs.length : 92));
  const inBed = Math.round(target / (eff / 100) / 5) * 5;
  const bed = ((wake - inBed) % 1440 + 1440) % 1440;
  return { need, debt: Math.round(debt), nights, payback, extra, strain: st.v, strainPartial: st.src === "ours" && cov && cov.partial, target, inBed, bed, wake };
}
const clockMin = m => new Date(2000, 0, 1, Math.floor(m / 60), m % 60).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
// Workouts: detected from heart rate by the server, plus sessions you log yourself.
// WHOOP's own records fill in (tagged "official") only where we have nothing for that time.
const ZONES = ["50–60%", "60–70%", "70–80%", "80–90%", "90%+"];
const localDay = ts => { const d = new Date(ts * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
function zoneBar(z) {
  const tot = z.reduce((a, b) => a + b, 0); if (!tot) return "";
  return `<div class="stackbar zones" role="img" aria-label="Minutes in each heart-rate zone">${z.map((m, k) => m ? `<div class="z${k + 1}" style="width:${m / tot * 100}%" title="Zone ${k + 1} (${ZONES[k]} of max): ${m} min"></div>` : "").join("")}</div>`;
}
function workoutsFor(day) {
  const all = (S.data.workouts || []).filter(w => w.device === "whoop" && (w.day || localDay(w.start)) === day);
  const mine = all.filter(w => w.source !== "official");
  const extra = S.prefs.fallback ? all.filter(w => w.source === "official" && !mine.some(m => m.start < w.end && w.start < m.end)) : [];
  return mine.concat(extra).sort((a, b) => a.start - b.start);
}
async function logWorkout(day, type, start, minutes) {
  if (S.demo) { const t = new Date(`${day}T${start}:00`).getTime() / 1000;
    S.data.workouts.push({ device: "whoop", source: "logged", id: "l" + Date.now(), day, start: t, end: t + minutes * 60, sport: type }); return; }
  const r = await fetch("/api/workouts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ day, type, start, minutes }) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "The server refused that workout");
  await load();
}
async function removeWorkout(id) {
  if (S.demo) { S.data.workouts = S.data.workouts.filter(w => w.id !== id); return; }
  await fetch("/api/workouts/" + encodeURIComponent(id), { method: "DELETE" }); await load();
}
function renderWorkouts(day) {
  const host = $("#workoutPanel"); if (!host) return;
  const list = workoutsFor(day), ex = (row(OURS, day) || {}).extra || {}, z = (ex.zone_minutes || []).slice(1), zt = z.reduce((a, b) => a + b, 0);
  host.innerHTML = `<div class="panel-h"><h2>Workouts</h2><span class="muted" style="font-size:12px">${esc(shortDay(day))}</span></div>
    ${list.length ? list.map(w => `<div class="wk"><div class="wk-top"><span><b style="text-transform:capitalize">${esc(w.sport || "Workout")}</b> <span class="src">${esc(w.source)}</span></span>
      ${w.source === "logged" ? `<button class="linkbtn" data-del="${esc(w.id)}">Remove</button>` : ""}</div>
      <div class="muted num" style="font-size:12px">${clock(w.start)} · ${Math.round((w.end - w.start) / 60)} min${w.avg_hr ? ` · avg ${Math.round(w.avg_hr)} bpm` : ""}${w.strain != null ? ` · strain ${fmt(w.strain, 1)}` : w.source === "logged" ? " · no heart rate, counted at a typical effort" : ""}</div>
      ${w.zones ? zoneBar(w.zones) : ""}</div>`).join("") : `<div class="empty" style="padding:14px 0">No workouts for this day.</div>`}
    ${zt ? `<div class="eyebrow" style="margin:14px 0 6px">Heart-rate zones, whole day</div>${zoneBar(z)}
      <div class="legend">${z.map((m, k) => `<span><span class="swatch z${k + 1}"></span>Z${k + 1} <span class="num">${m}m</span></span>`).join("")}</div>` : ""}
    <form id="wkForm" class="wk-form">
      <select id="wkType" aria-label="Workout type">${["Strength", "Run", "Walk", "Cycle", "Sport", "Other"].map(t => `<option>${t}</option>`).join("")}</select>
      <input id="wkStart" type="time" value="18:00" aria-label="Start time" required>
      <input id="wkMin" type="number" min="5" max="600" step="5" value="45" aria-label="Minutes" required>
      <button class="btn" type="submit">Log workout</button></form>
    <p class="muted" id="wkMsg" style="font-size:12px;margin:10px 0 0">Logged sessions count toward Strain even when the strap wasn't heard, which is common with lifting or away from home.</p>`;
  host.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => { b.disabled = true; await removeWorkout(b.dataset.del); if (S.demo) renderWorkouts(day); });
  $("#wkForm", host).onsubmit = async e => { e.preventDefault(); const min = +$("#wkMin", host).value, start = $("#wkStart", host).value; if (!min || !start) return;
    const btn = $("#wkForm button", host); btn.disabled = true;
    try { await logWorkout(day, $("#wkType", host).value, start, min); if (S.demo) renderWorkouts(day); }
    catch (err) { btn.disabled = false; const m = $("#wkMsg", host); m.textContent = "Couldn't log that workout: " + err.message; m.style.color = "var(--bad)"; } };
}
const go = v => { S.view = v; try { history.replaceState(null, "", "#" + v); } catch {} window.scrollTo(0, 0); render(); };
const ONE = { name: "", color: "var(--accent)", dash: false };

// ------------------------------------------------------------------ views
function dayBar(i, eyebrow, day) {
  return `<div class="daybar">
    <button class="iconbtn" id="prevDay" aria-label="Previous day" ${i <= 0 ? "disabled" : ""}>‹</button>
    <div><div class="eyebrow">${eyebrow}</div><h2>${esc(dayLabel(day))}</h2></div>
    <button class="iconbtn" id="nextDay" aria-label="Next day" ${i >= S.days.length - 1 ? "disabled" : ""}>›</button></div>`;
}
function wireDayBar(root, i) {
  $("#prevDay", root).onclick = () => { S.day = S.days[i - 1]; render(); };
  $("#nextDay", root).onclick = () => { S.day = S.days[i + 1]; render(); };
}

function viewToday(root) {
  const day = S.day, i = S.days.indexOf(day);
  const rec = val(day, "recovery");
  const isLatest = i === S.days.length - 1;
  const ringDefs = [["recovery", 100, v => Math.round(v) + "%", null, v => v >= 67 ? "var(--good)" : v >= 34 ? "var(--warn)" : "var(--bad)"],
    ["strain", 21, v => Number(v).toFixed(1), "of 21", () => "var(--accent)"],
    ["sleep_perf", 100, v => Math.round(v) + "%", "of need", () => "var(--sleep)"]].filter(r => shown(r[0]));
  const vitalDefs = [["hrv", v => fmt(v) + " ms", 1], ["rhr", v => fmt(v) + " bpm", -1], ["resp_rate", v => fmt(v, 1) + " rpm", 0],
    ["spo2", v => fmt(v, 1) + "%", 0], ["skin_temp", v => (v > 20 ? fmt(v, 1) + " °C" : (v >= 0 ? "+" : "") + fmt(v, 2) + "°"), 0]];
  const vit = vitalDefs.filter(d => shown(d[0]));
  const cal = val(day, "calories"), steps = val(day, "steps"), slp = val(day, "sleep_min"), bed = val(day, "bedtime"), wake = val(day, "waketime");
  const cov = coverage(day), plan = isLatest && shown("sleep_min") ? sleepPlan() : null;
  const lastSeen = (((S.data.status || {}).whoop || {}).bluetooth || {}).last_sample;
  const calib = (row(OURS, day) || {}).calibrating;

  root.innerHTML = `
    ${isLatest && S.alerts.length ? `<div class="alerts">${S.alerts.map(a => `<div class="alert ${a.level}" role="status">
      <span class="alert-mark" aria-hidden="true">${a.level === "info" ? "i" : "!"}</span>
      <div><b>${esc(a.title)}</b><div>${esc(a.body)}</div></div></div>`).join("")}</div>` : ""}
    <div class="panel">
      <div class="panel-h">${dayBar(i, "Day", day)}${tier(rec.v)}</div>
      <div class="brief" id="brief"></div>
      ${ringDefs.length ? `<div class="rings" id="rings" style="grid-template-columns:repeat(${ringDefs.length},minmax(0,1fr))"></div>` : `<div class="empty">No scores yet for this day.</div>`}
      ${calib ? `<p class="hidden-note">Recovery is still calibrating: it needs 7 nights to learn your baseline.</p>` : ""}
      ${isLatest ? `<p class="hidden-note">Strap last heard ${ago(lastSeen)}.${cov.strain && cov.strain.partial ? " Strain is partial: it only counts the time the strap was in range." : ""}</p>` : cov.strain && cov.strain.partial ? `<p class="hidden-note">Strain is partial: the strap was in range for ${hm(cov.strain.min)} of this day.</p>` : ""}
      ${hiddenNote(["recovery", "strain", "sleep_perf"])}
    </div>
    ${plan ? `<div class="panel tonight">
      <div class="panel-h"><h2>Tonight</h2><span class="muted" style="font-size:12px">to wake at your usual ${clockMin(plan.wake)}</span></div>
      <div class="tonight-main"><div><div class="eyebrow">In bed by</div><div class="bigtime num">${clockMin(plan.bed)}</div></div>
        <div><div class="eyebrow">Sleep target</div><div class="bigtime num">${hm(plan.target)}</div></div></div>
      <dl class="kv" style="margin:12px 0 0"><dt>Your nightly need</dt><dd class="num">${hm(plan.need)}</dd>
        <dt>Sleep debt, last ${plan.nights} nights: ${hm(plan.debt)}</dt><dd class="num">+${plan.payback} min</dd>
        <dt>Today's strain${plan.strain == null ? "" : " " + fmt(plan.strain, 1)}${plan.strainPartial ? " (partial)" : ""}</dt><dd class="num">+${plan.extra} min</dd></dl>
    </div>` : ""}
    <div class="grid2">
      <div class="panel">
        <div class="panel-h"><h2>Vitals</h2><span class="muted" style="font-size:12px">vs your 30-day average</span></div>
        ${vit.length ? `<table class="vitals"><tbody>${vit.map(([f, ff, good]) => {
          const x = val(day, f); if (x.v == null) return `<tr><td>${LABELS[f]}</td><td><span class="na">no reading</span></td></tr>`;
          const b = avgBefore(day, f); let delta = "";
          if (b != null && good !== 0) { const dlt = x.v - b, pct = Math.round(dlt / b * 100);
            if (Math.abs(pct) >= 1) delta = `<span class="delta ${dlt * good > 0 ? "up" : "down"}">${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}%</span>`; }
          return `<tr><td>${LABELS[f]}${srcTag(x)}</td><td class="num">${esc(ff(x.v))}${delta}</td></tr>`; }).join("")}</tbody></table>` : `<div class="empty">No vitals yet.</div>`}
        ${hiddenNote(vitalDefs.map(d => d[0]))}
      </div>
      <div class="panel">
        <div class="panel-h"><h2>Live heart rate</h2><span class="muted" id="liveWhen" style="font-size:12px"></span></div>
        <div class="live"><span class="pulse off" id="livePulse"></span><span class="bpm num" id="liveBpm">—</span><span class="muted">bpm</span></div>
        <div id="liveChart" style="margin-top:8px"></div>
        <p class="note" id="liveNote" style="margin:10px 0 0">Shows when the strap is within Bluetooth range of the Pi or your laptop.</p>
      </div>
      <div class="panel">
        <div class="panel-h"><h2>Night and day</h2></div>
        <div class="stats">
          ${shown("sleep_min") ? `<div class="stat"><div class="k">Sleep${srcTag(slp)}</div><div class="v num">${hm(slp.v)}</div><div class="s">${bed.v ? `${clock(bed.v)} → ${clock(wake.v)}` : "No sleep recorded"}</div></div>` : ""}
          ${shown("calories") ? `<div class="stat"><div class="k">Calories${srcTag(cal)}</div><div class="v num">${cal.v == null ? "—" : Math.round(cal.v).toLocaleString()}</div><div class="s">kcal burned</div></div>` : ""}
          ${shown("steps") ? `<div class="stat"><div class="k">Steps${srcTag(steps)}</div><div class="v num">${steps.v == null ? "—" : Math.round(steps.v).toLocaleString()}</div><div class="s">today</div></div>` : ""}
        </div>
        ${hiddenNote(["sleep_min", "calories", "steps"])}
      </div>
      <div class="panel" id="workoutPanel"></div>
      <div class="panel" id="journalPanel"></div>
    </div>`;
  renderBrief(day);
  renderJournal(day);
  renderWorkouts(day);
  const rings = $("#rings", root);
  if (rings) for (const [f, max, cf, sub, col] of ringDefs) {
    const x = val(day, f), box = document.createElement("div"); box.className = "ring";
    box.appendChild(ringSVG([{ value: x.v, color: x.v == null ? "var(--track)" : col(x.v), dash: false }], max, x.v == null ? "—" : cf(x.v), sub));
    const lb = document.createElement("div"); lb.className = "label"; lb.innerHTML = LABELS[f] + srcTag(x);
    box.appendChild(lb);
    const c = x.src !== "ours" ? null : f === "strain" ? cov.strain : cov.night;
    if (c) { const cv = document.createElement("div"); cv.className = "cov";
      const sh = m => { m = Math.round(m); return m % 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m / 60}h`; };
      cv.innerHTML = `${sh(c.min)} of ${sh(c.total)}${f === "strain" && cov.latest ? " so far" : ""}${f === "strain" && cov.estimated ? ` + ${cov.estimated}m logged` : ""}${c.partial ? ` <span class="src warn">partial</span>` : ""}`; box.appendChild(cv); }
    rings.appendChild(box);
  }
  wireDayBar(root, i);
  startLive();
}

async function renderBrief(day) {
  if (!S.summaries[day]) { try { S.summaries[day] = await api(`/api/summary?day=${day}`); } catch { return; } }
  const host = $("#brief"), s = S.summaries[day];
  if (!host || S.day !== day || !s || !s.items || !s.items.length) return;
  host.innerHTML = `<div class="eyebrow">Morning brief</div>
    <p class="brief-head">${esc(s.headline)}</p><ul>${s.items.map(t => `<li>${esc(t)}</li>`).join("")}</ul>`;
}

function renderJournal(day) {
  const host = $("#journalPanel"); if (!host) return;
  const on = new Set(S.journal.entries.filter(e => e.day === day).map(e => e.tag));
  const tags = [...new Set([...(S.journal.presets || []), ...S.journal.entries.map(e => e.tag)])];
  host.innerHTML = `<div class="panel-h"><h2>Journal</h2><span class="muted" style="font-size:12px">What you did on ${esc(shortDay(day))}</span></div>
    <div class="filters" role="group" aria-label="Journal tags">${tags.map(t => `<button class="chip" data-tag="${esc(t)}" aria-pressed="${on.has(t)}">${esc(t)}</button>`).join("")}</div>
    <form id="tagForm" style="display:flex;gap:8px;margin-top:10px">
      <input id="tagInput" maxlength="40" placeholder="Add your own tag" aria-label="New tag"
        style="flex:1;min-width:0;height:34px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:0 10px;font:inherit;font-size:13px">
      <button class="btn" type="submit">Add</button></form>
    <p class="muted" style="font-size:12px;margin:10px 0 0">Tags are compared with the night that follows. Trends shows what they change once you've logged a few.</p>`;
  host.querySelectorAll("[data-tag]").forEach(b => b.onclick = () => {
    const now = b.getAttribute("aria-pressed") !== "true"; b.setAttribute("aria-pressed", String(now)); setTag(day, b.dataset.tag, now);
  });
  $("#tagForm", host).onsubmit = e => { e.preventDefault(); const v = $("#tagInput", host).value.trim(); if (!v) return;
    setTag(day, v, true); renderJournal(day); };
}

let liveTimer = null;
function startLive() {
  clearInterval(liveTimer);
  const tick = async () => {
    if (S.view !== "today") return clearInterval(liveTimer);
    let d; try { d = await api("/api/live"); } catch { return; }
    const pts = d.whoop || [];
    const fresh = pts.length && Date.now() / 1000 - pts[pts.length - 1].ts < 20;
    const bpm = $("#liveBpm"), pulse = $("#livePulse"), when = $("#liveWhen"), host = $("#liveChart"), note = $("#liveNote");
    if (!bpm) return;
    pulse.classList.toggle("off", !fresh);
    bpm.textContent = fresh ? pts[pts.length - 1].bpm : "—";
    const st = S.data.status || {}; const last = st.whoop && st.whoop.bluetooth && st.whoop.bluetooth.last_sample;
    when.textContent = fresh ? "Live from the strap" : `Strap last seen ${ago(last)}`;
    note.hidden = !!fresh;
    if (fresh) lineChart(host, [{ ...ONE, name: "Heart rate", points: pts.map(p => [p.ts, p.bpm]), gap: 30 }], { height: 110, xIsTime: true, yFmt: v => Math.round(v) });
    else host.innerHTML = "";
  };
  tick(); liveTimer = setInterval(tick, 5000);
}

async function viewSleep(root) {
  const day = S.day, i = S.days.indexOf(day);
  const tiles = [["sleep_min", "Asleep", hm], ["sleep_perf", "Of your need", v => Math.round(v) + "%"], ["efficiency", "Efficiency", v => Math.round(v) + "%"],
    ["in_bed_min", "In bed", hm], ["bedtime", "Bedtime", clock], ["waketime", "Wake time", clock], ["resp_rate", "Breathing", v => fmt(v, 1) + " rpm"]].filter(t => shown(t[0]));
  const stageSrc = (row(OURS, day) || {}).deep_min != null ? row(OURS, day) : (S.prefs.fallback ? row(OFFICIAL, day) : null);
  const stagesEver = anyData("deep_min");
  root.innerHTML = `
    <div class="panel">
      <div class="panel-h">${dayBar(i, "Night ending", day)}</div>
      ${tiles.length ? `<div class="stats">${tiles.map(([f, k, ff]) => { const x = val(day, f);
        return `<div class="stat"><div class="k">${k}${srcTag(x)}</div><div class="v num">${x.v == null ? "—" : esc(ff(x.v))}</div></div>`; }).join("")}</div>` : `<div class="empty">No sleep data yet.</div>`}
      ${hiddenNote(["sleep_min", "sleep_perf", "efficiency", "in_bed_min", "bedtime", "waketime", "resp_rate"])}
    </div>
    ${stagesEver || !S.prefs.hideEmpty ? `<div class="panel">
      <div class="panel-h"><h2>Stages and heart rate</h2><span class="muted" style="font-size:12px">stages estimated from heart rate</span></div>
      <div id="hyp"></div>
      <div class="stages" id="stages" style="margin-top:14px"></div>
    </div>` : `<div class="panel">${hiddenNote(["stages"]) || ""}</div>`}`;
  wireDayBar(root, i);
  const sh = $("#stages", root);
  if (sh) {
    const r = stageSrc || {}; const parts = [["deep", r.deep_min], ["rem", r.rem_min], ["light", r.light_min], ["awake", r.awake_min]];
    const tot = parts.reduce((a, [, v]) => a + (v || 0), 0);
    sh.innerHTML = tot ? `<div class="stackbar">${parts.map(([k, v]) => v ? `<div class="st-${k}" style="width:${v / tot * 100}%" title="${k}: ${hm(v)}"></div>` : "").join("")}</div>
      <div class="stats">${parts.map(([k, v]) => `<div class="stat"><div class="k"><span class="swatch st-${k}" style="display:inline-block;vertical-align:middle;margin-right:6px"></span>${k === "rem" ? "REM" : k[0].toUpperCase() + k.slice(1)}</div><div class="v num" style="font-size:20px">${hm(v)}</div><div class="s num">${Math.round((v || 0) / tot * 100)}%</div></div>`).join("")}</div>` : `<div class="empty">No stage breakdown for this night.</div>`;
  }
  const key = "whoop:" + day;
  if (!S.nights[key]) { try { S.nights[key] = await api(`/api/night?device=whoop&day=${day}`); } catch { S.nights[key] = { hypnogram: [], hr: [] }; } }
  const hy = $("#hyp", root);
  if (hy && S.view === "sleep" && S.day === day) hypnogramSVG(hy, S.nights[key].hypnogram, S.nights[key].hr);
}

function viewTrends(root) {
  const all = ["recovery", "hrv", "rhr", "sleep_min", "strain", "resp_rate", "spo2"], list = all.filter(shown);
  if (!list.length) { root.innerHTML = `<div class="panel"><div class="empty">No data to chart yet.</div></div>`; return; }
  if (!list.includes(S.metric)) S.metric = list[0];
  const m = METRICS[S.metric];
  root.innerHTML = `
    <div class="panel">
      <div class="panel-h">
        <h2>${esc(m.label)} over time</h2>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <select id="metricSel" aria-label="Metric">${list.map(k => `<option value="${k}" ${k === S.metric ? "selected" : ""}>${METRICS[k].label}</option>`).join("")}</select>
          <div class="seg" role="group" aria-label="Range">${[7, 30, 90].map(n => `<button data-range="${n}" aria-pressed="${n === S.range}">${n}d</button>`).join("")}</div>
        </div>
      </div>
      <div id="trend"></div>
      ${hiddenNote(all)}
    </div>
    <div class="panel"><div class="panel-h"><h2>Averages</h2><span class="muted" style="font-size:12px">last 7 days vs the 7 before</span></div>
      <table class="vitals" id="avgTbl"></table></div>
    <div class="panel"><div class="panel-h"><h2>What changes your next night</h2></div>
      <div class="scroll-x" id="patterns"></div></div>`;
  renderPatterns();
  const end = S.days[S.days.length - 1], start = addDays(end, -S.range + 1);
  const pts = S.days.filter(d => d >= start).map(d => [new Date(d + "T12:00:00").getTime(), val(d, S.metric).v]).filter(p => p[1] != null);
  lineChart($("#trend", root), [{ ...ONE, name: m.label, gap: 2.5 * 86400000, points: pts }],
    { yFmt: (v, tip) => tip ? m.fmt(v) : (S.metric === "sleep_min" ? (v / 60).toFixed(0) + "h" : fmt(v, m.d)), yMin: S.metric === "recovery" ? 0 : null, yMax: S.metric === "recovery" ? 100 : null });
  const avg = (f, a, b) => { const v = []; for (let d = a; d >= b; d--) { const x = val(addDays(end, -d), f).v; if (x != null) v.push(x); } return v.length ? v.reduce((x, y) => x + y, 0) / v.length : null; };
  $("#avgTbl", root).innerHTML = `<thead><tr><th>Metric</th><th>Last 7</th><th>Before</th><th>Change</th></tr></thead><tbody>${list.map(f => {
    const a = avg(f, 6, 0), b = avg(f, 13, 7), d = a != null && b ? Math.round((a - b) / b * 100) : null;
    return `<tr><td>${METRICS[f].label}</td><td class="num">${a == null ? "—" : esc(METRICS[f].fmt(a))}</td><td class="num muted">${b == null ? "—" : esc(METRICS[f].fmt(b))}</td><td class="num">${d ? `${d > 0 ? "▲" : "▼"} ${Math.abs(d)}%` : "—"}</td></tr>`; }).join("")}</tbody>`;
  $("#metricSel", root).onchange = e => { S.metric = e.target.value; store.set("metric", S.metric); render(); };
  root.querySelectorAll("[data-range]").forEach(b => b.onclick = () => { S.range = +b.dataset.range; store.set("range", S.range); render(); });
}

function renderPatterns() {
  const host = $("#patterns"); if (!host) return;
  const P = S.patterns;
  if (!P || !P.patterns || !P.patterns.length) {
    host.innerHTML = `<div class="empty">Log journal tags on the Today screen. After about a week of entries, this shows how each one moves your next night.</div>`; return; }
  const cols = [["recovery", "Recovery", v => `${v > 0 ? "+" : ""}${fmt(v)} pts`], ["hrv", "HRV", v => `${v > 0 ? "+" : ""}${fmt(v)} ms`],
    ["rhr", "Resting HR", v => `${v > 0 ? "+" : ""}${fmt(v, 1)} bpm`], ["sleep_min", "Sleep", v => `${v > 0 ? "+" : "−"}${Math.abs(Math.round(v))} min`]];
  const label = { clear: "clear effect", possible: "possible effect", none: "no clear effect" };
  host.innerHTML = `<table class="vitals"><thead><tr><th>Tag</th><th>Days</th>${cols.map(c => `<th>${c[1]}</th>`).join("")}</tr></thead><tbody>${
    P.patterns.map(p => `<tr><td>${esc(p.tag)}</td><td class="num">${p.n}</td>${cols.map(([k, , f]) => { const m = p.metrics[k];
      return m ? `<td class="num">${esc(f(m.diff))}<span class="eff ${m.strength}">${label[m.strength]}</span></td>` : `<td><span class="na">—</span></td>`; }).join("")}</tr>`).join("")}</tbody></table>
    <p class="muted" style="font-size:12px;margin:10px 0 0">Average of the nights after a tagged day minus the nights after other days. Patterns, not proof: other things you did that day count too.</p>`;
}

function viewDevices(root) {
  const st = S.data.status || {}, s = st.whoop || { bluetooth: {}, official: {} }, bt = s.bluetooth || {}, of = s.official || {};
  const fresh = bt.last_sample && Date.now() / 1000 - bt.last_sample < 600;
  const sw = (key, title, body) => `<div class="setrow"><div><b>${title}</b><div class="muted" style="font-size:12px">${body}</div></div>
    <button class="switch" role="switch" id="pref-${key}" data-pref="${key}" aria-checked="${!!S.prefs[key]}" aria-label="${title}"><i></i></button></div>`;
  root.innerHTML = `
    <div class="grid2">
      <div class="panel dev">
        <div class="panel-h" style="margin:0"><h2>WHOOP MG</h2><span class="tier"><i style="background:${fresh ? "var(--good)" : "var(--ink-3)"}"></i>${fresh ? "In range" : "Not in range"}</span></div>
        <dl class="kv"><dt>Last reading</dt><dd>${ago(bt.last_sample)}</dd><dt>Coverage, last 24 h</dt><dd class="num">${bt.minutes_last_24h ? hm(bt.minutes_last_24h) : "—"}</dd></dl>
        <p class="note" style="margin:0">From the strap today: heart rate and beat intervals, which give our HRV, resting HR, breathing rate, sleep, Recovery and Strain. SpO₂ and skin temperature are not decoded.</p>
      </div>
      <div class="panel">
        <div class="panel-h"><h2>Display</h2></div>
        ${sw("hideEmpty", "Hide metrics with no data", "Rings, rows, tiles and chart options disappear when a metric has never had a value.")}
        ${sw("fallback", "Fill gaps with WHOOP's official values", "Used only where ours is missing, and tagged “official”. Stops when the membership ends.")}
      </div>
    </div>
    <button class="panel linkrow" data-go="compare"><span><b>Compare</b><span class="muted" style="display:block;font-size:13px">How our scores track WHOOP's own and Oura, and whether it's safe to cancel.</span></span><span aria-hidden="true" style="font-size:22px">›</span></button>
    <div class="panel"><div class="panel-h"><h2>Sync</h2><button class="btn" id="syncNow">Sync now</button></div>
      <p class="muted" style="margin:0;font-size:13px">Our scores recompute every 10 minutes. Travelling more than 14 days? Run the laptop collector so the strap's memory doesn't fill.</p></div>`;
  root.querySelectorAll("[data-pref]").forEach(b => b.onclick = () => { const k = b.dataset.pref; S.prefs[k] = !S.prefs[k]; store.set("prefs", S.prefs); render(); });
  $("#syncNow", root).onclick = async e => { if (S.demo) { e.target.textContent = "Not available with sample data"; return; }
    e.target.disabled = true; e.target.textContent = "Syncing…"; try { await fetch("/api/sync", { method: "POST" }); await load(); } finally { e.target.disabled = false; e.target.textContent = "Sync now"; } };
}

// ------------------------------------------------------------------ compare (separate screen)
const PAIRS = {
  whoop_ours_vs_official: ["whoop:ours", "whoop:official", "Our scores vs WHOOP's own"],
  whoop_vs_oura: ["whoop:ours", "oura:official", "Our scores vs Oura"],
  official_whoop_vs_oura: ["whoop:official", "oura:official", "WHOOP's own vs Oura"],
};
function viewCompare(root) {
  const back = `<button class="btn" data-go="devices">‹ Devices</button>`;
  if (!S.compare) { root.innerHTML = `${back}<div class="panel"><div class="empty">Loading comparisons…</div></div>`; return; }
  if (!PAIRS[S.pair]) S.pair = "whoop_ours_vs_official";
  const [ak, bk] = PAIRS[S.pair];
  const A = SERIES.find(s => s.key === ak), B = SERIES.find(s => s.key === bk);
  const res = S.compare[S.pair] || {};
  const metricList = ["hrv", "rhr", "sleep_min", "recovery", "strain", "resp_rate", "bedtime", "waketime"].filter(k => !S.prefs.hideEmpty || (res[k] && res[k].n));
  if (!metricList.includes(S.cmpMetric)) S.cmpMetric = metricList[0] || "hrv";
  const unit = { strain: v => fmt(v, 1), resp_rate: (v, t) => fmt(v, 1) + (t ? " rpm" : ""), hrv: (v, t) => fmt(v) + (t ? " ms" : ""), rhr: (v, t) => fmt(v) + (t ? " bpm" : ""), sleep_min: (v, t) => t ? hm(v) : (v / 60).toFixed(1) + "h", recovery: (v, t) => fmt(v) + (t ? "%" : ""), bedtime: v => clock(v), waketime: v => clock(v) };
  const diffFmt = (k, v) => v == null ? "—" : k === "sleep_min" ? `${v >= 0 ? "+" : "−"}${Math.round(Math.abs(v))} min` : (k === "bedtime" || k === "waketime") ? `${v >= 0 ? "+" : "−"}${Math.round(Math.abs(v) / 60)} min` : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(1)}`;
  root.innerHTML = `
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">${back}<span class="muted" style="font-size:13px">Reference only. Nothing here changes what the main screens show.</span></div>
    <div class="panel">
      <div class="panel-h"><h2>How closely do they agree?</h2>
        <select id="pairSel" aria-label="Comparison">${Object.entries(PAIRS).map(([k, v]) => `<option value="${k}" ${k === S.pair ? "selected" : ""}>${v[2]}</option>`).join("")}</select></div>
      ${metricList.length ? `<div class="stats">${metricList.map(k => { const r = res[k] || { n: 0 }; const good = r.n && r.within_pct >= 80;
        return `<button class="stat" data-m="${k}" style="text-align:left;border:${k === S.cmpMetric ? "1.5px solid var(--ink-2)" : "1.5px solid transparent"};cursor:pointer">
          <div class="k">${METRICS[k].label}</div>
          <div class="v num">${r.n ? r.within_pct + "%" : "—"}</div>
          <div class="s">${r.n ? `within ${esc(r.tolerance.replace("±1800", "±30 min"))} · ${r.n} nights` : "no paired nights yet"}</div>
          <div class="s">${r.n ? `avg diff ${diffFmt(k, r.mean_diff)}${r.r != null ? " · r " + r.r : ""}` : ""}</div>
          ${r.n ? `<div class="s" style="color:${good ? "var(--good)" : "var(--warn)"};font-weight:600">${good ? "● Close match" : "● Needs work"}</div>` : ""}</button>`; }).join("")}</div>` : `<div class="empty">No nights where both sources have data yet.</div>`}
    </div>
    <div class="grid2">
      <div class="panel"><div class="panel-h"><h2>${METRICS[S.cmpMetric].label}, night by night</h2></div><div id="scatter"></div></div>
      <div class="panel" id="ready"></div>
    </div>`;
  scatterSVG($("#scatter", root), ((res[S.cmpMetric] || {}).pairs || []), A, B, unit[S.cmpMetric]);
  $("#pairSel", root).onchange = e => { S.pair = e.target.value; store.set("pair", S.pair); render(); };
  root.querySelectorAll("[data-m]").forEach(b => b.onclick = () => { S.cmpMetric = b.dataset.m; render(); });
  const r = (S.readiness || {}).whoop, rd = $("#ready", root);
  if (!r) { rd.innerHTML = `<div class="panel-h"><h2>Ready to cancel WHOOP?</h2></div><div class="empty">Not enough data yet.</div>`; return; }
  const pct = Math.min(100, Math.round(r.nights / r.target_nights * 100));
  const names = { hrv: "HRV matches WHOOP's", rhr: "Resting HR matches WHOOP's", sleep_min: "Sleep time matches WHOOP's", hrv_vs_oura: "HRV matches Oura" };
  const cal = ((S.data.status || {}).whoop || {}).calibration;
  rd.innerHTML = `<div class="panel-h"><h2>Ready to cancel WHOOP?</h2><span class="tier"><i style="background:${r.ready ? "var(--good)" : "var(--warn)"}"></i>${r.ready ? "Ready" : "Not yet"}</span></div>
    <div class="progress" role="progressbar" aria-valuenow="${r.nights}" aria-valuemax="${r.target_nights}"><div style="width:${pct}%"></div></div>
    <div class="muted" style="font-size:12px;margin-top:4px">${r.nights >= r.target_nights ? `${r.nights} paired nights (target ${r.target_nights} reached)` : `${r.nights} of ${r.target_nights} paired nights`}</div>
    <div class="checklist">${Object.entries(r.checks).map(([k, c]) => { const ok = c.n >= 30 && c.within_pct >= 80;
      return `<div class="check"><span class="mark" style="background:${ok ? "var(--good)" : c.n ? "var(--warn)" : "var(--ink-3)"}">${ok ? "✓" : c.n ? "…" : "–"}</span>
      <span><b>${names[k] || k}</b><br><span class="muted">${c.n ? `${c.within_pct}% of ${c.n} nights within ${esc(c.tolerance)} (target 80% over 30 nights)` : "Waiting for data from both sources"}</span></span></div>`; }).join("")}</div>
    <p class="note" style="margin:12px 0 0">${cal && cal.recovery ? `Our Recovery is tuned on ${cal.recovery.n} nights and lands within ±${cal.recovery.mae} points of WHOOP's on average.` : "Our Recovery tunes itself against WHOOP's after 14 paired nights."} Oura is used here only as an outside yardstick.</p>`;
}

// ------------------------------------------------------------------ shell
const VIEWS = { today: viewToday, sleep: viewSleep, trends: viewTrends, devices: viewDevices, compare: viewCompare };
function render() {
  if (!S.data) return;
  const tab = S.view === "compare" ? "devices" : S.view;
  document.querySelectorAll("[data-tab]").forEach(t => { if (t.dataset.tab === tab) t.setAttribute("aria-current", "page"); else t.removeAttribute("aria-current"); });
  document.querySelectorAll("[data-view]").forEach(v => v.hidden = v.dataset.view !== S.view);
  VIEWS[S.view](document.getElementById("view-" + S.view));
}
document.addEventListener("click", e => { const b = e.target.closest("[data-go]"); if (b) go(b.dataset.go); });
document.querySelectorAll("[data-tab]").forEach(t => t.onclick = () => go(t.dataset.tab));
const h = (location.hash || "").slice(1).split("?")[0];
if (VIEWS[h]) S.view = h;
window.addEventListener("hashchange", () => { const v = location.hash.slice(1); if (VIEWS[v] && v !== S.view) { S.view = v; render(); } });
if (S.demo) document.getElementById("demoBadge").hidden = false;
let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(render, 200); });
load().catch(err => { document.getElementById("view-today").innerHTML = `<div class="panel"><div class="empty">Couldn't reach the Loop server (${esc(err.message)}). Check that the Pi is on and Tailscale is connected.</div></div>`; });
})();
</script>
```

## `web/manifest.webmanifest`

```json
{
  "name": "Loop",
  "short_name": "Loop",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#0C111A",
  "theme_color": "#0C111A",
  "icons": [{ "src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml" }]
}
```

## `web/icon.svg`

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0C111A"/><circle cx="256" cy="256" r="150" fill="none" stroke="#222C3B" stroke-width="34"/><circle cx="256" cy="256" r="150" fill="none" stroke="#C4851F" stroke-width="34" stroke-linecap="round" stroke-dasharray="660 943" transform="rotate(-90 256 256)"/><circle cx="256" cy="256" r="100" fill="none" stroke="#222C3B" stroke-width="28"/><circle cx="256" cy="256" r="100" fill="none" stroke="#8270E6" stroke-width="28" stroke-linecap="round" stroke-dasharray="460 628" transform="rotate(-90 256 256)"/></svg>
```

## `tests/conftest.py`

```python
import os
import tempfile

# One throwaway database for the whole test run, set before any loop import.
os.environ["LOOP_DB"] = os.path.join(tempfile.mkdtemp(), "test.sqlite")
os.environ.setdefault("WHOOP_IMPORT_FROM", "2022-04-01")
os.environ.setdefault("OURA_IMPORT_FROM", "2026-10-01")
```

## `tests/test_frames.py`

```python
from loop.ble import protocol as P
from loop.ble.frames import FrameAssembler, build_frame, crc16_modbus, crc32, parse_standard_hr


def test_check_vectors():
    assert crc16_modbus(b"123456789") == 0x4B37
    assert crc32(b"123456789") == 0xCBF43926


def test_client_hello_rebuilds_byte_for_byte():
    # type 35 command frame, seq 1, command 0x91, data 0x01
    assert build_frame(bytes([0x23, 0x01, 0x91, 0x01])) == P.CLIENT_HELLO


def test_assembler_handles_fragments_and_noise():
    a = build_frame(b"\x28" + bytes(range(23)))
    b = build_frame(b"\x31\x05\x02" + bytes(12))
    stream = b"\x00\x13" + a + b"\xaa\x01\xff" + b
    asm, out = FrameAssembler(), []
    for i in range(0, len(stream), 7):          # 7-byte "MTU" fragments
        out += asm.feed(stream[i:i + 7])
    assert [f.packet_type for f in out] == [0x28, 0x31]


def test_standard_hr():
    bpm, rr = parse_standard_hr(bytes([0x10, 62, 0x00, 0x04]))  # 1024/1024 s
    assert bpm == 62 and rr == [1000.0]
```

## `tests/test_analytics.py`

```python
import math
import random

from loop.analytics import resp_rate, rmssd


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
```

## `tests/test_history.py`

```python
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
```

## `tests/test_sources.py`

```python
import os, tempfile
from loop import db
from loop.sources import whoop_api, oura_api

SLEEP = {"id": "s1", "nap": False, "score_state": "SCORED", "start": "2022-04-24T02:25:44.774Z", "end": "2022-04-24T10:25:44.774Z",
         "timezone_offset": "-05:00", "score": {"stage_summary": {"total_in_bed_time_milli": 30272735, "total_awake_time_milli": 1403507,
         "total_light_sleep_time_milli": 14905851, "total_slow_wave_sleep_time_milli": 6630370, "total_rem_sleep_time_milli": 5879573,
         "disturbance_count": 12}, "respiratory_rate": 16.1, "sleep_performance_percentage": 98, "sleep_efficiency_percentage": 91.7}}
REC = {"sleep_id": "s1", "score_state": "SCORED", "score": {"recovery_score": 44, "resting_heart_rate": 64, "hrv_rmssd_milli": 31.8,
       "spo2_percentage": 95.7, "skin_temp_celsius": 33.7, "user_calibrating": False}}
CYC = {"id": 1, "start": "2022-04-24T02:25:44.774Z", "timezone_offset": "-05:00", "score_state": "SCORED",
       "score": {"strain": 5.3, "kilojoule": 8288.3}, "step_count": 8234}

def test_whoop_mapping(monkeypatch):
    pages = {"/v2/activity/sleep": [SLEEP], "/v2/recovery": [REC], "/v2/cycle": [CYC], "/v2/activity/workout": []}
    monkeypatch.setattr(whoop_api.provider, "get", lambda url, params=None: {"records": pages[url.split("/developer")[1]]})
    whoop_api.sync()
    r = db.query("SELECT * FROM daily WHERE device='whoop' AND source='official'")[0]
    assert r["day"] == "2022-04-24" and r["recovery"] == 44 and r["hrv"] == 31.8 and r["strain"] == 5.3
    assert round(r["sleep_min"]) == 457 and r["steps"] == 8234

def test_oura_mapping(monkeypatch):
    data = {"daily_readiness": [{"day": "2026-10-20", "score": 81, "temperature_deviation": -0.1}],
            "sleep": [{"type": "long_sleep", "day": "2026-10-20", "total_sleep_duration": 27000, "time_in_bed": 29000,
                       "deep_sleep_duration": 5400, "rem_sleep_duration": 6000, "light_sleep_duration": 15600, "awake_time": 2000,
                       "efficiency": 93, "average_hrv": 48, "lowest_heart_rate": 52, "average_breath": 14.6,
                       "bedtime_start": "2026-10-19T23:10:00-04:00", "bedtime_end": "2026-10-20T07:13:00-04:00"}],
            "daily_sleep": [{"day": "2026-10-20", "score": 85}], "daily_activity": [{"day": "2026-10-20", "steps": 9000, "total_calories": 2500, "score": 90}],
            "daily_spo2": [{"day": "2026-10-20", "spo2_percentage": {"average": 96.5}}],
            "heartrate": [{"timestamp": "2026-10-20T12:00:00+00:00", "bpm": 70, "source": "awake"}]}
    data["sleep"][0]["heart_rate"] = {"interval": 300.0, "items": [55.0, None, 53.0], "timestamp": "2026-10-19T23:10:00-04:00"}
    data["sleep"][0]["hrv"] = {"interval": 300.0, "items": [40.0, 45.0, None], "timestamp": "2026-10-19T23:10:00-04:00"}
    monkeypatch.setattr(oura_api.provider, "get", lambda url, params=None: {"data": data[url.rsplit("/", 1)[1]]})
    oura_api.sync()
    r = db.query("SELECT * FROM daily WHERE device='oura' AND source='official'")[0]
    assert r["recovery"] == 81 and r["hrv"] == 48 and r["rhr"] == 52 and r["sleep_min"] == 450 and r["spo2"] == 96.5 and r["steps"] == 9000
    assert db.query("SELECT COUNT(*) n FROM hr_samples WHERE device='oura'")[0]["n"] == 3
    assert db.query("SELECT COUNT(*) n FROM hrv_samples WHERE device='oura'")[0]["n"] == 2
```

## `tests/test_insights.py`

```python
"""Runs in a subprocess so it gets its own empty database."""
import os
import subprocess
import sys
import tempfile
import textwrap

CODE = textwrap.dedent("""
    import time
    from datetime import datetime, timedelta
    from loop import analytics, db, insights
    today = datetime.now(analytics.TZ).date()
    now = int(time.time())
    for i in range(40, -1, -1):
        d = today - timedelta(days=i)
        drank = i % 4 == 1                       # tag on day d ...
        hrv = 50.0
        if (i + 1) % 4 == 1:                     # ... lowers the next night's HRV
            hrv = 38.0
        if i < 2:                                # last two nights: illness-like signal
            hrv, rhr = 35.0, 60.0
        else:
            rhr = 54.0
        db.upsert_daily("whoop", "ours", d.isoformat(), {"recovery": 60, "hrv": hrv, "rhr": rhr, "sleep_min": 450}, now)
        if drank:
            insights.set_tag(d.isoformat(), "Alcohol", True)
    pats = {p["tag"]: p for p in insights.patterns("whoop", "ours")}
    m = pats["Alcohol"]["metrics"]["hrv"]
    assert m["diff"] < -5 and m["strength"] == "clear", m
    ids = [a["id"] for a in insights.alerts()]
    assert any(i.startswith("strain-signal") for i in ids), ids
    s = insights.summary()
    assert s["headline"].startswith("Recovery 60%") and any("HRV" in x for x in s["items"]), s
    print("ok")
""")


def test_patterns_alerts_summary():
    env = dict(os.environ, LOOP_DB=tempfile.mktemp(suffix=".sqlite"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", CODE], env=env, cwd=root, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
```

## `tests/test_workouts.py`

```python
"""Runs in a subprocess so it gets its own empty database."""
import os
import subprocess
import sys
import tempfile
import textwrap

CODE = textwrap.dedent("""
    import math
    from datetime import datetime, timedelta
    from loop import analytics, db
    day = datetime.now(analytics.TZ).date() - timedelta(days=2)
    mid = int(datetime(day.year, day.month, day.day, tzinfo=analytics.TZ).timestamp())
    rows = []
    for m in range(7 * 60, 21 * 60):                      # strap heard 07:00-21:00
        bpm = 72
        if 17 * 60 <= m < 17 * 60 + 40:                   # 40-minute run at 17:00 ...
            bpm = 150 if not (17 * 60 + 18 <= m < 17 * 60 + 20) else 95   # ... with a 2-minute breather
        if 9 * 60 <= m < 9 * 60 + 4:                      # 4 minutes of stairs: too short
            bpm = 140
        rows.append((mid + m * 60, bpm, None, None))
    db.insert_samples("whoop", rows)

    analytics.recompute_day("whoop", day)
    det = db.query("SELECT * FROM workouts WHERE source='detected'")
    assert len(det) == 1, det
    w = det[0]
    assert w["start"] == mid + 17 * 3600 and (w["end"] - w["start"]) // 60 == 40, w
    assert 140 < w["avg_hr"] < 150 and w["max_hr"] == 150 and w["strain"] > 3, w
    base = db.query("SELECT strain, extra FROM daily WHERE device='whoop' AND source='ours'")[0]

    # A logged session outside the heard hours adds estimated strain ...
    lw = analytics.log_workout("whoop", day.isoformat(), "Strength", "05:30", 60)
    after = db.query("SELECT strain, extra FROM daily WHERE device='whoop' AND source='ours'")[0]
    assert after["strain"] > base["strain"] and '"strain_estimated_min": 60' in after["extra"], (base, after)
    assert lw["avg_hr"] is None
    # ... one logged over the detected run takes it over and gets the heart-rate numbers ...
    run = analytics.log_workout("whoop", day.isoformat(), "Run", "17:00", 40)
    assert not db.query("SELECT 1 FROM workouts WHERE source='detected'")
    assert run["avg_hr"] and run["zones"], run
    both = db.query("SELECT strain FROM daily WHERE device='whoop' AND source='ours'")[0]
    assert both["strain"] == after["strain"]              # heard minutes are never counted twice
    # ... and removing the logs puts everything back.
    assert analytics.delete_workout("whoop", lw["id"]) and analytics.delete_workout("whoop", run["id"])
    end = db.query("SELECT strain FROM daily WHERE device='whoop' AND source='ours'")[0]
    assert end["strain"] == base["strain"] and len(db.query("SELECT 1 FROM workouts WHERE source='detected'")) == 1
    try:
        analytics.log_workout("whoop", day.isoformat(), "Run", "25:00", 30)
        raise SystemExit("bad time accepted")
    except ValueError:
        pass
    print("ok")
""")


def test_detect_log_and_strain():
    env = dict(os.environ, LOOP_DB=tempfile.mktemp(suffix=".sqlite"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", CODE], env=env, cwd=root, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
```

## `README.md`

````markdown
# Loop

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
| WHOOP MG stored-history offload | Requested and stored raw; `loop.ble.history probe` works out the layout from your own data |
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
LOOP_DB=data/demo.sqlite python -m loop.tools.seed_demo
LOOP_DB=data/demo.sqlite uvicorn loop.server:app --port 8800
# open http://localhost:8800
```

## Start now on your laptop (before the Pi arrives)

This gets the official WHOOP and Oura data flowing so the 30-night comparison and
auto-tuning start immediately. You move everything to the Pi later.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # set LOOP_BASE_URL=http://localhost:8800 and the API keys
uvicorn loop.server:app --port 8800
```

In the WHOOP and Oura developer dashboards, register `http://localhost:8800/auth/whoop/callback`
and `http://localhost:8800/auth/oura/callback` now, and the Tailscale versions later (both
can be listed). Open http://localhost:8800 → Devices → Connect. The server only syncs while
it's running, but each sync catches up the last few days, so running it daily is enough.

**Moving to the Pi:** stop the server, copy `data/loop.sqlite` and `.env` to the Pi,
change `LOOP_BASE_URL` to the Tailscale address, and start the Pi services. Your
connections and history carry over.

## Set up the Pi

1. **Flash** Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. In its settings,
   set the hostname to `loop`, enable SSH, and add your Wi-Fi.
2. **Install:**
   ```bash
   sudo apt update && sudo apt install -y python3-venv bluez git
   sudo usermod -aG bluetooth pi
   # copy this folder to /home/pi/loop, then:
   cd ~/loop && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env
   ```
3. **Tailscale** (free personal plan):
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   sudo tailscale serve --bg 8800
   tailscale serve status     # shows https://loop.<your-tailnet>.ts.net
   ```
   Put that address in `.env` as `LOOP_BASE_URL`. Install Tailscale on your iPhone
   and laptop and sign in with the same account. Nothing is exposed to the internet.
4. **Pair the strap:** close the WHOOP app on your phone, then
   ```bash
   .venv/bin/python -m loop.ble.whoop_mg scan
   ```
   Put the address in `.env` as `WHOOP_ADDRESS`. If nothing shows up, put the strap in
   pairing mode and scan again.
5. **Start the services:**
   ```bash
   sudo cp deploy/*.service /etc/systemd/system/
   sudo systemctl enable --now loop-server loop-collector
   journalctl -u loop-collector -f     # watch it connect
   ```
6. **Backups:** `chmod +x deploy/backup.sh`, add the cron line from the top of the
   script, and optionally run `rclone config` to add an offsite remote named `offsite`.
7. **On your iPhone:** open the Tailscale address in Safari, then Share → Add to Home
   Screen. It opens full-screen like an app.

## Connect the official APIs (trial-period baselines)

**WHOOP** (your wife's account, from your first day of wearing it):
1. At developer-dashboard.whoop.com, create an app. Redirect URL:
   `https://loop.<tailnet>.ts.net/auth/whoop/callback`. Copy the client ID and secret into `.env`.
2. Set `WHOOP_IMPORT_FROM` to the first day you wore the strap.
3. Restart the server, open Devices → Connect WHOOP MG, and have your wife sign in and approve.
4. Ask her to update the account's height, weight and age to yours.

**Oura:**
1. At cloud.ouraring.com/oauth/applications, create an app with redirect
   `https://loop.<tailnet>.ts.net/auth/oura/callback`. Copy the ID and secret into `.env`.
2. Restart the server, open Devices → Connect Oura Ring 4, and approve.

## Travelling more than 14 days

Run the same collector on your laptop. It uploads to the Pi over Tailscale and, if
the Pi can't be reached, saves batches locally and sends them later.

```bash
pip install -r requirements.txt
# put LOOP_INGEST_TOKEN (same value as on the Pi) and WHOOP_ADDRESS in .env
python -m loop.ble.whoop_mg run --upload https://loop.<tailnet>.ts.net
```

Leave it running with the strap nearby for an hour or so to drain the backlog. The
strap may need a one-time re-pair the first time it switches between Pi and laptop.

## Decoding the strap's stored history

Once the Pi has collected live heart rate *and* the strap has offloaded history
for a few days (both happen automatically), run:

```bash
.venv/bin/python -m loop.ble.history probe          # shows what it found
.venv/bin/python -m loop.ble.history probe --save   # only saves a strong match
.venv/bin/python -m loop.ble.history redecode       # fills old data from stored frames
```

It lines up the stored records with the live heart rate to find where the time and
heart rate sit in each record. After saving, history is decoded automatically, which
fills the hours you were away from the Pi and makes daily Strain complete. Live
readings are never overwritten.

## How the app is laid out

The main screens (Today, Sleep, Trends) show **one number per metric: our own score**.
Two switches under Devices → Display control the rest:

- **Hide metrics with no data** (on by default): a metric that has never had a value
  disappears from rings, rows, tiles and the Trends picker.
- **Fill gaps with WHOOP's official values** (on by default): used only where ours is
  missing, tagged "official". Turn it off to see the app as it will be after cancelling.

Each ring says how much data is behind it ("15h 23m of 24h", tagged "partial" under 70%),
because Strain only counts time the strap was heard. **Tonight** gives an in-bed time and
sleep target from your need, a quarter of the last week's sleep debt (max 1 h) and extra
after a high-strain day, working back from your usual wake time. These weights are a
starting point in `web/app.html` (`sleepPlan`), not WHOOP's formula.

**Compare** (opened from Devices) is the only place WHOOP's official numbers and Oura are
set against ours, with the "Ready to cancel WHOOP?" check.

## Workouts

- **Detected:** at least 10 minutes at or above 45% of heart-rate reserve; easier minutes
  or gaps under 4 minutes don't split a workout. Only time the strap was heard can be
  detected. Constants are at the top of the workouts section in `loop/analytics.py`.
- **Logged:** add one on the Today screen (type, start, minutes). If the strap was heard
  for at least half of it, it gets real heart-rate numbers and replaces the detected
  stretch. Minutes that weren't heard count toward Strain at a typical effort for the
  activity (`LOGGED_EFFORT`), and the Strain ring shows "+ 45m logged".
- API: `POST /api/workouts {day, type, start: "HH:MM", minutes}`, `DELETE /api/workouts/{id}`.

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
  "loop": {
    "command": "python3",
    "args": ["-m", "loop.mcp_server"],
    "cwd": "/path/to/loop",
    "env": { "LOOP_URL": "https://loop.<your-tailnet>.ts.net" }
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
loop/ble/        Bluetooth: framing + CRCs, protocol constants, WHOOP MG collector, upload spooler
loop/sources/    WHOOP and Oura official API sync (OAuth)
loop/ble/history.py  stored-history layout finder + decoder
loop/analytics.py  our sleep, HRV, RHR, breathing, recovery, strain, auto-tuning, agreement
loop/insights.py   journal patterns, alerts, morning brief, ntfy push
loop/mcp_server.py Claude desktop connector
loop/server.py   FastAPI app: dashboard, data API, ingest, OAuth
web/app.html       the dashboard (single file, no build step)
deploy/            systemd units, backup script
tests/             framing, scoring, API mapping, workouts
```

Protocol knowledge comes from the community reverse-engineering projects
b-nnett/goose, NOOP and dofek; none of their code is included here.
````

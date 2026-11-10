#!/usr/bin/env bash
# Nightly backup: consistent SQLite snapshot, keep 14 locally, copy offsite if rclone is set up.
# Cron (crontab -e):  15 3 * * * /home/pi/vitals/deploy/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/backups
snap="data/backups/vitals-$(date +%F).sqlite"
python3 - "$snap" <<'EOF'
import sqlite3, sys
src = sqlite3.connect("data/vitals.sqlite")
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
dst.close(); src.close()
EOF
gzip -f "$snap"
ls -1t data/backups/*.gz | tail -n +15 | xargs -r rm --
# Offsite: run `rclone config` once and name the remote "offsite" (Google Drive or Backblaze B2).
if command -v rclone >/dev/null && rclone listremotes | grep -q '^offsite:'; then
  rclone copy data/backups offsite:vitals-backups --max-age 48h
fi

#!/bin/bash
# Daily backup cron — run as `hoaproxy` user via /etc/cron.d/hoaproxy.
# 1. VACUUM INTO a temp file (consistent snapshot of the live SQLite DB).
# 2. gsutil cp the snapshot to gs://hoaproxy-backups/db/.
# 3. Once weekly, rsync hoa_docs to gs://hoaproxy-backups/hoa_docs/.
# 4. Prune local snapshot.
#
# Logs to /var/log/hoaproxy/backup.log (rotated by logrotate).

set -euo pipefail

DATA_DIR="/var/lib/hoaproxy/data"
DOCS_DIR="/var/lib/hoaproxy/hoa_docs"
GCS_BUCKET="gs://hoaproxy-backups"
LOG_DIR="/var/log/hoaproxy"
mkdir -p "$LOG_DIR"

export GOOGLE_APPLICATION_CREDENTIALS=/etc/hoaproxy/gcp-sa.json

TS=$(date -u +%Y%m%d-%H%M%S)
SNAP="${DATA_DIR}/_snap-${TS}.db"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

log "VACUUM INTO ${SNAP}"
# VACUUM INTO writes a clean, defragmented copy with no WAL trailing.
# This is the right way to make a consistent SQLite snapshot of a live DB.
sqlite3 "${DATA_DIR}/hoa_index.db" "VACUUM INTO '${SNAP}';"
SIZE_BYTES=$(stat -c%s "$SNAP")
log "snapshot size: $(( SIZE_BYTES / 1024 / 1024 )) MB"

log "uploading to ${GCS_BUCKET}/db/hoa_index-${TS}.db"
gsutil -q cp "$SNAP" "${GCS_BUCKET}/db/hoa_index-${TS}.db"

log "removing local snapshot"
rm -f "$SNAP"

# Weekly hoa_docs sync (only on Sunday)
if [ "$(date -u +%u)" = "7" ]; then
    log "weekly hoa_docs rsync → ${GCS_BUCKET}/hoa_docs/"
    gsutil -m rsync -r -d "${DOCS_DIR}/" "${GCS_BUCKET}/hoa_docs/"
fi

# Retention: keep the last 14 daily DBs in GCS, plus monthly snapshots
# from the 1st of each month indefinitely.
log "applying retention policy"
KEEP_DAYS=14
gsutil ls "${GCS_BUCKET}/db/hoa_index-*.db" | while read -r blob; do
    date_part=$(echo "$blob" | sed -E 's|.*hoa_index-([0-9]{8})-.*|\1|')
    # Keep the 1st-of-month snapshots forever
    if [[ "$date_part" =~ [0-9]{6}01 ]]; then continue; fi
    age_days=$(( ( $(date -u +%s) - $(date -u -d "${date_part:0:4}-${date_part:4:2}-${date_part:6:2}" +%s) ) / 86400 ))
    if [ "$age_days" -gt "$KEEP_DAYS" ]; then
        log "  pruning $blob (age ${age_days}d)"
        gsutil -q rm "$blob"
    fi
done

log "done"

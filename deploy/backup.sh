#!/bin/bash
# Daily backup cron — run as `hoaproxy` user via /etc/cron.d/hoaproxy.
#
# Two destinations, by design:
#   - PRIMARY:  gs://hoaproxy-backups  (multi-region US, Standard class)
#               3-zone auto-replication, fast restores, holds everything.
#   - REPLICA:  gs://hoaproxy-backups-replica  (us-east1, Coldline,
#               Object Versioning ON, 90-day noncurrent-version retention)
#               Second-bucket safety net against accidental delete /
#               bucket compromise / project-level billing shutoff.
#
# Daily steps:
#   1. VACUUM INTO a temp file (consistent snapshot of the live SQLite DB).
#   2. gsutil cp the snapshot to PRIMARY/db/.
#   3. gsutil rsync hoa_docs → PRIMARY/hoa_docs/   (delta only)
#   4. gsutil rsync hoa_docs → REPLICA/hoa_docs/   (delta only; cheap because
#      ingress to GCS is free).
#   5. Sunday-only: also copy today's DB snapshot to REPLICA/db/   (weekly,
#      to cap cross-region egress cost; the DB is the heavy blob).
#   6. Mirror gs://hoaproxy-backups/db/precious-*.sql.gz → REPLICA/db/
#      (tiny user-data dumps from /admin/backup; daily, cheap).
#   7. Prune local staging snapshot.
#   8. Apply 14-day rolling + 1st-of-month-forever retention to PRIMARY/db/.
#      REPLICA is governed by its bucket lifecycle, not us — we just write.
#
# Logs to /var/log/hoaproxy/backup.log (rotated by logrotate).

set -euo pipefail

DATA_DIR="/var/lib/hoaproxy/data"
DOCS_DIR="/var/lib/hoaproxy/hoa_docs"
# VACUUM INTO writes the staging snapshot here. We use the cloud volume
# rather than the local NVMe because the live DB grows past what the
# 160 GB root partition can comfortably stage alongside it (a 90+ GB DB
# wants ~95 GB headroom for VACUUM INTO; the 200 GB cloud volume has it).
STAGING_DIR="${DOCS_DIR}/_backup_staging"
GCS_BUCKET="gs://hoaproxy-backups"
REPLICA_BUCKET="gs://hoaproxy-backups-replica"
LOG_DIR="/var/log/hoaproxy"
mkdir -p "$LOG_DIR" "$STAGING_DIR"

export GOOGLE_APPLICATION_CREDENTIALS=/etc/hoaproxy/gcp-sa.json

TS=$(date -u +%Y%m%d-%H%M%S)
SNAP="${STAGING_DIR}/_snap-${TS}.db"
DOW=$(date -u +%u)  # 1=Mon ... 7=Sun

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Bail loudly if the staging volume can't hold a snapshot the size of
# the live DB — the prior failure mode was a silent disk-fill mid-VACUUM
# that left a 53 GiB orphan and started returning 500s site-wide.
DB_BYTES=$(stat -c%s "${DATA_DIR}/hoa_index.db")
NEEDED_BYTES=$(( DB_BYTES + 5 * 1024 * 1024 * 1024 ))  # DB + 5 GB safety
AVAIL_BYTES=$(df -B1 --output=avail "$STAGING_DIR" | tail -1 | tr -d ' ')
if [ "$AVAIL_BYTES" -lt "$NEEDED_BYTES" ]; then
    log "ABORT: only $(( AVAIL_BYTES / 1024 / 1024 / 1024 )) GiB free at $STAGING_DIR, need $(( NEEDED_BYTES / 1024 / 1024 / 1024 )) GiB"
    log "       resize the cloud volume (Hetzner Console → Volumes → hoa-docs-1 → Resize)"
    exit 1
fi

# Cleanup trap: drop the staging file even on failure so a crashed VACUUM
# can't leave a half-written GB-scale file behind on the volume.
trap 'rm -f "$SNAP" "${SNAP}-journal" 2>/dev/null || true' EXIT

log "VACUUM INTO ${SNAP}"
# VACUUM INTO writes a clean, defragmented copy with no WAL trailing.
# This is the right way to make a consistent SQLite snapshot of a live DB.
sqlite3 "${DATA_DIR}/hoa_index.db" "VACUUM INTO '${SNAP}';"
SIZE_BYTES=$(stat -c%s "$SNAP")
log "snapshot size: $(( SIZE_BYTES / 1024 / 1024 )) MB"

log "uploading DB → ${GCS_BUCKET}/db/hoa_index-${TS}.db"
gsutil -q cp "$SNAP" "${GCS_BUCKET}/db/hoa_index-${TS}.db"

# Sunday-only DB mirror to replica. Doing this once a week keeps weekly
# cross-region egress at ~95 GB ($1/week, $4/mo) instead of $30/mo daily.
# User data (precious-*.sql.gz) is mirrored daily below and is the
# higher-value blob.
if [ "$DOW" = "7" ]; then
    log "weekly DB mirror → ${REPLICA_BUCKET}/db/hoa_index-${TS}.db"
    gsutil -q cp "$SNAP" "${REPLICA_BUCKET}/db/hoa_index-${TS}.db"
fi

log "removing local snapshot"
rm -f "$SNAP"
trap - EXIT

# Daily hoa_docs rsync (was weekly pre-2026-05-16; bumped to daily so the
# worst-case window between an upload and its backup is ~24 h, not 7 days).
# Exclude the staging dir so we don't try to back up an in-progress VACUUM
# snapshot. -d removes blobs at destination that no longer exist locally;
# on the replica, Object Versioning + lifecycle keeps a 90-day noncurrent
# copy as an undelete safety net.
log "hoa_docs rsync → ${GCS_BUCKET}/hoa_docs/"
gsutil -m rsync -r -d -x "_backup_staging/.*" "${DOCS_DIR}/" "${GCS_BUCKET}/hoa_docs/"

log "hoa_docs rsync → ${REPLICA_BUCKET}/hoa_docs/"
gsutil -m rsync -r -d -x "_backup_staging/.*" "${DOCS_DIR}/" "${REPLICA_BUCKET}/hoa_docs/"

# Mirror tiny precious-*.sql.gz user-data dumps to replica every run.
# These are the user-facing precious tables (users, proxies, claims, …),
# uploaded by /admin/backup (cron-job.org, 10:00 + 22:00 UTC). They're
# ~10 KB each so daily mirroring is effectively free, and `cp -n` skips
# any blob already present at the destination so we only pay egress on
# the newest ones each run. Old precious blobs in the replica accumulate
# but at ~10 KB × ~700/year that's < 10 MB/year — under retention budget.
log "precious-* mirror → ${REPLICA_BUCKET}/db/"
gsutil -m cp -n "${GCS_BUCKET}/db/precious-*.sql.gz" "${REPLICA_BUCKET}/db/" 2>/dev/null || true

# Retention on PRIMARY: keep the last 14 daily DBs, plus monthly snapshots
# from the 1st of each month indefinitely. REPLICA retention is governed
# by the bucket lifecycle (90-day noncurrent-version sweep), so we don't
# manage it here.
log "applying retention policy to ${GCS_BUCKET}/db/"
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

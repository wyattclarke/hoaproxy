#!/bin/bash
# Restore the live SQLite DB and hoa_docs from the GCS backup bucket.
# Idempotent: rsync skips files that are already present and match by size+mtime.
#
# Run on the Hetzner host as the `hoaproxy` user, AFTER:
#   - the server is bootstrapped (server-bootstrap.sh)
#   - GCP service account key is in /etc/hoaproxy/gcp-sa.json
#   - the most recent full DB backup exists at gs://hoaproxy-backups/db/
#   - hoa_docs has been snapshotted via POST /admin/snapshot-hoa-docs-to-gcs
#     (one-time call against the Render service; see runbook)

set -euo pipefail

DATA_DIR="/var/lib/hoaproxy/data"
DOCS_DIR="/var/lib/hoaproxy/hoa_docs"
GCS_BUCKET="gs://hoaproxy-backups"

echo "[restore] using GCP service account /etc/hoaproxy/gcp-sa.json"
export GOOGLE_APPLICATION_CREDENTIALS=/etc/hoaproxy/gcp-sa.json
gcloud auth activate-service-account --key-file="$GOOGLE_APPLICATION_CREDENTIALS"

# 1. Latest full DB snapshot
echo "[restore] finding latest full DB backup"
LATEST_DB=$(gsutil ls "${GCS_BUCKET}/db/hoa_index-*.db" | sort | tail -1)
echo "[restore] downloading $LATEST_DB → ${DATA_DIR}/hoa_index.db.new"
gsutil -m cp "$LATEST_DB" "${DATA_DIR}/hoa_index.db.new"

# Atomically swap into place (DB may or may not exist yet; mv is idempotent enough)
if [ -f "${DATA_DIR}/hoa_index.db" ]; then
    mv "${DATA_DIR}/hoa_index.db" "${DATA_DIR}/hoa_index.db.previous"
fi
mv "${DATA_DIR}/hoa_index.db.new" "${DATA_DIR}/hoa_index.db"

# 2. hoa_docs
echo "[restore] rsync ${GCS_BUCKET}/hoa_docs/ → ${DOCS_DIR}/"
# -m parallel, -r recursive, -c checksum on size+mtime
gsutil -m rsync -r "${GCS_BUCKET}/hoa_docs/" "${DOCS_DIR}/"

# 3. Quick sanity checks
echo "[restore] DB integrity check"
sqlite3 "${DATA_DIR}/hoa_index.db" "PRAGMA quick_check;" | head -5

echo "[restore] HOA count in restored DB:"
sqlite3 "${DATA_DIR}/hoa_index.db" "SELECT COUNT(*) FROM hoas;"

echo "[restore] documents row count:"
sqlite3 "${DATA_DIR}/hoa_index.db" "SELECT COUNT(*) FROM documents;"

echo "[restore] hoa_docs file count + size:"
find "${DOCS_DIR}" -type f | wc -l
du -sh "${DOCS_DIR}"

echo "[restore] DONE"

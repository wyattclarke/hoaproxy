# Local Ingestion + Deploy Plan

## Problem

Bulk ingestion via the `/upload` API is slow and fragile on Render's 2GB Standard plan. Each PDF requires OCR (Document AI or tesseract), chunking, OpenAI embeddings, and Qdrant vector upsert — all in a background task on the same server handling web traffic. This causes OOM kills during large imports.

## Solution: Ingest Locally, Deploy Artifacts

Run the full ingestion pipeline on a local machine (no memory constraints), then push the pre-built artifacts to Render via SSH/SCP. The server never does the expensive work.

## What Gets Written During Ingestion

The ingestion pipeline (`hoaware/ingest.py`) writes to three places:

| Artifact | Location (Render) | What |
|---|---|---|
| **Raw PDFs** | `/var/data/hoa_docs/{hoa_name}/{file}.pdf` | The original uploaded files |
| **SQLite DB** | `/var/data/hoa_index.db` | HOA metadata, document records, chunk text + point IDs |
| **Qdrant vectors** | `/var/data/qdrant_local/` | Embedding vectors for semantic search |

All three live on Render's persistent disk (20GB, mounted at `/var/data`).

## Step-by-Step Process

### Prerequisites (one-time setup)

```bash
# Install Render CLI
brew tap render-oss/render && brew install render
render login

# Add SSH key to Render account
# Dashboard → Account Settings → SSH Public Keys → Add your Ed25519 key
# Generate one if needed: ssh-keygen -t ed25519

# Verify SSH access
render ssh  # pick hoaproxy-app from the menu
```

### Phase 1: Pull Current Production State

```bash
# Download latest SQLite backup from GCS
gsutil ls gs://hoaproxy-backups/db/ | tail -1  # find latest
gsutil cp gs://hoaproxy-backups/db/hoa_index-LATEST.db data/hoa_index.db

# OR pull directly from Render via SCP
scp -s SERVICE_ID@ssh.REGION.render.com:/var/data/hoa_index.db data/hoa_index.db
```

No need to pull Qdrant data or existing PDFs — we only need the SQLite DB to avoid re-creating existing HOA records.

### Phase 2: Scrape Documents (state-specific)

For CA HOAs (or any new state):

```bash
# Scrape HOA websites for governing docs
python scripts/STATENAME_scrape_docs.py

# Classify and build upload manifest
python scripts/STATENAME_classify_docs.py

# Result: scraped PDFs in scraped_hoa_docs/STATENAME/
# Result: import.json with HOA metadata
# Result: upload_manifest.json with file classifications
```

### Phase 3: Local Ingestion

Run the existing ingestion pipeline locally against the production DB copy:

```bash
# Set environment for local ingestion
export HOA_DB_PATH=data/hoa_index.db
export HOA_DOCS_ROOT=local_ingest_docs   # temporary working dir
export HOA_QDRANT_LOCAL_PATH=data/qdrant_local_build
export OPENAI_API_KEY=...                # from settings.env
export HOA_ENABLE_OCR=1
export HOA_OCR_DPI=300

# 1. Bulk-import HOA metadata into SQLite
python scripts/push_import.py data/STATENAME/import.json

# 2. Copy PDFs into the docs root structure expected by the ingester
#    (organized as {hoa_name}/{file}.pdf)
python scripts/STATENAME_stage_for_ingest.py  # TBD: script to copy manifest files

# 3. Run ingestion — this does OCR, chunking, embedding, Qdrant upsert
#    All locally, no memory limits!
python -m hoaware.ingest --directory local_ingest_docs/
```

**Note:** The ingestion uses `ingest_pdf_paths()` which needs an `OPENAI_API_KEY` for embeddings. This is the only external API cost — same cost whether done locally or on the server.

### Phase 4: Push Artifacts to Render

```bash
# Find SSH target from Render dashboard (Connect → SSH tab)
RENDER_SSH="SERVICE_ID@ssh.REGION.render.com"

# 1. Stop the server to avoid DB corruption during copy
#    (or use the Render dashboard to suspend the service)

# 2. Upload the grown SQLite DB
scp -s data/hoa_index.db $RENDER_SSH:/var/data/hoa_index.db

# 3. Upload the new Qdrant data
#    Tar it up first for faster transfer
tar czf /tmp/qdrant_build.tar.gz -C data qdrant_local_build/
scp -s /tmp/qdrant_build.tar.gz $RENDER_SSH:/tmp/
ssh $RENDER_SSH "cd /var/data && tar xzf /tmp/qdrant_build.tar.gz && rm /tmp/qdrant_build.tar.gz"

# 4. Upload the raw PDFs (so they're viewable/downloadable on the site)
tar czf /tmp/new_hoa_docs.tar.gz -C local_ingest_docs .
scp -s /tmp/new_hoa_docs.tar.gz $RENDER_SSH:/tmp/
ssh $RENDER_SSH "cd /var/data/hoa_docs && tar xzf /tmp/new_hoa_docs.tar.gz && rm /tmp/new_hoa_docs.tar.gz"

# 5. Restart the service
#    Render dashboard → Manual Deploy, or:
render deploys create SERVICE_ID
```

### Phase 5: Verify

```bash
# Health check
curl https://hoaproxy.org/healthz

# Spot-check a few new HOAs
curl https://hoaproxy.org/hoas/SOME_NEW_HOA/documents

# Verify search works on new docs
curl "https://hoaproxy.org/search?q=CC%26R&hoa=SOME_NEW_HOA"
```

## Qdrant Merge Strategy

**Problem:** The production Qdrant already has vectors for existing HOAs (TX, etc.). We need to ADD new vectors without clobbering old ones.

**Solution:** Qdrant uses UUIDs as point IDs (generated from chunk content hashes in `vector_store.py`). New HOA vectors will have different IDs, so they won't collide. Options:

1. **Additive merge (preferred):** Pull the existing Qdrant snapshot, run local ingestion against it (so it grows), then push the whole thing back. No data loss.
2. **Rebuild from scratch:** If the Qdrant data is small enough, just re-ingest everything. Per CLAUDE.md: "Qdrant does NOT need backup — it's rebuildable by re-running the ingestion pipeline."
3. **Remote upsert:** Point local ingestion at a temporarily-exposed Qdrant port on Render (not practical without port forwarding).

Option 1 is safest. Option 2 is simpler but costs OpenAI embedding credits to re-embed existing docs.

## Disk Space Budget

Render persistent disk: 20 GB total.

Current usage estimate (after TX upload):
- SQLite DB: ~500 MB (with chunks table)
- Qdrant vectors: ~1-2 GB
- PDF files: ~5-10 GB (depends on how many TX HOAs uploaded)
- Free: ~7-13 GB

Each new state will add roughly:
- PDFs: varies (TX was ~4 GB for 4,287 files)
- Qdrant: ~100-200 MB per 1,000 documents
- SQLite: ~50-100 MB per 1,000 documents

Monitor with: `ssh $RENDER_SSH "df -h /var/data"`

## Advantages Over API Upload

| | API Upload | Local Ingest + Deploy |
|---|---|---|
| Memory pressure | High (OCR + embed on 2GB server) | None (local machine) |
| Speed | ~2 min/HOA (sequential, with polling) | Parallel, limited only by OpenAI rate limits |
| Reliability | OOM kills, connection timeouts | Retry locally, push once |
| Server downtime | Risk of crashes during upload | Only brief pause during artifact push |
| Cost | Same (OpenAI embeddings) | Same (OpenAI embeddings) |

## Universal Ingest Queue

All scrapers feed into a single queue at `data/ingest_queue/`:

```
data/ingest_queue/
  pending/       # HOAs waiting to be processed
  done/          # Successfully ingested
  failed/        # Errors (inspect and retry or discard)
```

Each entry is a JSON file: `{source}__{hoa_slug}.json`

### Feeding the queue

```bash
# From a scraper (Python)
from scripts.queue_hoa import enqueue
enqueue(name="Sunset Hills HOA", state="CA", source="california_sos",
        files=["path/to/ccr.pdf", "path/to/bylaws.pdf"])

# From CLI
python scripts/queue_hoa.py add --name "Sunset Hills HOA" --state CA \
    --source california_sos --files docs/ccr.pdf docs/bylaws.pdf

# Bulk from existing manifest format
python scripts/queue_hoa.py from-manifest \
    --manifest data/trec_texas/upload_manifest.json \
    --import-json data/trec_texas/import.json \
    --source trec_texas
```

### Processing the queue

```bash
# Via API (slow, safe — current approach)
python scripts/ingest.py --mode api --delay 5

# Locally (fast — for bulk imports)
python scripts/ingest.py --mode local

# Filter by source
python scripts/ingest.py --mode local --source california_sos

# Dry run
python scripts/ingest.py --mode api --dry-run --limit 10
```

## TODO Before CA Import

- [ ] Validate SSH access to Render (`render ssh` or direct SSH)
- [ ] Test SCP file transfer to/from persistent disk
- [ ] Measure current disk usage on Render
- [ ] Test local ingestion end-to-end with a small batch (5 HOAs)
- [ ] Test Qdrant snapshot pull + merge workflow
- [ ] Build CA scraper scripts

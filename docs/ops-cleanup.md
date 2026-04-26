# One-time prod cleanup runbook

Code for the agent-paradigm migration shipped in commits `5b6d03d` through `67447bc` (PR-1 through PR-6). Several follow-up operational steps remain before the legacy state is gone from production. This is the runbook.

For the design / API contract of the new ingestion path, see [`agent-ingestion.md`](agent-ingestion.md).

## Status checklist

- [x] PR-1..6 deployed to Render (verify `/agent/precheck` returns 200 with a category)
- [ ] Categories backfilled on prod DB (PR-2)
- [ ] Junk/PII docs hidden from search (PR-2 with `--apply-hidden-reason`)
- [ ] Bulk-importer accounts purged from prod (PR-5)
- [ ] Orphan documents removed from prod (PR-5)
- [ ] `hoa_locations.source` normalized (PR-5)
- [ ] Tesseract-garbage docs re-OCR'd via DocAI (PR-4)
- [ ] Daily DocAI cost alert wired to a cron (PR-6)
- [ ] `data/doc_audit_report.json` deleted from local repo

Each section below corresponds to a checkbox.

---

## 1. Backfill categories on prod (PR-2)

`documents.category` was added in PR-1 but is empty for everything ingested before then (~17,000 docs). The historical audit at `data/doc_audit_report.json` already classified them; we just need to write the verdicts into the DB.

**Two ways to run it.**

### Option A — over the wire (recommended)

Push the audit JSON to the new admin endpoint:
```bash
curl -X POST -H "Authorization: Bearer $JWT_SECRET" \
  -H "Content-Type: application/json" \
  --data-binary @data/doc_audit_report.json \
  "https://hoaproxy.org/admin/backfill-categories?apply_hidden_reason=true"
```

Returns:
```json
{
  "matched": 12345,
  "not_found_hoa": 67,
  "not_found_doc": 200,
  "marked_hidden": 4055,
  "by_category": {"ccr": 4250, "rules": 2948, ...}
}
```

`apply_hidden_reason=true` also flags junk/PII docs by setting `documents.hidden_reason`. They're hidden from search/QA immediately. The PDFs remain on disk; PR-3 of the future-cleanup plan would hard-delete them after a 30-day rollback window.

### Option B — local script against a downloaded prod DB

```bash
gcloud storage cp gs://hoaproxy-backups/db/hoa_index-LATEST.db /tmp/prod.db
HOA_DB_PATH=/tmp/prod.db python scripts/backfill_categories.py --apply-hidden-reason --dry-run
HOA_DB_PATH=/tmp/prod.db python scripts/backfill_categories.py --apply-hidden-reason
# Upload back: see backup recovery procedure in CLAUDE.md
```

After the backfill, check:
```bash
curl -sS -H "Authorization: Bearer $JWT_SECRET" "https://hoaproxy.org/hoas/Park%20Village/documents" | python3 -c "
import sys, json
docs = json.load(sys.stdin)
for d in docs:
    print(f\"  {d['category'] or '?':10s}  hidden={d['hidden_reason'] or ''}  {d['relative_path']}\")
"
```

---

## 2. Cleanup legacy DB state (PR-5)

Removes accounts created by deleted bulk-importer scripts, drops orphan document rows whose PDFs are gone, and normalizes `hoa_locations.source` to `{agent, public_contributor, legacy_bulk_import}`.

```bash
# Always dry-run first
HOA_DB_PATH=/tmp/prod.db python scripts/cleanup_legacy_db.py --dry-run

# Sample output:
#   Bulk-importer accounts to remove: 6
#     - id=37  email=bulk-importer-e634ed98@hoaproxy-bulk.local
#     ...
#   Orphan documents (missing on disk): 14
#   Source distribution before normalization:
#     - manual: 8242
#     - anonymous_upload: 11
#     - alexandria_va_common_ownership: 1856
#     - trec_texas: 734
#     ...
#   Renames to apply: 4
#     - manual -> agent
#     - anonymous_upload -> public_contributor
#     - alexandria_va_common_ownership -> legacy_bulk_import
#     - trec_texas -> legacy_bulk_import

# Then apply
HOA_DB_PATH=/tmp/prod.db python scripts/cleanup_legacy_db.py
```

This script must run against a local copy of the prod DB or directly on Render; there's no admin endpoint equivalent (intentional — it's destructive).

---

## 3. Score and re-OCR tesseract garbage (PR-4)

Tesseract output on stamped/recorded documents (e.g. Preston Point Declaration: `"fRES~HTEO"`, `"wo~J::Jt..lJ"`) is unusable for search. Score the existing chunks, then re-OCR the worst offenders with DocAI.

```bash
# 1. Score and produce a manifest, prioritized by category and worst-quality first
HOA_DB_PATH=/tmp/prod.db python scripts/score_ocr_quality.py \
  --category ccr \
  --threshold 0.6 \
  --limit 0 \
  --output data/reocr_candidates.json

# Output preview:
#   ratio   pages  cat   hoa / file
#   0.31    28     ccr   Park Village / declaration.pdf
#   0.42    50     ccr   Cary Park / covenants.pdf
#   ...
#   Total pages if re-OCR'd: 2,400  →  est $3.60 via DocAI

# 2. Repeat for other priority categories
HOA_DB_PATH=/tmp/prod.db python scripts/score_ocr_quality.py --category bylaws --output data/reocr_bylaws.json --limit 0
HOA_DB_PATH=/tmp/prod.db python scripts/score_ocr_quality.py --category articles --output data/reocr_articles.json --limit 0

# 3. Re-OCR with a hard cost ceiling
python scripts/reocr_with_docai.py --manifest data/reocr_candidates.json --max-cost-usd 50 --dry-run
python scripts/reocr_with_docai.py --manifest data/reocr_candidates.json --max-cost-usd 50
```

The re-OCR script:
- Reads each PDF from `${HOA_DOCS_ROOT}/${HOA}/${file}` (must be on the same machine where the docs live).
- Calls DocAI on the whole document via the agent's `text_extractable=false` path.
- Re-chunks, re-embeds via OpenAI, replaces chunks + embeddings in SQLite.
- Logs cost via `api_usage_log`.
- Stops when running cost would exceed `--max-cost-usd`.

**Cost estimate.** PR-2's audit said 110,900 valid scanned pages exist in total — at $1.50/1000 pages that's $166 if you re-OCR every scanned doc. Sticking to the worst third by quality (ratio < 0.6) is more like $30–50.

**Where to run it.** It needs both the prod DB and the actual PDFs. Easiest is on Render via shell — both are mounted at `/var/data/`. Locally would require downloading 20+ GB of PDFs.

---

## 4. Wire the daily DocAI cost alert (PR-6)

Set up a cron-job.org (or any cron) hit to the alert endpoint daily:

```
URL:    https://hoaproxy.org/admin/costs/docai-alert?threshold_usd=10&hours=24&notify=true
Method: GET
Header: Authorization: Bearer <JWT_SECRET>
Schedule: daily at 09:00 UTC
```

When 24h DocAI spend exceeds $10, the endpoint emails `COST_REPORT_EMAIL` (must be set in Render env). A response with `over_threshold: false` means nothing happened.

The hard backstop on `/upload` is `DAILY_DOCAI_BUDGET_USD` (env, default $20). To raise it temporarily for a deliberate big import:
```
Render env → DAILY_DOCAI_BUDGET_USD = "100"
```

---

## 5. Delete the audit report from the local repo

Once step 1 (backfill) has succeeded against prod, the 11 MB `data/doc_audit_report.json` has served its purpose:

```bash
rm data/doc_audit_report.json
git status data/  # nothing — it's gitignored
```

The report can be regenerated at any time by running the (deleted) `scripts/audit_docs.py` logic — but the new agent paradigm classifies during upload, so re-running it would only ever be a backfill of any docs that slipped in without `category` set. Unlikely to be needed.

---

## Rollback notes

| Step | Reversible? | How |
|---|---|---|
| 1. Backfill categories | Yes | Each `documents.category` is a single column update; clear with `UPDATE documents SET category = NULL, hidden_reason = NULL` |
| 2a. Delete bulk-importer accounts | Yes | Restore from latest GCS DB backup |
| 2b. Delete orphan documents | Yes (rows only) | Restore from backup; the PDFs were already gone |
| 2c. Normalize source enum | Yes | Run a `UPDATE` mapping the new enum back to the originals — but the original distinctions (alexandria vs trec) are lost on hardware. Restore from backup if you need them. |
| 3. Re-OCR | Yes | Old chunks are replaced; restore from backup if you need them. New chunks are higher-quality, so usually you don't. |
| 4. Cost alert | N/A | Disable the cron |
| 5. Delete audit report | Yes | Re-run audit (but the old report is in older git stashes / GCS too if you kept one) |

Always take a fresh GCS backup (`POST /admin/backup`) before steps 2 and 3.

---

## After this is done

The system is in steady state: agents add HOAs one at a time via `/upload`, the budget guard catches any runaway, the daily alert reports cumulative spend, and the corpus contains only categorized, non-PII documents. The next time someone touches ingestion, they should read [`agent-ingestion.md`](agent-ingestion.md), not this file.

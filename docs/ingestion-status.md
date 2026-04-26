# Ingestion Status — CA/CO HOA Documents

**Last updated:** 2026-04-06

## Pipeline Overview

```
Scrape PDFs (Serper) → Audit/Classify → Ingest (OCR + Embeddings) → Push to Render
```

**Audit** classifies each HOA's documents and recommends one of:
- **upload** — valid HOA docs (CC&Rs, bylaws, etc.), no PII
- **reject** — junk (court filings, tax docs, government docs, unrelated)
- **review_pii** — valid docs but contain personal information (membership lists, ballots, violations)

**Digital vs Scanned** determines ingestion cost:
- **Digital PDFs** — pdfminer can extract text directly. Free OCR. Only cost = OpenAI embeddings.
- **Scanned PDFs** — need Document AI OCR ($1.50/1K pages) before embedding.

## Current Numbers (4,606 HOAs audited)

### By Audit Recommendation

| Recommendation | Count | Notes |
|---|---|---|
| **upload** | 3,077 | Safe to ingest |
| **reject** | 797 | Junk, skip entirely |
| **review_pii** | 732 | Need manual review |
| **Total** | 4,606 | |

### Upload HOAs Broken Down

| Category | Already Ingested | Still Pending | Pending Pages | Cost to Ingest |
|---|---|---|---|---|
| **All-digital** | 413 | **687** | 33,767 digital | ~$0 (embeddings only) |
| **Mixed (digital + scanned)** | 411 | 894 | 41,329 digital + 43,414 scanned | ~$65 Doc AI |
| **Scanned-only** | 190 | 482 | 21,198 scanned | ~$32 Doc AI |
| **Totals** | 1,014 | 2,063 | | |

### Cost Summary for Remaining Upload HOAs

| Item | Pages | Cost |
|---|---|---|
| Document AI (scanned pages) | 64,612 | ~$97 |
| OpenAI embeddings (all pages) | ~140,000 | ~$5 |
| Haiku classification (already done) | — | ~$3 spent |
| **Total remaining** | | **~$102** |

## What's Where

| Artifact | Location | Size |
|---|---|---|
| SQLite DB (all HOAs) | `data/hoa_index.db` | 832 MB |
| Ingested PDFs | `casnc_hoa_docs/` | 16 GB (1,524 HOA dirs) |
| Qdrant vectors | `data/qdrant_local_build/` | 7.7 GB (split across 4 workers + main) |
| Scraped PDFs (CA) | `scraped_hoa_docs/california/` | 15 GB |
| Scraped PDFs (CO) | `scraped_hoa_docs/colorado/` | 17 GB |
| Audit report | `data/doc_audit_report.json` | Full per-HOA breakdown |

## Ingestion Order

1. **Now: 687 all-digital pending HOAs** — zero Document AI cost
2. **Next: 894 mixed HOAs** — $65 Document AI for scanned pages
3. **Then: 482 scanned-only HOAs** — $32 Document AI
4. **Review: 732 PII-flagged HOAs** — need manual decision

## Render Deploy

After ingestion, push three artifacts to Render via SCP:
1. `data/hoa_index.db` → `/var/data/hoa_index.db`
2. `data/qdrant_local_build/` → `/var/data/qdrant_local/` (must merge worker stores first)
3. `casnc_hoa_docs/{ready HOAs}` → `/var/data/hoa_docs/`

Render persistent disk: 20 GB total. Current estimate for all upload HOAs: ~16.5 GB.

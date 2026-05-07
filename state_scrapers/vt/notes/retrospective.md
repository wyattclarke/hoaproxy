# Vermont HOA Scrape — Retrospective

## TL;DR

- **Outcome:** 15 VT HOA/condo profiles live with 26 live documents, 736 live
  chunks, and 93.3% map coverage. The run imported 15 prepared bundles and 17
  newly indexed documents; Sunrise already existed live and merged into that
  profile.
- **Marginal spend:** about **$0.33**: Serper ~$0.03, OpenRouter $0.00, DocAI
  $0.294, embeddings roughly $0.01.
- **Coverage of estimated universe:** about 1% of the <1,500 CAI estimate. The
  SoS universe could not be scraped headlessly, so this is a free-public-doc
  seed set rather than a registry-complete run.
- **Structural ceiling:** Vermont public search is dominated by municipal
  planning/zoning PDFs; higher coverage needs a SoS export or town land-record
  access, not broader keyword searching.

## Cost Breakdown

| Phase | API | Spend | Notes |
|---|---:|---:|---|
| Discovery | Serper | ~$0.03 | 88 search calls |
| Model classification / name repair | OpenRouter | $0.00 | No model calls used |
| OCR | Google Document AI | $0.294 | 196 pages at $0.0015/page |
| Embeddings | OpenAI | ~$0.01 | Estimate from imported chunk volume |
| ZIP centroid backfill | zippopotam.us/manual ZIP centroids | $0 | No paid geocoder |
| **Total** | | **~$0.33** | Well below all caps |

## Final Counts

```json
{
  "state": "VT",
  "raw_manifests": 17,
  "prepared_bundles": 15,
  "prepared_documents": 19,
  "imported_bundles": 15,
  "live_profiles": 15,
  "live_documents": 26,
  "live_chunks": 736,
  "map_points": 14,
  "map_rate": 0.9333,
  "by_location_quality": {"zip_centroid": 14},
  "out_of_bounds_points": 0,
  "ocr_cost_usd": 0.294,
  "rejected_documents": 2,
  "budget_deferred": 0,
  "failed_bundles": 0,
  "zero_chunk_docs_for_vt": 0
}
```

## Source-Family Yield

| Source family | Result | Assessment |
|---|---:|---|
| Vermont SoS registry | 0 leads | Public UI requires CAPTCHA; direct API calls hit Imperva 403 |
| County Serper fallback | 17 clean manifests / 21 bank PDFs after cleanup | Low-yield but usable |
| SNHA/Smugglers' Notch source sweep | 1 net PDF enrichment | Useful only for known resort condos |

## False Positives

The first automated probe was too permissive and banked municipal plans,
zoning bylaws, hazard mitigation plans, DRB staff reports, court material, and
regional planning documents. Those prefixes were deleted before prepare, and
the run switched to direct-only curated leads.

Two prepared-stage rejections were correct enough to leave alone:

| Reject reason | Count | Note |
|---|---:|---|
| `pii:membership_list` | 1 | Sunset Cove booklet included owner/contact-directory risk |
| `low_value:financial` | 1 | Haystack Highlands financial/low-value classification |

## What Worked

- Direct-only probing. Passing only vetted `pre_discovered_pdf_urls` prevented
  broad site crawls from collecting municipal packets and unrelated docs.
- Resort/community document hosts: SNHA, Mountainside, Chimney Hill, Great
  Hawk, Quechee Lakes, Eastridge Acres, and Intervale produced the substantive
  documents.
- ZIP-centroid backfill recovered map coverage after public Nominatim returned
  429s during prepare.

## Would Not Do Again

- Do not use the generic state Serper probe with `--probe` for VT. It inferred
  HOA names from zoning/planning PDFs and created bad bank prefixes.
- Do not spend time retrying the current Vermont SoS API from headless scripts;
  it is free to view but not cleanly automatable from this environment.
- Do not include HOA website roots during probing unless the link set is
  prefiltered; the harvester can collect insurance, directories, service
  introductions, and municipal attachments.

## Reusable Artifacts

| Artifact | Reuse |
|---|---|
| `state_scrapers/vt/scripts/run_state_ingestion.py` | Vermont constants and fallback county query wiring |
| `state_scrapers/vt/leads/vt_curated_*.jsonl` | Audited direct-only VT lead examples |
| `state_scrapers/vt/queries/vt_*_serper_queries.txt` | Low-yield fallback queries; keep for audit, not as a preferred future branch |
| `state_scrapers/vt/results/vt_20260507_224836_codex/final_state_report.json` | Final run artifact |

## Cross-State Lessons

For small Northeast states, SoS-first is still the right architecture, but the
preflight must explicitly distinguish "public web UI" from "headlessly
queryable source." If the registry is CAPTCHA/Imperva-gated, immediately move
to exact source families and direct-only PDF curation; broad county keyword
searches should be treated as candidate collection, not automatic bank input.

# Maine HOA Scrape Retrospective

Two-pass run history. Pass 1 (Codex, May 7) produced 17 ME profiles via
curated direct-PDF probing of Portland parcel folders and York/Wells
registry pages after ICRS SoS was confirmed reCAPTCHA-walled. Pass 2
(Claude, May 7-8) added 2 net new communities through targeted county
Serper rounds and consolidated the bank under proper county prefixes.

## TL;DR

- **Final state:** 19 ME profiles live with 24 documents, 974 chunks,
  100% map coverage (19/19 mapped, all in-bbox). 0 out-of-bbox map
  points.
- **Coverage of estimated universe:** about 1.0% of the <2,000 CAI
  estimate. The structural ceiling pass 1 identified holds — ME
  recorded declarations are not freely web-indexed at scale, and the
  bank-side classifier correctly rejects the noise that keyword Serper
  pulls in (planning packets, reserve studies, broker disclosures, news
  articles).

## Pass 2 Summary

### New live communities

- **Two Echo Homeowners Association** (Brunswick, Cumberland) — banked
  from `twoecho.org`, prepared, imported, location backfilled.
- **York and High Condominium Association** (Portland, Cumberland) —
  banked from a CDN-hosted ME real-estate condo addendum PDF; address
  in the document body resolved the location to 25 High Street,
  Portland, ME.

### Bank consolidation

- Moved Two Echo and York and High from `_unknown-county` to
  `cumberland` in `gs://hoaproxy-bank/v1/ME/...` (sequential
  copy + delete via Python GCS client; `gsutil -m mv` hangs reliably on
  this Mac, per the KS handoff warning).
- Removed 6 rejected `_unknown-county` prefixes that prepare correctly
  declined: Birchwood on Rangeley Lake (CPA bio, junk:tax),
  Diamond Cove (financial report), Jordan Grand (ballot package, PII),
  Lakeside at Pleasant Mountain (planning packet, junk:government),
  The Summit (reserve study, low_value:financial), Tuscan Way
  (unsupported_category:unknown).
- Removed the empty `_unknown-county/eagle-river` manifest left by a
  failed probe.

## Cost Breakdown (Pass 2)

| Phase | API | Spend | Notes |
|---|---:|---:|---|
| Discovery (2 rounds) | Serper | ~$0.04 | 129 search calls (72 + 57) |
| Model classification | OpenRouter | $0.00 | Curated probe path; no LLM classify |
| Bank-side OCR | Google Document AI | ~$0.10 | Net new pages from 2 imported communities |
| Embeddings | OpenAI | ~$0.005 | Marginal |
| **Pass 2 marginal** | | **~$0.15** | Well under Tier 1 cap of $30 |

## Final Counts

```json
{
  "state": "ME",
  "raw_manifests": 20,
  "raw_pdfs": 25,
  "live_profiles": 19,
  "live_documents": 24,
  "live_chunks": 974,
  "map_points": 19,
  "map_rate": 1.0,
  "map_in_bbox": 19,
  "map_out_of_bbox": 0,
  "no_location": 0
}
```

## What Was Tried and Rejected by the Classifier

The pass-2 round-2 ME query set found six candidates that probe banked
but prepare rejected for the right reasons:

| Lead | Reject reason | Verdict |
|---|---|---|
| Birchwood on Rangeley Lake Condominium Association | `junk:tax` | Source URL was a CPA's accountant bio PDF |
| Diamond Cove Homeowners Association | `low_value:financial` | Source was an annual financial report |
| Jordan Grand Condominium Owners Association | `pii:ballot` | Source was a vote package with member PII |
| Lakeside at Pleasant Mountain Condominium Association | `junk:government` | Source was a town planning-board packet |
| The Summit Condominium Owners Association | `low_value:financial` | Source was a reserve study |
| Tuscan Way Condominium Association | `unsupported_category:unknown` | Source was a town subdivision review form |

These are useful negative results — the classifier saved the live
catalog from drift toward financial / town / broker / vote-package
content that mentions an HOA but is not a governing document.

## Source Families: Pass 1 + Pass 2

| Source family | Net manifests | Yield | Status |
|---|---:|---|---|
| Maine ICRS SoS corporate search | 0 | blocked | reCAPTCHA / no-automation notice |
| Generic county-scoped Serper | noisy (pass 1 cleaned + pass 2 curated) | high after curation | reusable |
| Curated Portland parcel folders + town registry PDFs | 15 (pass 1) | high | reusable; safest first path |
| Penobscot direct-only sweep | 0 | dry | confirmed dry by both passes |
| York/Wells source-family sweep | 4 (pass 1) | high | reusable |
| `twoecho.org` direct PDF | 1 | clean | new in pass 2 |
| `cloudfront.net` ME condo addendums | 1 | clean | new in pass 2 |

## Cross-State Lessons (additions on top of pass 1's)

1. **`gsutil -m mv` hangs reliably on Mac for small GCS prefix moves.**
   Use the `google.cloud.storage` Python client and sequential
   `bucket.copy_blob` + `blob.delete()` instead. The KS handoff already
   warned about `gsutil -m rm`; the same advice extends to `mv`.
2. **State-hint name matching needs an address verification step.** Pass
   2 caught a CA-located "Vermont Villas" in the VT run via post-import
   review. The same risk applies to ME — "Maine" is rarer in HOA names
   than "Vermont" so the failure mode is less common, but still worth
   anchoring on city/county evidence before importing live.
3. **`prepare_bank_for_ingest.py`'s rejection categories are
   precision-positive.** `junk:tax`, `low_value:financial`,
   `pii:ballot`, `junk:government`, `unsupported_category:unknown`
   filtered out 6 of 6 round-2 candidates that looked plausibly
   HOA-shaped at the lead stage. Trust the classifier; do not lower
   the budget cap to force imports.

## Reusable Artifacts (Pass 2)

| Artifact | Reuse |
|---|---|
| `state_scrapers/me/queries/me_continuation_targeted.txt` | Round 1 county + statute queries |
| `state_scrapers/me/queries/me_continuation_round2.txt` | Round 2 condominium + homeowners queries |
| `state_scrapers/me/scripts/probe_enriched_leads.py` | Custom probe driver (existing) |
| `state_scrapers/me/scripts/enrich_me_locations.py` | Conservative ZIP-centroid backfill (existing) |

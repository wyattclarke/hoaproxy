# Maine HOA Scrape Retrospective

A frank account of what worked, what did not, and what the next person scraping
a similar state should do differently.

## TL;DR

- **Outcome:** 17 Maine HOAs live on hoaproxy.org with 22 documents, 950 search
  chunks, and 100% map coverage. Total marginal measured spend was about $0.22
  before embeddings: $0.1665 DocAI content OCR plus roughly $0.05 in Serper
  searches. OpenRouter spend attributable to the ME run was $0.00; curation was
  deterministic/manual after search-result inspection.
- **Coverage of estimated universe:** about 1% of the Tier 1 CAI estimate
  (<2,000), but the imported set is high confidence and concentrated in the
  public-record pockets that are actually accessible.
- **Structural ceiling:** Maine has useful public PDFs, but the expected SoS
  universe source is not safely automatable, and county/municipal discovery is
  fragmented across parcel folders, town attachments, registry-style PDFs, and
  noisy government packets.

## Cost Breakdown

| Phase | API | Spend | Notes |
|---|---|---:|---|
| Discovery | Serper | ~$0.05 | About 52 search calls across first county sweep, Penobscot no-bank sweep, and source-family no-bank sweep |
| Model classification / name repair | OpenRouter | $0.00 | No ME-specific OpenRouter records found; DeepSeek/Kimi were configured but the final curated path did not need them |
| OCR | Google Document AI | $0.1665 | 111 content pages at $0.0015/page |
| Embeddings | OpenAI | not measured here | 950 chunks imported by live service |
| ZIP centroid backfill | zippopotam.us | $0 | 17 ZIP centroid records |
| **Total before embeddings** | | **~$0.22** | Well under all explicit caps |

### Per-HOA Economics

| Unit | Count | Cost per unit before embeddings |
|---|---:|---:|
| Raw manifest banked | 18 | ~$0.0122 |
| HOA imported live | 17 | ~$0.0129 |
| HOA with substantive content (>=10 chunks) | 13 | ~$0.0169 |

## False-Positive Classes

| Reject reason | Count | Verdict |
|---|---:|---|
| `unsupported_category:unknown` | 1 | Correct; Homestead Farms was an offering statement, not a usable governing document |
| Duplicate already prepared | 16 | Correct on the second prepare pass after the supplemental York/Wells batch |
| Government/legal/court/planning noise | many in dry sweeps | Correctly kept out of bank after the noisy first live sweep was cleaned |
| Out-of-state collisions | several in source-family audit | Correctly rejected during curation |

## Final Counts

```json
{
  "state": "ME",
  "raw_manifests": 18,
  "raw_pdfs": 23,
  "prepared_bundles": 18,
  "prepared_documents": 22,
  "live_profiles": 17,
  "live_documents": 22,
  "live_chunks": 950,
  "map_points": 17,
  "map_rate": 1.0,
  "by_location_quality": {"zip_centroid": 17},
  "out_of_bounds_points": 0,
  "ocr_cost_usd": 0.1665,
  "rejected_documents": 1,
  "failed_bundles": 0
}
```

## Source-Family Yield

| Source family | Manifests | PDFs | Final assessment |
|---|---:|---:|---|
| Maine ICRS SoS corporate search | 0 | 0 | Blocked by reCAPTCHA and explicit no-automation notice |
| Generic county-scoped Serper | noisy | noisy | Poor; too many non-governing public PDFs |
| Curated Portland parcel folders and municipal/registry PDFs | 15 | 17 | High; safest first production path |
| Penobscot no-bank direct sweep | 0 | 0 | Dry |
| York/Wells source-family no-bank sweep | 4 | 6 | High; best late-run source family |

## Would Not Do Again

- Do not let the generic county runner bank automatically from broad Serper
  results in Maine. It created dirty manifests that had to be deleted.
- Do not spend early effort on sparse or inland counties until Cumberland/York
  source families are exhausted.
- Do not use SoS automation without a compliant export or non-automated lead
  source; the live ICRS form is explicitly unsuitable for this run shape.

## Unsung Win

The `probe_enriched_leads.py` wrapper with curated direct PDFs was the right
control point. It preserved known PDF URLs, avoided re-searching, and let prepare
make the category/OCR decisions without letting broad discovery pollute the bank.

## Cross-State Lessons to Fold Back Into the Playbook

- Add a Maine Appendix D note: SoS-first is theoretically right, but ICRS should
  be treated as blocked for automation unless the access pattern changes.
- For small New England states, direct-PDF curation from municipal parcel folders
  can beat entity-universe discovery when business registries are gated.
- The bank cleanup warning from the Kansas handoff matters: avoid broad
  `gsutil -m rm -r` on macOS when a sequential GCS-client delete is safer and
  more controllable for a single state prefix.

## Reusable Scripts

| Script | Phase | Reusable as-is? |
|---|---|---|
| `state_scrapers/me/scripts/run_state_ingestion.py` | Orchestration | Yes for ME; copy template for other states |
| `state_scrapers/me/scripts/probe_enriched_leads.py` | Curated direct-PDF probing | Reusable with state/default path changes |
| `state_scrapers/me/scripts/enrich_me_locations.py` | Location backfill | Maine-specific ZIP map; reusable pattern, not data |

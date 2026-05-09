# {STATE} HOA Scrape — Retrospective

A frank account of what worked, what didn't, and what the next person scraping
a similar state should do differently.

> **Scope note.** Write for the next person, not as a marketing summary.
> Document dead ends — that is load-bearing information.

## TL;DR

- **Outcome:** {N} {STATE} HOAs live on hoaproxy.org with {N} documents, {N}
  search chunks, {X}% map coverage. Total marginal spend **~${X.XX}**.
- **Coverage of estimated universe:** ~{X}% ({N} of {CAI_ESTIMATE} estimated HOAs).
- **Structural ceiling:** {one sentence on why free public discovery stops here}

## Cost Breakdown

| Phase | API | Spend | Notes |
|---|---|---|---|
| Discovery (SoS scrape / Serper sweeps) | Serper | $X.XX | {N} calls |
| Model classification / name repair | OpenRouter | $X.XX | model, token count |
| OCR | Google Document AI | $X.XX | {N} pages at $0.0015/page |
| Embeddings | OpenAI | $X.XX | {N} chunks |
| ZIP centroid backfill | zippopotam.us | $0 | free |
| **Total** | | **$X.XX** | |

### Per-HOA economics

| Unit | Count | Cost per unit |
|---|---|---|
| Entity attempted | {N} | $X.XXXX |
| HOA imported live | {N} | $X.XXXX |
| HOA with substantive content (≥10 chunks) | {N} | $X.XXXX |

## False-Positive Classes

Describe the main reject classes and whether they were correctly handled.

| Reject reason | Count | Verdict |
|---|---|---|
| `junk:government` | {N} | correct / over-reject / under-reject |
| `pii:membership_list` | {N} | correct |
| `unsupported_category:unknown` | {N} | {note} |
| `junk:unrelated` | {N} | {note} |

## Final Counts

```json
{
  "state": "{STATE}",
  "raw_manifests": 0,
  "prepared_bundles": 0,
  "imported_bundles": 0,
  "live_profiles": 0,
  "live_documents": 0,
  "live_chunks": 0,
  "map_points": 0,
  "map_rate": 0.0,
  "by_location_quality": {"polygon": 0, "address": 0, "zip_centroid": 0},
  "out_of_bounds_points": 0,
  "ocr_cost_usd": 0.0,
  "rejected_documents": 0,
  "budget_deferred": 0,
  "failed_bundles": 0
}
```

## Source-Family Yield

| Source family | Manifests | PDFs | Final assessment |
|---|---|---|---|
| {family} | {N} | {N} | high / medium / zero |

## Would Not Do Again

List the strategies or decisions that cost time / money with no return.

## Unsung Win

The one technique or observation that paid back more than expected.

## Cross-State Lessons to Fold Back Into the Playbook

List anything that should be added to `docs/multi-state-ingestion-playbook.md`
or a future Appendix D note for this state's tier/pattern.

## Reusable Scripts

| Script | Phase | Reusable as-is? |
|---|---|---|
| `state_scrapers/{state}/scripts/...` | | adapt {what} per state |

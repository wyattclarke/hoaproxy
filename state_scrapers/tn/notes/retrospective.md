# Tennessee HOA Scrape Retrospective

A narrative account for the next state scraper. This is not a handoff of
unfinished work; Tennessee reached the live site. The point of this document is
to preserve the operating lessons, cost shape, and traps that are easy to
forget after the run looks clean in hindsight.

## TL;DR

- Final live result: 672 Tennessee HOA rows, 1,072 documents, and 35,190 chunks.
- Final map result: 446 map points, 66.37% coverage, with 0 out-of-Tennessee
  points after cleanup. Quality split: 109 polygon points and 337 ZIP centroids.
- Raw bank at ingestion: 736 manifests under `gs://hoaproxy-bank/v1/TN/`.
- Prepared ingest: 708 imported bundles after removing 3 out-of-state prepared
  prefixes. All 708 ended in imported status.
- Marginal metered cost estimate: about `$22.42`, or about `$0.033` per live
  HOA. DocAI was the dominant cost.
- The most important engineering lesson: same-name HOAs from earlier state
  imports can retain stale coordinates when a later state import updates
  `state=TN` without new spatial evidence. Tennessee required a live cleanup
  patch to clear stale cross-state geometry before map coverage could be trusted.

## Outcome

The final live site state after the import and cleanup sweep:

| Metric | Value |
|---|---:|
| Live TN HOAs | 672 |
| Live TN documents | 1,072 |
| Live chunks | 35,190 |
| Raw TN manifests seen | 736 |
| Prepared bundles written before removals | 711 |
| Prepared bundles imported | 708 |
| Map points | 446 |
| Map coverage | 66.37% |
| Out-of-state map points | 0 |
| DocAI pages | 13,858 |
| DocAI estimated cost | `$20.787` |

The final machine-readable report is `final_state_report.json` in this same
directory.

## Cost Estimate

This estimate includes Serper, OpenRouter, and DocAI. It excludes fixed platform
costs, the main-agent subscription/runtime cost, local CPU time, GCS storage,
Render hosting, embeddings, and human review time.

| Component | Basis | Estimated cost |
|---|---:|---:|
| DocAI | 13,858 pages at `$0.0015/page` | `$20.787` |
| Serper | 3,606 TN `search_calls` from available Serper summary artifacts; Serper homepage advertised `$0.30 / 1,000 queries` on 2026-05-07 | `$1.082` |
| OpenRouter | 158 TN-attributable logged calls in `data/model_usage.jsonl`, conservatively billing output as completion plus reasoning tokens | `$0.548` |
| Total | DocAI + Serper + OpenRouter | `$22.417` |

Per-unit views:

| Denominator | Cost |
|---|---:|
| Per live TN HOA, 672 HOAs | `$0.0334` |
| Per raw bank manifest, 736 manifests | `$0.0305` |
| Per mapped HOA, 446 map points | `$0.0503` |
| Per live document, 1,072 documents | `$0.0209` |

OpenRouter pricing assumption:

- DeepSeek V4 Flash: `$0.14/M` input and `$0.28/M` output tokens from
  `https://openrouter.ai/deepseek/deepseek-v4-flash`.
- Kimi K2.6: `$0.75/M` input and `$3.50/M` output tokens from
  `https://openrouter.ai/moonshotai/kimi-k2.6`.

OpenRouter token usage attributed to TN:

| Model | Calls | Prompt tokens | Completion tokens | Reasoning tokens | Conservative cost |
|---|---:|---:|---:|---:|---:|
| `deepseek/deepseek-v4-flash` | 131 | 105,427 | 116,457 | 69,809 | `$0.0669` |
| `moonshotai/kimi-k2.6` | 27 | 11,953 | 65,053 | 69,919 | `$0.4814` |

The OpenRouter number is intentionally conservative. Billing may not charge
reasoning tokens exactly this way for every provider response, but counting them
as output makes the estimate safer. If only `completion_tokens` are billed as
output, OpenRouter cost would be about `$0.284`, and total metered cost would be
about `$22.153`.

## What Tennessee Was Structurally

Tennessee was not a small-state SoS-first problem. It behaved like a broad,
document-host/source-family state:

- Many bankable documents lived on builder, realtor, HOA-owned, management,
  WordPress, Squarespace, CloudFront, S3, HOA Express, PSMT, WMCO, SREG, IRP,
  Renaissance Company, and listing-host style sources.
- County government and municipal packets were frequent false positives, but
  county names in snippets were still useful routing evidence.
- The long tail mattered. Large metro counties produced the obvious early wins,
  but the state only filled out after source-family expansion and targeted
  lower-yield county-gap passes.
- Public snippets were often better than extracted PDF text for scanned
  covenant PDFs. Many valid Tennessee scans had poor or no extractable text but
  high-signal filenames and Serper snippets.

The right mental model was "find public host families, then mine them
deterministically." County-by-county search was still useful, but mostly as a
way to find or validate host families.

## Strategy Timeline

### 1. Broad deterministic Serper sweeps

The first phase used general query families:

- Tennessee HOA documents/bylaws/declarations.
- City and county HOA covenant phrases.
- Governing-document terms such as declaration, restrictions, bylaws,
  amendment, architectural guidelines, and rules.
- Public document-host patterns.

This produced a large early seed but also a lot of noise: government planning
packets, court filings, real-estate packets, public notices, generic HOA
industry PDFs, and out-of-state shared-name communities.

The useful pattern was to keep the model out of the hot path. Search results
were deduped, direct PDFs were preflighted, hard rejects were applied, and only
compact public metadata went to OpenRouter when a name/county needed repair.

### 2. Source-family expansion

The highest-yield part of Tennessee was source-family mining. Productive or
worth-preserving families included:

- `psmt.cincwebaxis.com` and PSMT public community pages.
- WMCO and SREG upload/source pages.
- IRP CDN paths, especially `f15f4572/files/uploaded`.
- HOA Express `/file/document...` pages and direct-file leftovers.
- `travisclosehomes.com` and its Squarespace `/s/...pdf` aliases.
- `d1e1jt2fj4r8r.cloudfront.net`, especially the account path
  `3ce0d364-21e7-4c5c-b30a-e9a6354b1d46`.
- `static1.squarespace.com` and `wolf-contrabass-xnmp.squarespace.com/s` for
  Chattanooga/Hamilton-area scans.
- `renaissance-company.com/wp-content/uploads`.
- `leegodfrey.com/wp-content/uploads`.
- BuilderCloud/S3, WordPress uploads, and small builder/listing hosts.

Once a family was productive, the correct move was to stop asking the model and
mine it deterministically with exact-source dedupe, hostname-specific rejects,
and local name/county normalization.

### 3. Leftover re-mining

Re-mining existing Serper result directories was high leverage and low cost.
Several later wins came from scanning old result files for unbanked direct PDFs
after new dedupe and source-family knowledge existed.

This matters for future states: do not throw away noisy result sets. They are a
cheap backlog. Once a source family is understood, old "rejected" or
"unprocessed" rows can become bankable with no new Serper or OpenRouter spend.

### 4. County-gap passes

After source families flattened, county-gap passes helped fill the long tail.
DeepSeek query generation was useful for compact county query generation, not
for document judgment at scale. The best pattern was:

1. Generate compact county-specific query families.
2. Run Serper deterministically.
3. Exact-source dedupe against GCS.
4. Clean/preflight PDFs locally.
5. Review only compact public metadata or first-page visuals when needed.

The late gap passes had low yield but caught real documents in Sevier,
Jefferson, Cocke, Grainger, Williamson, Wilson, and similar counties. They also
proved when branches were dry enough to stop.

## What Worked

### Deterministic gates before models

The mandatory hard gates paid off. Most Tennessee false positives were obvious
once deterministic filters encoded them:

- Government packets and subdivision regulations.
- Court, foreclosure, bankruptcy, and public-notice PDFs.
- Real-estate listing packets and auction disclosures.
- Newsletters, forms, facility documents, pool rules, ARC applications, and
  payment/dues sheets.
- Signed AWS URLs and credentialed query strings.
- Out-of-state shared-name documents.

The model was most useful after these gates, not before them.

### Public snippet plus filename review for scans

Many scanned governing PDFs had almost no usable text extraction. For these,
source snippets, file names, and first-page visual evidence were better than
generic text classification. This was especially true for CloudFront,
Squarespace, Travis Close, and builder/listing-host scans.

The lesson is not "trust snippets blindly." The lesson is to preserve snippets
as evidence and compare them against rendered first-page state/county cues.

### Page-one OCR during prepared ingest

Prepared ingest's page-one review was the right quality/cost tradeoff. It let
the discovery phase keep ambiguous-but-safe public PDFs instead of discarding
them too early. The worker then rejected unsupported or junk categories after
extracting or OCRing page 1.

This is why the prompt template now says to bank ambiguous public candidates
with `suggested_category=null` rather than over-pruning them in discovery.

### ZIP collection

ZIP collection materially improved the live map. After import, 335 unmapped TN
location rows already had Tennessee ZIP evidence. A Census ZCTA centroid pass
raised the map from 117 clean points to 446 clean points.

For future states, every governing PDF pass should preserve ZIPs found in OCR,
source pages, and public metadata. Even a ZIP centroid is far better than no map
point, and it is safer than city-center geocoding.

## What Failed Or Was Noisy

### Generic county searches

Generic county searches were noisy and mostly returned:

- Municipal subdivision regulations.
- Trustee/tax-sale and foreclosure notices.
- Public-record snippets mentioning covenants but not containing the governing
  document.
- Out-of-state communities sharing Tennessee city/county names.

They were still useful late in the run, but only after higher-yield source
families had been mined.

### Management-company domain probes

Broad probes for Ghertner, Timmons, AMI, Cedar, Sentry, Kuester, CMG, PMI, FCS,
and similar management-company domains were not productive without a specific
public document page. Do not expand a management-company domain just because it
manages Tennessee HOAs.

### Kimi as availability fallback

Kimi was too expensive to use as a broad availability fallback. It was useful
only as a bounded quality fallback when DeepSeek could not confidently repair a
name or when a small, high-value candidate set deserved a second opinion.

The final TN model spend was still small, but Kimi accounted for most of it:
about `$0.48` of the conservative `$0.55` OpenRouter estimate.

### Render import batch size

Render returned 502 HTML when imports ran in overly large or unlucky batches,
especially during deploy/update windows. The safe pattern was:

- Import prepared bundles in small batches of 5 when Render is under pressure.
- Validate that every response is JSON before trusting a batch file.
- Stop immediately on HTML/502 and retry after health/deploy status is clean.

## The Live Map Trap

This was the most important production issue.

Some Tennessee imports matched HOA names that already existed from other states.
When the prepared bundle had Tennessee city/state evidence but no new geometry,
the old location row could retain stale coordinates and boundaries while the
state changed to `TN`. The result was a TN map endpoint with points in Texas,
California, Washington, Iowa, and other states.

The fix was commit `4488e2e`, which added a narrow cleanup path:

- Prepared import clears stale spatial fields when a same-name existing HOA is
  switched to a new state without new spatial evidence.
- `/admin/backfill-locations` can explicitly clear coordinates and boundaries
  with `clear_coordinates` and `clear_boundary_geojson`.

After applying that cleanup, map enrichment could proceed safely:

1. Demote every current TN map point outside the Tennessee bounding box.
2. Backfill Tennessee ZIP centroids from public ZIP evidence.
3. Re-audit `/hoas/map-points?state=TN`.
4. Clear any newly exposed out-of-state polygon leftovers and reapply ZIP
   centroid when a valid TN ZIP exists.

Final audit: 446 points, 0 outside the TN bbox.

## Future-State Guidance

Use Tennessee as the reference for broad source-family states:

1. Start with top metro counties, but pivot quickly to productive host families.
2. Preserve every search result directory. Re-mining old results is cheap.
3. Promote productive source families to deterministic scraping after two
   successful sweeps.
4. Use OpenRouter only for compact name/county repair or query generation.
5. Treat Kimi as a quality fallback, not an availability fallback.
6. Capture city, county, and ZIP as first-class data. Do not wait until live
   import to think about the map.
7. Before final live import, remove out-of-state prepared prefixes. For TN the
   removed prefixes were:
   - `bartow/lake-jeanette-known-as`
   - `greene/lake-jeanette-known-as`
   - `hillsborough/planned-development`
8. After import, audit map points against a state bounding box before declaring
   success.
9. Produce a final narrative and cost report while the artifacts are still
   fresh. The cost estimate is much harder to reconstruct later.

## What I Would Do Differently

- Add a per-run Serper usage ledger instead of reconstructing search calls from
  `summary.json` files.
- Add a per-run OpenRouter usage ledger keyed by state/run ID, not only a global
  `data/model_usage.jsonl`.
- Run the stale-geometry audit before the first live import sweep, not after the
  map looked wrong.
- Make ZIP centroid backfill part of the prepared/live ingestion plan rather
  than an ad hoc cleanup pass.
- Keep final docs in the state artifact directory from the beginning:
  `final_state_report.json`, narrative retrospective, import ledger, map audit,
  ZIP payloads, invalid prepared removals, and cost calculation.


# KS OpenRouter Discovery Handoff

Current task: zero-lead Kansas HOA governing-document discovery using Serper for search, code for fetch/extract/banking, and OpenRouter models for cheap triage.

## Current State

Kansas is being banked into the existing production document bank:

```text
gs://hoaproxy-bank/v1/KS/...
```

No new bucket is needed for new states. Use a different run id per experiment and let the bank layout separate states/counties/HOAs.

Current cleaned KS manifest count after the 2026-05-03 runs plus the HA-KC deterministic pass:

```text
75
```

Useful current listing command:

```bash
gsutil ls 'gs://hoaproxy-bank/v1/KS/**/manifest.json' 2>/dev/null \
  | sed 's|gs://hoaproxy-bank/v1/KS/||; s|/manifest.json||' \
  | sort
```

## Implemented

Primary harness:

```bash
benchmark/run_ks_openrouter_discovery.py
```

It:

1. Starts with only Kansas, no HOA leads.
2. Optionally asks an OpenRouter model for search-query ideas.
3. Runs Serper searches.
4. Fetches public result pages and PDFs.
5. Extracts PDF snippets locally with PyPDF.
6. Uses OpenRouter model triage on small candidate payloads.
7. Banks accepted docs through `hoaware.bank.bank_hoa()`.

High-yield deterministic source scraper:

```bash
benchmark/scrape_ks_hakc.py
```

It scrapes the Homes Association of Kansas City Kansas association index, calls the public document endpoint (`/scripts/showdocuments.php?an={id}&dt={type}`), downloads Bylaws/Restrictions/Articles/Rules PDFs, and banks only associations with downloaded governing docs.

Important hardening now in the runner:

- OpenRouter client has explicit timeout and no SDK retries.
- Serper pagination is supported with `--pages-per-query`.
- Accepted PDFs are banked after each triage batch, so a later timeout does not lose earlier wins.
- HOA names are normalized before banking (`Homes Association` -> `HOA`, city/state suffix cleanup, generic-name rejection).
- Model-supplied HOA names must have evidence in title/snippet/filename/PDF text/URL path or an HOA-like hostname.
- Generic names such as `Kansas HOA`, `HOA Kansas City`, and `Original HOA` are rejected.
- Non-governing categories such as `minutes`, `financial`, and `insurance` are not bankable.
- Rationales that say the document is not Kansas, Missouri/Florida, newsletter, or meeting minutes are rejected.

## Runs

Completed summaries:

```text
benchmark/results/ks_openrouter_qwen_ks_seeded_2/summary.tsv
qwen/qwen3.5-flash-02-23   10 queries   12 candidates   9 accepted/banked

benchmark/results/ks_openrouter_deepseek_ks_seeded_1/summary.tsv
deepseek/deepseek-v4-flash 18 queries   20 candidates   7 accepted/banked

benchmark/results/ks_openrouter_deepseek_ks_seeded_3/summary.tsv
deepseek/deepseek-v4-flash 35 queries   40 candidates   27 accepted/banked

benchmark/results/ks_openrouter_deepseek_ks_expanded_1/summary.tsv
deepseek/deepseek-v4-flash 58 queries   60 candidates   46 accepted/banked

benchmark/results/ks_hakc_full_1/summary.json
HA-KC deterministic scraper   44 associations   29 with docs   85 PDFs banked/merged

benchmark/results/ks_hakc_full_2/summary.json
HA-KC corrected rerun         43 associations   28 with docs   77 PDFs banked/merged
```

The cleaned bank count is lower than raw accepted totals because duplicates and false positives were removed or merged.

An attempted large OpenRouter scale run (`deepseek_ks_scale_1`) made 270 Serper calls and fetched 72 PDF candidates, but was stopped before triage because candidate collection waited too long before banking anything. Next improvement for broad search should be chunked candidate collection/triage, or smaller source-focused batches.

## Cost/Quality Read

Best current mix: deterministic source scrapers first, then `deepseek/deepseek-v4-flash` through OpenRouter for long-tail source discovery and ambiguous triage.

Why:

- HA-KC deterministic scraping added more unique KS HOAs than the broad LLM/search pass.
- DeepSeek produced better triage reasons and names than Qwen in this task.
- DeepSeek stayed cheap enough that Serper was a more visible line item than model tokens.
- The architecture is the main cost lever: code searches/fetches/extracts, the model only classifies small JSON batches.

Rough Serper spend in these named runs is about:

```text
0.01 + 0.018 + 0.035 + 0.058 = $0.121
```

OpenRouter usage for triage was small relative to the $10 budget because each candidate payload is a short snippet, not a full PDF.

## Cleanup Done

Known false/duplicate prefixes removed after the expanded pass:

- `cobalt-reks`
- `kansas`
- `crescent-lakes-addition-to-andover`
- `crescent-lakes-addition-kansas`
- `arlington-estates-homes`
- `blue-valley-riding-homes`
- `sylvan-lake-s`
- `northwood-trails`
- `tuscany`
- `original`
- `kansas-city`
- `normandy-place-homes`
- `symphony-hills`
- `milburn-fields-homes`

The scanned Cobalt Reks false positive was manually re-banked correctly as:

```text
gs://hoaproxy-bank/v1/KS/pottawatomie-county/wildcat-woods/manifest.json
```

## Tomorrow Strategy

For other states, run parallel state-specific versions of this harness rather than making a new bucket.

Recommended approach:

1. Identify aggregator/management-company indexes first. This is how NC gets to 1000+; blind search alone will not.
2. Build deterministic source scrapers for high-yield hosts before spending LLM tokens.
3. Use deterministic query packs next: state name, state abbreviation, major counties/cities, `declaration of covenants`, `restrictive covenants`, `deed restrictions`, `bylaws`, `articles of incorporation`, and local terms like `homes association` or `property owners association`.
4. Add a small number of model-generated queries per state (`--model-queries 5-10`) to discover local wording and high-yield hosts.
5. Keep DeepSeek V4 Flash as the default triage model.
6. Use a stronger model only for review queues: scanned PDFs with no text, generic hostnames, or conflicting state evidence.
7. Run states as separate processes with separate run ids.
8. After each state, inspect manifest names and remove/merge obvious generic or duplicate prefixes before draining into the app.

Example KS command:

```bash
source .venv/bin/activate
OPENROUTER_TIMEOUT_SECONDS=35 python benchmark/run_ks_openrouter_discovery.py \
  --models deepseek/deepseek-v4-flash \
  --run-id deepseek_ks_source_batch \
  --model-queries 8 \
  --max-queries 60 \
  --results-per-query 10 \
  --pages-per-query 2 \
  --max-results 300 \
  --max-pages 100 \
  --max-pdfs 80 \
  --triage-batch-size 4
```

Example deterministic KS source command:

```bash
source .venv/bin/activate
python benchmark/scrape_ks_hakc.py --run-id full_1 --max-associations 500 --delay 0.05
```

## Files

Committed code/docs to preserve:

- `benchmark/run_ks_openrouter_discovery.py`
- `benchmark/scrape_ks_hakc.py`
- `benchmark/KS_OPENROUTER_DISCOVERY_HANDOFF.md`

Untracked local results to preserve if useful:

- `benchmark/results/ks_openrouter_*`
- `benchmark/results/ks_hakc_*`

Pre-existing untracked Claude benchmark files are unrelated:

- `benchmark/run_benchmark.sh`
- `benchmark/task.txt`
- `benchmark/results/20260503_172720/...`

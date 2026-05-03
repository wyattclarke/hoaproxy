# KS OpenRouter Discovery Handoff

Current task: zero-lead Kansas HOA governing-document discovery using Serper for search, code for fetch/extract/banking, and OpenRouter models for cheap triage.

## Current State

Kansas is being banked into the existing production document bank:

```text
gs://hoaproxy-bank/v1/KS/...
```

No new bucket is needed for new states. Use a different run id per experiment and let the bank layout separate states/counties/HOAs.

Current cleaned KS manifest count after the 2026-05-03 runs:

```text
49
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

Important hardening now in the runner:

- OpenRouter client has explicit timeout and no SDK retries.
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
```

The cleaned bank count is lower than raw accepted totals because duplicates and false positives were removed or merged.

## Cost/Quality Read

Best current choice: `deepseek/deepseek-v4-flash` through OpenRouter.

Why:

- It produced better triage reasons and names than Qwen in this task.
- It stayed cheap enough that Serper was a more visible line item than model tokens.
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

1. Use deterministic query packs first: state name, state abbreviation, major counties/cities, `declaration of covenants`, `restrictive covenants`, `deed restrictions`, `bylaws`, `articles of incorporation`, and local terms like `homes association` or `property owners association`.
2. Add a small number of model-generated queries per state (`--model-queries 5-10`) to discover local wording and high-yield hosts.
3. Keep DeepSeek V4 Flash as the default triage model.
4. Use a stronger model only for review queues: scanned PDFs with no text, generic hostnames, or conflicting state evidence.
5. Run states as separate processes with separate run ids.
6. After each state, inspect manifest names and remove/merge obvious generic or duplicate prefixes before draining into the app.

Example KS command:

```bash
source .venv/bin/activate
OPENROUTER_TIMEOUT_SECONDS=35 python benchmark/run_ks_openrouter_discovery.py \
  --models deepseek/deepseek-v4-flash \
  --run-id deepseek_ks_expanded_1 \
  --model-queries 8 \
  --max-queries 58 \
  --results-per-query 10 \
  --max-results 220 \
  --max-pages 80 \
  --max-pdfs 60 \
  --triage-batch-size 4
```

## Files

Committed code/docs to preserve:

- `benchmark/run_ks_openrouter_discovery.py`
- `benchmark/KS_OPENROUTER_DISCOVERY_HANDOFF.md`

Untracked local results to preserve if useful:

- `benchmark/results/ks_openrouter_*`

Pre-existing untracked Claude benchmark files are unrelated:

- `benchmark/run_benchmark.sh`
- `benchmark/task.txt`
- `benchmark/results/20260503_172720/...`

# KS OpenRouter Discovery Handoff

Current task: zero-lead Kansas HOA governing-document discovery using Serper for search, code for fetch/extract/banking, and OpenRouter models for triage.

## Implemented

Primary harness:

```bash
benchmark/run_ks_openrouter_discovery.py
```

It:

1. Starts with only Kansas, no HOA leads.
2. Runs Serper searches.
3. Fetches result pages and PDFs.
4. Extracts PDF snippets locally with PyPDF.
5. Uses OpenRouter model triage.
6. Banks accepted docs through `hoaware.bank.bank_hoa()` into:

```text
gs://hoaproxy-bank/v1/KS/_unknown-county/...
```

Useful command shape:

```bash
source .venv/bin/activate
OPENROUTER_TIMEOUT_SECONDS=45 python benchmark/run_ks_openrouter_discovery.py \
  --models qwen/qwen3.5-flash-02-23 \
  --run-id qwen_ks_seeded_2 \
  --max-queries 10 \
  --model-queries 0 \
  --results-per-query 8 \
  --max-results 50 \
  --max-pages 20 \
  --max-pdfs 12 \
  --triage-batch-size 4
```

DeepSeek comparison:

```bash
OPENROUTER_TIMEOUT_SECONDS=45 python benchmark/run_ks_openrouter_discovery.py \
  --models deepseek/deepseek-v4-flash \
  --run-id deepseek_ks_seeded_1 \
  --max-queries 18 \
  --model-queries 0 \
  --results-per-query 8 \
  --max-results 80 \
  --max-pages 30 \
  --max-pdfs 20 \
  --triage-batch-size 4
```

## Results So Far

Completed result summaries:

```text
benchmark/results/ks_openrouter_qwen_ks_seeded_2/summary.tsv
qwen/qwen3.5-flash-02-23   10 queries   12 candidates   9 accepted/banked

benchmark/results/ks_openrouter_deepseek_ks_seeded_1/summary.tsv
deepseek/deepseek-v4-flash 18 queries   20 candidates   7 accepted/banked
```

Current count at time of handoff:

```bash
gsutil ls 'gs://hoaproxy-bank/v1/KS/_unknown-county/*/manifest.json' | wc -l
# 21
```

Known good/corrected banks include:

- `Yoder Airport Owners Association`
- `Cedar Creek HOA`
- `Crescent Lakes Addition HOA`
- `Creekside HOA`
- `Blue Valley Riding HOA`
- `Normandy Place Homes Association`
- `Arlington Estates HOA`
- `Symphony Hills Homes Association`
- `Sylvan Lake HOA`
- `Prairie Lake Estates HOA`
- `The Moorings Homeowners Association`
- `Prairie Creek Addition HOA`
- `Arbor Woods Homes Association`
- `Woodland Ridge HOA`
- `Shadow Rock HOA`

## Important Lessons

This is not a pure classifier benchmark. The useful architecture is:

- deterministic Serper/search/fetch/PDF extraction;
- model as triage/search strategy;
- strict code-side banking guardrails.

Qwen is cheap and productive but schema compliance is inconsistent. DeepSeek V4 Flash gives better reasons and more useful triage text, but still omits `hoa_name` often. The runner now tolerates:

- JSON array instead of object;
- `decision: keep/accept` instead of boolean `keep`;
- missing confidence;
- `reason` instead of `rationale`.

Main quality issue: HOA name extraction. Early shakedowns banked bad slugs like host-derived names. I cleaned obvious bad shakedown prefixes and re-banked corrected names manually.

## Cleanup Already Done

Removed bad shakedown prefixes including:

- `dos-rios-ill`
- `to-homes`
- `indian-creek-park-estates-homes`
- `wce-homes`
- `by-and-between-yoder-airport`
- `properties-to-city-olathe-or-johnson-county-kansas-at-any-time-in-its`
- `cobaltre`
- `langere`
- `homes-associations-kansas-city`
- `information`
- `mooringsfirsthoa-moorings-in-wichita-ks`
- `preferred-properties-kansas`
- `amazon-s3`
- `67dc31b5b6a159eea0dba972-wr`

If unsure, inspect current KS manifest names before deleting more:

```bash
gsutil ls 'gs://hoaproxy-bank/v1/KS/_unknown-county/*/manifest.json'
```

## Next Best Step

Before scaling, strengthen `benchmark/run_ks_openrouter_discovery.py`:

1. Do not bank if the inferred HOA name is host/storage/generic-derived.
2. Prefer `hoa_name` extracted from model `reason` text, then document text, then URL path.
3. Add a `--dry-run` flag to write accepted docs to summary without banking.
4. Add an `accepted_needs_review` bucket/list for docs with weak names.
5. Possibly add a second model pass only for HOA-name extraction on accepted PDFs.

Then run another broader seeded scrape with DeepSeek or Qwen+DeepSeek:

```bash
OPENROUTER_TIMEOUT_SECONDS=45 python benchmark/run_ks_openrouter_discovery.py \
  --models deepseek/deepseek-v4-flash \
  --run-id deepseek_ks_seeded_2 \
  --max-queries 30 \
  --model-queries 0 \
  --results-per-query 10 \
  --max-results 120 \
  --max-pages 45 \
  --max-pdfs 35 \
  --triage-batch-size 4
```

## Files To Preserve

Untracked but important:

- `benchmark/run_ks_openrouter_discovery.py`
- `benchmark/KS_OPENROUTER_DISCOVERY_HANDOFF.md`
- `benchmark/results/ks_openrouter_*`

There are also pre-existing untracked benchmark files from the Claude benchmark:

- `benchmark/run_benchmark.sh`
- `benchmark/task.txt`
- `benchmark/results/20260503_172720/...`

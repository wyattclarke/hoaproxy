# Legal Corpus Scripts

Pipeline order:

1. `build_source_map.py`
2. `build_proxy_requirement_matrix.py`
3. `fetch_law_texts.py`
4. `normalize_law_texts.py`
5. `extract_rules.py`
6. `assemble_profiles.py`
7. `validate_corpus.py`
8. `update_progress_index.py`
9. `build_electronic_proxy_summary.py`

One-command pipeline:

```bash
python scripts/legal/run_pipeline.py --state NC
python scripts/legal/run_pipeline.py --state NC --refresh-fetch --force-normalize
python scripts/legal/run_pipeline.py --rebuild-source-map --rebuild-proxy-matrix --limit 500
python scripts/legal/build_electronic_proxy_summary.py --community-type hoa
```

Notes:
- `build_source_map.py` creates `data/legal/source_map.json` with 50-state placeholders plus deterministic curated seeds from `data/legal/state_source_registry.json` (and legacy pilot seeds as fallback).
- Use `--registry-seeds` to point at a different curated registry file.
- Fetch/normalize/extract only process rows with `retrieval_status` seeded/verified and non-empty `source_url`.
- Fetch and normalize are idempotent by default:
  - fetch skips URLs already present in `sources.jsonl` unless `--refresh`
  - normalize skips snapshot paths already normalized unless `--force`
- `run_pipeline.py` does not rebuild `source_map.json` unless `--rebuild-source-map` is provided.
- Extracted rules are heuristic; many are still flagged for human review.

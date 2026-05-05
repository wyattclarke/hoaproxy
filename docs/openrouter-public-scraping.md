# OpenRouter Public Scraping Setup

This setup is for public HOA governing-document discovery only. Keep private
portal pages, resident data, cookies, dashboard exports, emails, credentials,
and internal/company documents out of prompts.

## Environment

`settings.env` is gitignored. Use OpenRouter for chat/classification and keep
OpenAI for embeddings until the vector pipeline is changed:

```bash
OPENROUTER_API_KEY=sk-or-...
QA_API_BASE_URL="https://openrouter.ai/api/v1"
QA_API_KEY="${OPENROUTER_API_KEY}"
QA_MODEL="deepseek/deepseek-v4-flash"

HOA_ENABLE_LLM_CLASSIFIER="1"
HOA_CLASSIFIER_API_BASE_URL="https://openrouter.ai/api/v1"
HOA_CLASSIFIER_API_KEY="${OPENROUTER_API_KEY}"
HOA_CLASSIFIER_MODEL="deepseek/deepseek-v4-flash"
# HOA_CLASSIFIER_FALLBACK_MODEL="moonshotai/kimi-k2.6"
HOA_CLASSIFIER_BLOCKLIST="qwen/qwen3.5-flash,qwen/qwen3.6-flash"
HOA_MODEL_USAGE_LOG="data/model_usage.jsonl"

HOA_DISCOVERY_RESPECT_ROBOTS="1"
HOA_DISCOVERY_REQUEST_DELAY_SECONDS="1.0"
HOA_DISCOVERY_USER_AGENT="HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
```

OpenRouter's official API base URL is `https://openrouter.ai/api/v1`, and its
chat schema is OpenAI-compatible. Model ids must include the provider prefix,
for example `qwen/...` or `deepseek/...`.

`qwen/qwen3.5-flash` and `qwen/qwen3.6-flash` are blocklisted for classifier
calls by default because the May 2026 Kansas scrape produced runaway hidden
reasoning-token usage. Override only for an explicit experiment with
`HOA_ALLOW_BLOCKLISTED_CLASSIFIER_MODELS=1`.

## VS Code

Open this repo folder in VS Code, select the workspace interpreter at
`.venv/bin/python`, and use the checked-in tasks:

- `Run API` starts `uvicorn api.main:app --reload`
- `Run tests` runs `python -m pytest tests/ -q`
- `Scrape NC leads` writes `data/discovery/nc_leads.jsonl`
- `Probe NC leads` runs the current public discovery pipeline

## Cheap Classification Loop

Prefer deterministic code before model calls:

```bash
python scripts/hoa_precheck.py --url "https://example.org/covenants.pdf" --hoa "Example HOA" --human
```

Use the OpenRouter classifier only for ambiguous digital PDFs:

```bash
python scripts/hoa_precheck.py --url "https://example.org/document.pdf" --hoa "Example HOA" --llm --human
```

The LLM prompt receives only URL, title/filename/anchor metadata, and a short
text snippet. It returns JSON with category, confidence, and a short rationale.

Every LLM classifier call appends a metadata-only row to `HOA_MODEL_USAGE_LOG`
(`data/model_usage.jsonl` by default): model, endpoint, generation id, token
usage, exact OpenRouter generation metadata when available, timing, category
metadata, and any error. Prompts, completions, API keys, cookies, and document
text are not logged.

To analyze an exported OpenRouter activity CSV:

```bash
python benchmark/analyze_openrouter_activity.py ~/Downloads/openrouter_activity_2026-05-05.csv
```

## Guardrails

- Do not send secrets: API keys, GitHub tokens, cookies, private dashboards, or
  logged-in pages.
- Respect robots.txt, site terms, and practical rate limits.
- Avoid republishing copyrighted PDFs or turning private HOA portal content into
  a searchable public corpus.
- Keep source URL, source page, document type, HOA name, city/state, confidence,
  and classification method in the manifest so bad URLs and bad classifications
  are auditable.

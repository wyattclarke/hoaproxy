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
QA_MODEL="qwen/qwen3.6-flash"

HOA_ENABLE_LLM_CLASSIFIER="1"
HOA_CLASSIFIER_API_BASE_URL="https://openrouter.ai/api/v1"
HOA_CLASSIFIER_API_KEY="${OPENROUTER_API_KEY}"
HOA_CLASSIFIER_MODEL="qwen/qwen3.5-flash-02-23"
# HOA_CLASSIFIER_FALLBACK_MODEL="deepseek/deepseek-v4-flash"

HOA_DISCOVERY_RESPECT_ROBOTS="1"
HOA_DISCOVERY_REQUEST_DELAY_SECONDS="1.0"
HOA_DISCOVERY_USER_AGENT="HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
```

OpenRouter's official API base URL is `https://openrouter.ai/api/v1`, and its
chat schema is OpenAI-compatible. Model ids must include the provider prefix,
for example `qwen/...` or `deepseek/...`.

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

## Guardrails

- Do not send secrets: API keys, GitHub tokens, cookies, private dashboards, or
  logged-in pages.
- Respect robots.txt, site terms, and practical rate limits.
- Avoid republishing copyrighted PDFs or turning private HOA portal content into
  a searchable public corpus.
- Keep source URL, source page, document type, HOA name, city/state, confidence,
  and classification method in the manifest so bad URLs and bad classifications
  are auditable.

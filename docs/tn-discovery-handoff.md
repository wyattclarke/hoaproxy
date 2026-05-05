# Tennessee Discovery Handoff

Updated: 2026-05-05

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/TN/`
- Starting count: 0 manifests, 0 PDFs.
- Active strategy: deterministic county/city and source-family search first; OpenRouter only for compact validation if deterministic selection flattens.
- Reusable scraper: `benchmark/scrape_state_serper_docpages.py`.
- Initial query file: `benchmark/tn_initial_queries.txt`.

## Guardrails

- Leads must use `state="TN"` so documents land under the Tennessee bank prefix.
- Use `hoaware.discovery.probe.probe()` / `hoaware.bank.bank_hoa()` as the write path.
- Do not use Gemini or Qwen Flash.
- Do not send secrets, cookies, resident data, private portal content, emails, payment data, or internal/work data to any model.
- Respect robots.txt with `HOA_DISCOVERY_RESPECT_ROBOTS=1` and practical delays.

## Counties Started

- Davidson / Nashville
- Williamson / Franklin / Brentwood
- Rutherford / Murfreesboro
- Knox / Knoxville
- Hamilton / Chattanooga
- Shelby / Memphis / Collierville
- Sumner / Hendersonville
- Wilson / Mt. Juliet / Lebanon

## Running Log

- 2026-05-05: Starting TN bank coverage was 0 manifests and 0 PDFs.

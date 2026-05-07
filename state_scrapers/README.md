# State Scrapers

State discovery work is intentionally isolated here because public HOA document
scraping is messy and state-specific. Keep reusable ingestion and banking code in
`hoaware/`, but put state-local experiments, query lists, repair scripts, and
benchmark prompts under the matching state folder.

Suggested layout:

- `state_scrapers/{state}/scripts/` - state-specific cleanup, repair, and scrape helpers.
- `state_scrapers/{state}/queries/` - search query lists and other small text inputs.
- `state_scrapers/{state}/benchmarks/` - benchmark harnesses and prompts tied to that state.
- `state_scrapers/{state}/notes/` - handoff notes that are too operational for `docs/`.

Generated outputs should stay out of git. Result directories named `results/`
under this tree are ignored by `.gitignore`.

Reusable utilities that apply across states should stay in `benchmark/`,
`scripts/`, or `hoaware/discovery/` instead of being copied into each state.

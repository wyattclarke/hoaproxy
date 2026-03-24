# Contributing to HOAproxy

Thanks for your interest in making HOA governance more transparent. Whether you're a developer, designer, policy nerd, or just someone who's had it with your HOA board — there's a way to help.

## Getting Started

1. Fork the repo and clone locally.
2. Set up the dev environment (Python 3.10, FastAPI, SQLite):
   ```bash
   python3.10 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install pytest httpx
   ```
3. Copy `settings.env.example` to `settings.env` and fill in your keys.
4. Run the server: `uvicorn api.main:app --reload`
5. Run tests: `python -m pytest tests/ -q`

## What We Need Help With

- **State law coverage** — We have HOA law profiles for 47 states. OK, PA, SD, and WY are missing because their statute sites block scraping. If you live there or know the law, we'd love help filling in the gaps.
- **HOA onboarding** — Know an HOA that should be on the platform? Upload their public documents.
- **Frontend polish** — The UI is vanilla HTML/CSS/JS. No framework, no build step. If you can make it look better while keeping it simple, that's a win.
- **Bug fixes and tests** — Always welcome. We have 129+ tests and want more.
- **Accessibility** — We haven't done a proper a11y audit yet.

## How to Contribute Code

1. Open an issue first for anything non-trivial, so we can discuss the approach.
2. Create a branch from `master`.
3. Write tests for new functionality.
4. Make sure all tests pass before submitting a PR.
5. Keep PRs focused — one feature or fix per PR.

## Contributor License Agreement

By submitting a pull request, you agree that:

- Your contribution is your original work (or you have the right to submit it).
- You grant the project maintainer a perpetual, worldwide, non-exclusive, royalty-free license to use, modify, and distribute your contribution as part of this project, including under future license terms.
- This allows the project to evolve its licensing (e.g., when versions convert to Apache 2.0 under the FSL) without needing to re-contact every contributor.

## Code Style

- Python: follow existing patterns in the codebase. No strict linter enforced yet.
- Frontend: vanilla HTML/CSS/JS. Fonts are Manrope (body) and Space Grotesk (headings). Primary color is `#1662f3`.
- Don't add frameworks, build tools, or heavy dependencies without discussing first.

## Code of Conduct

Be constructive. This project exists because HOA governance is broken and residents deserve better tools. Keep discussions focused on making things work.

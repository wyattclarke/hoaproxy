# HOAproxy

**A free, transparent civic organizing platform for HOA residents.**

Live at [hoaproxy.org](https://hoaproxy.org)

HOAproxy gives homeowners the tools their HOA boards won't: searchable governing documents, coordinated proxy voting, state law summaries, and resident proposals. It's built to work without board cooperation.

## What It Does

- **Document Search** — Upload HOA PDFs, ask questions in plain English, get answers with page citations (powered by OpenAI embeddings + GPT-4o-mini)
- **Proxy Voting** — Create, e-sign, deliver, and revoke proxy assignments for any HOA meeting. Guerilla online voting.
- **State Law Corpus** — 51-jurisdiction coverage of HOA records access, proxy voting rules, and records sharing limits
- **Resident Proposals** — Draft a proposal, get two neighbors to co-sign, and it goes live for your community to upvote
- **HOA Lookup** — Search any address to find its HOA and governing documents

## Contributing

If your HOA frustrates you and you know how to code, we'd love your help. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

**Areas where we need help:**
- Tell us what HOA residents actually need — feature ideas, pain points, workflow gaps
- Help us explain this to people — copywriting, outreach, framing
- Bug reports from real usage (the best kind)
- Accessibility audit

## Quick Start

```bash
git clone https://github.com/wyattclarke/hoaproxy.git
cd hoaproxy

# Python 3.10+ required
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set up config
cp settings.env.example settings.env
# Edit settings.env with your API keys

uvicorn api.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Environment Variables

| Variable | Description | Required |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API key for embeddings and Q&A | Required for search/QA |
| `JWT_SECRET` | Secret key for JWT auth tokens | Required |
| `HOA_DOCS_ROOT` | Directory for HOA PDF uploads | Optional (default: `hoa_docs`) |
| `HOA_DB_PATH` | SQLite database path | Optional (default: `data/hoa_index.db`) |
| `QDRANT_URL` | Qdrant vector DB endpoint | Optional (default: embedded local) |
| `HOA_ENABLE_DOCAI` | Enable Google Document AI OCR | Optional (default: `1`) |
| `EMAIL_PROVIDER` | Email backend: `stub`, `resend`, or `smtp` | Optional (default: `stub`) |

See `settings.env.example` for the full list.

## Running Tests

```bash
python -m pytest tests/ -q
```

Tests use an in-memory SQLite database — no Qdrant or OpenAI key needed.

## Stack

FastAPI, SQLite (WAL mode), Qdrant vector DB, OpenAI embeddings, vanilla HTML/CSS/JS frontend. No build step.

## License

[Functional Source License (FSL-1.1-Apache-2.0)](LICENSE.md). You can read, use, and contribute to the code. You cannot use it to run a competing service. Each version converts to Apache 2.0 two years after release.

HOAproxy is an informational tool only. Nothing on this platform constitutes legal advice.

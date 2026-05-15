"""Prepare-side bundle baking — runs OUTSIDE Hetzner.

Builds v2 chunks sidecars containing pre-chunked text + OpenAI embeddings
so the live server can ingest a bundle with zero external API calls.
See docs/upload-acceleration-plan.md.
"""

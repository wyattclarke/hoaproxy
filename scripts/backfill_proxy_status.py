#!/usr/bin/env python3
"""Backfill proxy_status for HOAs that already have documents ingested.

Reads chunk text directly from SQLite (no re-ingestion, no Qdrant calls),
runs the same GPT-4o-mini classification used during ingestion, and updates
hoas.proxy_status + hoas.proxy_citation.

Usage:
    python3 scripts/backfill_proxy_status.py [--force] [--hoa "HOA Name"]

    --force   Re-classify HOAs that already have a non-'unknown' status
    --hoa     Only process one specific HOA (by name)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI
from rich.console import Console
from rich.table import Table

from hoaware import db
from hoaware.config import load_settings

console = Console()

_PROXY_KEYWORDS = {"proxy", "proxies", "absentee ballot", "vote in person", "in-person voting"}


def _classify(texts: list[str], openai_client: OpenAI) -> tuple[str, str | None]:
    """Call GPT-4o-mini to classify proxy allowance. Returns (status, citation)."""
    relevant = [t for t in texts if any(kw in t.lower() for kw in _PROXY_KEYWORDS)]
    if not relevant:
        return "unknown", None

    excerpt = "\n\n---\n\n".join(relevant[:6])
    prompt = (
        "You are analyzing excerpts from HOA (Homeowners Association) governing documents "
        "(bylaws, CC&Rs, or rules). Based only on these excerpts, determine whether proxy "
        "voting is:\n"
        '- "allowed": The documents explicitly permit members to vote by proxy\n'
        '- "not_allowed": The documents explicitly prohibit or restrict proxy voting, '
        "or require in-person voting\n"
        '- "unknown": The excerpts don\'t clearly address proxy voting\n\n'
        f"Excerpts:\n{excerpt}\n\n"
        'Respond with JSON only, no other text: '
        '{"status": "allowed" | "not_allowed" | "unknown", '
        '"citation": "exact quote from the text that supports your determination, or null"}'
    )
    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=200,
    )
    raw = resp.choices[0].message.content or ""
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    result = json.loads(raw)
    status = result.get("status", "unknown")
    citation = result.get("citation") or None
    if status not in ("allowed", "not_allowed", "unknown"):
        status = "unknown"
    return status, citation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="Re-classify already-determined HOAs")
    parser.add_argument("--hoa", metavar="NAME", help="Only process this HOA")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.openai_api_key:
        console.print("[red]OPENAI_API_KEY not set — cannot classify.[/red]")
        sys.exit(1)

    openai_client = OpenAI(api_key=settings.openai_api_key)

    with db.get_connection(settings.db_path) as conn:
        if args.hoa:
            rows = conn.execute(
                "SELECT id, name, proxy_status FROM hoas WHERE name = ?", (args.hoa,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT id, name, proxy_status FROM hoas ORDER BY name").fetchall()

    hoas = [dict(r) for r in rows]
    if not hoas:
        console.print("[yellow]No HOAs found.[/yellow]")
        return

    table = Table("HOA", "Chunks", "Old status", "New status", "Citation snippet")
    updated = skipped = no_chunks = errors = 0

    for hoa in hoas:
        hoa_id = hoa["id"]
        name = hoa["name"]
        current_status = hoa.get("proxy_status") or "unknown"

        if current_status != "unknown" and not args.force:
            skipped += 1
            continue

        with db.get_connection(settings.db_path) as conn:
            texts = db.get_chunk_text_for_hoa(conn, name, limit=200)

        if not texts:
            no_chunks += 1
            table.add_row(name, "0", current_status, "[dim]no docs[/dim]", "")
            continue

        try:
            new_status, citation = _classify(texts, openai_client)
        except Exception as exc:
            errors += 1
            table.add_row(name, str(len(texts)), current_status, f"[red]error: {exc}[/red]", "")
            continue

        with db.get_connection(settings.db_path) as conn:
            db.set_hoa_proxy_status(conn, hoa_id, new_status, citation)

        updated += 1
        citation_snippet = (citation or "")[:60] + ("…" if citation and len(citation) > 60 else "")
        status_fmt = {
            "allowed": "[green]allowed[/green]",
            "not_allowed": "[red]not_allowed[/red]",
            "unknown": "[dim]unknown[/dim]",
        }.get(new_status, new_status)
        table.add_row(name, str(len(texts)), current_status, status_fmt, citation_snippet)

    console.print(table)
    console.print(
        f"\nDone — updated: {updated}, skipped (already set): {skipped}, "
        f"no documents: {no_chunks}, errors: {errors}"
    )


if __name__ == "__main__":
    main()

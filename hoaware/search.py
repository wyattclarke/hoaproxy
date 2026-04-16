from __future__ import annotations

from openai import OpenAI
from rich.console import Console
from rich.table import Table

from . import db
from .config import Settings, load_settings
from .embeddings import batch_embeddings

console = Console()


def search_cli(query: str, hoa_name: str, limit: int = 5, settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    openai_client = OpenAI(api_key=settings.openai_api_key)
    embedding = batch_embeddings([query], openai_client, settings.embedding_model)[0]
    with db.get_connection(settings.db_path) as conn:
        results = db.vector_search(conn, hoa_name, embedding, limit=limit)
    if not results:
        console.print("[yellow]No matches found[/yellow]")
        return
    table = Table(title=f"Top {limit} results for {hoa_name}")
    table.add_column("Score", justify="right")
    table.add_column("Document")
    table.add_column("Pages")
    table.add_column("Excerpt")
    for item in results:
        payload = item["payload"]
        excerpt = payload.get("text", "")[:200].replace("\n", " ")
        table.add_row(
            f"{item['score']:.3f}",
            payload.get("document", "unknown"),
            f"{payload.get('start_page')}–{payload.get('end_page')}",
            excerpt + ("..." if len(payload.get("text", "")) > 200 else ""),
        )
    console.print(table)

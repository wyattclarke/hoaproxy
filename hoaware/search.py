from __future__ import annotations

from openai import OpenAI
from rich.console import Console
from rich.table import Table

from .config import Settings, load_settings, normalize_hoa_name
from .embeddings import batch_embeddings
from .vector_store import build_client, search as qdrant_search, ensure_collection

console = Console()


def search_cli(query: str, hoa_name: str, limit: int = 5, settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    openai_client = OpenAI(api_key=settings.openai_api_key)
    embedding = batch_embeddings([query], openai_client, settings.embedding_model)[0]
    qdrant_client = build_client(settings.qdrant_url, settings.qdrant_api_key)
    collection = f"{settings.collection_prefix}_{normalize_hoa_name(hoa_name)}"
    ensure_collection(qdrant_client, collection)
    results = qdrant_search(
        qdrant_client,
        collection_name=collection,
        query_vector=embedding,
        limit=limit,
        hoa_name=hoa_name,
    )
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

from pathlib import Path
import os
import typer
from rich.console import Console

from .config import load_settings
from .ingest import ingest_hoa
from .search import search_cli
from .qa import answer_question

app = typer.Typer(help="Utilities for ingesting HOA PDFs into Qdrant.")
console = Console()


@app.callback()
def main(
    docs_root: Path = typer.Option(
        None,
        help="Override documents root (defaults to HOAdocs root).",
    ),
    db_path: Path = typer.Option(
        None,
        help="Override SQLite DB path.",
    ),
):
    if docs_root is not None:
        os.environ["HOA_DOCS_ROOT"] = str(docs_root)
    if db_path is not None:
        os.environ["HOA_DB_PATH"] = str(db_path)


@app.command("hoas")
def list_hoas():
    settings = load_settings()
    if not settings.docs_root.exists():
        raise typer.BadParameter(f"{settings.docs_root} does not exist")
    hoas = sorted(
        [p.name for p in settings.docs_root.iterdir() if p.is_dir()],
        key=str.lower,
    )
    for name in hoas:
        console.print(name)


@app.command()
def ingest(hoa_name: str = typer.Argument(..., help="HOA directory name.")):
    ingest_hoa(hoa_name)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query."),
    hoa_name: str = typer.Option(..., "--hoa", help="HOA directory name."),
    limit: int = typer.Option(5, "--limit", "-k"),
):
    search_cli(query, hoa_name=hoa_name, limit=limit)


@app.command()
def qa(
    question: str = typer.Argument(..., help="Question to ask."),
    hoa_name: str = typer.Option(..., "--hoa", help="HOA directory name."),
    limit: int = typer.Option(6, "--limit", "-k", help="Number of chunks to retrieve."),
    model: str = typer.Option("gpt-4o-mini", "--model", help="OpenAI chat model."),
):
    answer_question(question, hoa_name=hoa_name, k=limit, model=model)


def run():
    app()


if __name__ == "__main__":
    run()

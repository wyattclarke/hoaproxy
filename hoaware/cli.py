from pathlib import Path
import os
import subprocess
import sys
import typer
from rich.console import Console

from .config import load_settings
from .ingest import ingest_hoa
from .law import (
    answer_electronic_proxy_questions,
    answer_law_question,
    electronic_proxy_summary,
    list_jurisdictions,
    list_profiles,
)
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


@app.command("law-jurisdictions")
def law_jurisdictions():
    rows = list_jurisdictions()
    for row in rows:
        console.print(
            f"{row['jurisdiction']} profiles={row['profile_count']} "
            f"community_types={row['community_types']} rules={row['rule_count']} "
            f"last_verified={row['last_verified_date'] or 'n/a'}"
        )


@app.command("law-profiles")
def law_profiles(
    jurisdiction: str = typer.Option(None, "--jurisdiction", "-j"),
    community_type: str = typer.Option(None, "--community-type", "-c"),
    entity_form: str = typer.Option(None, "--entity-form", "-e"),
):
    rows = list_profiles(jurisdiction=jurisdiction, community_type=community_type, entity_form=entity_form)
    for row in rows:
        console.print(
            f"{row['jurisdiction']} {row['community_type']} {row['entity_form']} "
            f"rules={row['source_rule_count']} confidence={row['confidence']} "
            f"last_verified={row['last_verified_date'] or 'n/a'}"
        )


@app.command("law-qa")
def law_qa(
    jurisdiction: str = typer.Argument(..., help="2-letter state code, e.g. NC"),
    community_type: str = typer.Option(..., "--community-type", "-c", help="hoa|condo|coop"),
    question_family: str = typer.Option(..., "--question-family", "-q", help="records_and_sharing|proxy_voting"),
    entity_form: str = typer.Option("unknown", "--entity-form", "-e"),
):
    answer = answer_law_question(
        jurisdiction=jurisdiction,
        community_type=community_type,
        question_family=question_family,
        entity_form=entity_form,
    )
    console.print(answer.answer)
    console.print("\nChecklist:")
    for item in answer.checklist:
        console.print(f"- {item}")
    console.print("\nCitations:")
    for cite in answer.citations:
        console.print(f"- {cite['citation']} ({cite.get('citation_url') or 'no-url'})")
    if answer.known_unknowns:
        console.print("\nKnown unknowns:")
        for item in answer.known_unknowns:
            console.print(f"- {item}")
    console.print(f"\nConfidence: {answer.confidence}")
    console.print(f"Last verified: {answer.last_verified_date or 'n/a'}")
    console.print(f"\n{answer.disclaimer}")


@app.command("law-proxy-electronic")
def law_proxy_electronic(
    jurisdiction: str = typer.Argument(..., help="2-letter state code, e.g. FL"),
    community_type: str = typer.Option("hoa", "--community-type", "-c"),
    entity_form: str = typer.Option("unknown", "--entity-form", "-e"),
):
    answer = answer_electronic_proxy_questions(
        jurisdiction=jurisdiction,
        community_type=community_type,
        entity_form=entity_form,
    )
    console.print(f"{answer.jurisdiction} {answer.community_type} {answer.entity_form}")
    console.print(f"- electronic_assignment: {answer.electronic_assignment.status}")
    console.print(f"- electronic_signature: {answer.electronic_signature.status}")
    console.print("\nElectronic assignment evidence:")
    for rule in answer.electronic_assignment.evidence_rules:
        console.print(f"- {rule['rule_type']}: {rule['value_text']}")
    console.print("\nElectronic signature evidence:")
    for rule in answer.electronic_signature.evidence_rules:
        console.print(f"- {rule['rule_type']}: {rule['value_text']}")
    console.print("\nCitations (assignment):")
    for cite in answer.electronic_assignment.citations:
        console.print(f"- {cite['citation']} ({cite.get('citation_url') or 'no-url'})")
    console.print("\nCitations (signature):")
    for cite in answer.electronic_signature.citations:
        console.print(f"- {cite['citation']} ({cite.get('citation_url') or 'no-url'})")
    if answer.known_unknowns:
        console.print("\nKnown unknowns:")
        for item in answer.known_unknowns:
            console.print(f"- {item}")
    console.print(f"\nConfidence: {answer.confidence}")
    console.print(f"Last verified: {answer.last_verified_date or 'n/a'}")
    console.print(f"\n{answer.disclaimer}")


@app.command("law-proxy-electronic-summary")
def law_proxy_electronic_summary(
    community_type: str = typer.Option("hoa", "--community-type", "-c"),
    entity_form: str = typer.Option("unknown", "--entity-form", "-e"),
):
    rows = electronic_proxy_summary(community_type=community_type, entity_form=entity_form)
    for row in rows:
        console.print(
            f"{row['jurisdiction']} assignment={row['electronic_assignment_status']} "
            f"signature={row['electronic_signature_status']} confidence={row['confidence']} "
            f"last_verified={row['last_verified_date'] or 'n/a'}"
        )


@app.command("law-pipeline")
def run_law_pipeline(
    state: str = typer.Option(None, "--state", "-s", help="Optional 2-letter state filter"),
    limit: int = typer.Option(0, "--limit", "-l", help="Optional max records in fetch/normalize/extract"),
    rebuild_source_map: bool = typer.Option(False, "--rebuild-source-map", help="Rebuild source map before run."),
    refresh_fetch: bool = typer.Option(False, "--refresh-fetch", help="Re-fetch URLs even if already present."),
    force_normalize: bool = typer.Option(False, "--force-normalize", help="Re-normalize already normalized snapshots."),
    skip_validate: bool = typer.Option(False, "--skip-validate", help="Skip validation and progress index update."),
    rebuild_proxy_matrix: bool = typer.Option(False, "--rebuild-proxy-matrix", help="Rebuild proxy requirement matrix before run."),
):
    root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, "scripts/legal/run_pipeline.py"]
    if state:
        cmd += ["--state", state]
    if limit and limit > 0:
        cmd += ["--limit", str(limit)]
    if rebuild_source_map:
        cmd.append("--rebuild-source-map")
    if refresh_fetch:
        cmd.append("--refresh-fetch")
    if force_normalize:
        cmd.append("--force-normalize")
    if skip_validate:
        cmd.append("--skip-validate")
    if rebuild_proxy_matrix:
        cmd.append("--rebuild-proxy-matrix")
    subprocess.run(cmd, cwd=str(root), check=True)


def run():
    app()


if __name__ == "__main__":
    run()

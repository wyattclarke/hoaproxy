#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware import db
from hoaware.config import load_settings
from scripts.legal.proxy_matrix import clusters_for_scope, evaluate_proxy_coverage


def _build_summary(rules: list[dict], *, cap: int = 6) -> str | None:
    if not rules:
        return None
    lines = []
    for rule in rules[:cap]:
        lines.append(f"- {rule['rule_type']}: {rule['value_text']}")
    return "\n".join(lines)


def _score_confidence(rules: list[dict], known_gaps: list[str]) -> str:
    if not rules:
        return "low"
    flagged = sum(1 for rule in rules if int(rule.get("needs_human_review") or 0) == 1)
    if known_gaps:
        return "low"
    ratio = flagged / max(len(rules), 1)
    if ratio <= 0.2 and len(rules) >= 8:
        return "high"
    return "medium"


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble denormalized jurisdiction profiles from extracted legal rules.")
    parser.add_argument("--state", type=str, default=None, help="Optional state filter")
    args = parser.parse_args()

    settings = load_settings()
    state_filter = args.state.strip().upper() if args.state else None

    with db.get_connection(settings.db_path) as conn:
        run_id = db.create_legal_ingest_run(
            conn,
            run_phase="assemble_profiles",
            status="running",
            details={"state_filter": state_filter},
        )
        try:
            scope_rows = conn.execute(
                """
                SELECT DISTINCT jurisdiction, community_type, entity_form
                FROM legal_rules
                WHERE (? IS NULL OR jurisdiction = ?)
                ORDER BY jurisdiction, community_type, entity_form
                """,
                (state_filter, state_filter),
            ).fetchall()
            profile_count = 0
            for row in scope_rows:
                jurisdiction = str(row["jurisdiction"])
                community_type = str(row["community_type"])
                entity_form = str(row["entity_form"])
                all_rules = db.list_legal_rules_for_scope(
                    conn,
                    jurisdiction=jurisdiction,
                    community_type=community_type,
                    entity_form=entity_form,
                )
                access_rules = [r for r in all_rules if r["topic_family"] == "records_access"]
                sharing_rules = [r for r in all_rules if r["topic_family"] == "records_sharing_limits"]
                proxy_rules = [r for r in all_rules if r["topic_family"] == "proxy_voting"]
                known_gaps = []
                if not access_rules:
                    known_gaps.append("Missing records_access extraction.")
                if not sharing_rules:
                    known_gaps.append("Missing records_sharing_limits extraction.")
                if not proxy_rules:
                    known_gaps.append("Missing proxy_voting extraction.")
                proxy_clusters = clusters_for_scope(jurisdiction, community_type)
                missing_proxy_clusters, matched_proxy_clusters = evaluate_proxy_coverage(proxy_rules, proxy_clusters)
                if missing_proxy_clusters:
                    known_gaps.append(
                        "Missing proxy coverage clusters: " + ", ".join(missing_proxy_clusters)
                    )

                source_rows = db.list_legal_sources(
                    conn,
                    jurisdiction=jurisdiction,
                    community_type=community_type,
                    entity_form=entity_form,
                )
                governing_law_stack = [
                    {
                        "citation": source["citation"],
                        "citation_url": source["citation_url"],
                        "bucket": source["governing_law_bucket"],
                        "source_type": source["source_type"],
                    }
                    for source in source_rows
                ]
                last_verified = sorted(
                    [str(r["last_verified_date"]) for r in all_rules if r.get("last_verified_date")],
                    reverse=True,
                )
                db.upsert_jurisdiction_profile(
                    conn,
                    jurisdiction=jurisdiction,
                    community_type=community_type,
                    entity_form=entity_form,
                    governing_law_stack=governing_law_stack,
                    records_access_summary=_build_summary(access_rules),
                    records_sharing_limits_summary=_build_summary(sharing_rules),
                    proxy_voting_summary=_build_summary(proxy_rules),
                    conflict_resolution_notes=(
                        "Specific community statutes should control over corporation overlays; "
                        "human review still required for conflicts. "
                        f"Proxy clusters matched: {matched_proxy_clusters}."
                    ),
                    known_gaps=known_gaps,
                    confidence=_score_confidence(all_rules, known_gaps),
                    last_verified_date=last_verified[0] if last_verified else datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    source_rule_count=len(all_rules),
                )
                profile_count += 1

            db.finalize_legal_ingest_run(
                conn,
                run_id=run_id,
                status="completed",
                details={"profiles_upserted": profile_count},
            )
            print(f"Profile assembly complete. profiles_upserted={profile_count}")
        except Exception as exc:  # noqa: BLE001
            db.finalize_legal_ingest_run(
                conn,
                run_id=run_id,
                status="failed",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise


if __name__ == "__main__":
    main()

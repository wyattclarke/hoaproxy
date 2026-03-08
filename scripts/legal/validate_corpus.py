#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware import db
from hoaware.config import load_settings
from scripts.legal.proxy_matrix import clusters_for_scope, evaluate_proxy_coverage


def _electronic_status_from_rule_types(rule_types: set[str], prefix: str) -> str:
    if f"{prefix}_required_acceptance" in rule_types:
        return "required_to_accept"
    if f"{prefix}_prohibited" in rule_types:
        return "restricted_or_rejectable"
    if f"{prefix}_allowed" in rule_types:
        return "allowed"
    return "unclear"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate legal corpus coverage and extraction status.")
    parser.add_argument("--report", type=Path, default=None, help="Optional output report markdown path")
    args = parser.parse_args()

    settings = load_settings()
    source_map_path = settings.legal_source_map_path
    fetched_path = settings.legal_corpus_root / "metadata" / "sources.jsonl"
    normalized_path = settings.legal_corpus_root / "metadata" / "normalized_sources.jsonl"
    extracted_path = settings.legal_corpus_root / "metadata" / "extracted_rules.jsonl"
    fetch_errors_path = settings.legal_corpus_root / "metadata" / "fetch_errors.jsonl"
    electronic_summary_path = Path("data/legal/electronic_proxy_summary.json")

    source_map = json.loads(source_map_path.read_text()) if source_map_path.exists() else []
    fetched = _load_jsonl(fetched_path)
    normalized = _load_jsonl(normalized_path)
    extracted = _load_jsonl(extracted_path)
    fetch_errors = _load_jsonl(fetch_errors_path)
    electronic_summary_rows: list[dict] = []
    if electronic_summary_path.exists():
        try:
            payload = json.loads(electronic_summary_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
                electronic_summary_rows = [row for row in payload["rows"] if isinstance(row, dict)]
        except Exception:
            electronic_summary_rows = []

    by_state_seeded = Counter()
    for row in source_map:
        if row.get("retrieval_status") == "seeded":
            by_state_seeded[str(row["jurisdiction"]).upper()] += 1
    fetched_states = Counter(str(row.get("jurisdiction", "")).upper() for row in fetched)
    normalized_states = Counter(str(row.get("jurisdiction", "")).upper() for row in normalized)
    extracted_states = Counter(str(row.get("jurisdiction", "")).upper() for row in extracted)

    with db.get_connection(settings.db_path) as conn:
        jurisdictions = db.list_law_jurisdictions(conn)
        profiles = db.list_jurisdiction_profiles(conn)
        rules_by_scope = defaultdict(Counter)
        all_rules_by_scope: dict[tuple[str, str, str], list[dict]] = {}
        for profile in profiles:
            rules = db.list_legal_rules_for_scope(
                conn,
                jurisdiction=profile["jurisdiction"],
                community_type=profile["community_type"],
                entity_form=profile["entity_form"],
            )
            key = (profile["jurisdiction"], profile["community_type"], profile["entity_form"])
            all_rules_by_scope[key] = rules
            topic_counter = Counter(r["topic_family"] for r in rules)
            rules_by_scope[key].update(topic_counter)

    report_lines = []
    report_lines.append("# Legal Corpus Validation Report")
    report_lines.append("")
    report_lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    report_lines.append("")
    report_lines.append("## Coverage Summary")
    report_lines.append(f"- Source map rows: {len(source_map)}")
    report_lines.append(f"- Seeded source rows: {sum(by_state_seeded.values())}")
    report_lines.append(f"- Fetched sources: {len(fetched)}")
    report_lines.append(f"- Normalized sources: {len(normalized)}")
    report_lines.append(f"- Extracted rule rows (jsonl): {len(extracted)}")
    report_lines.append(f"- Jurisdictions with assembled profiles: {len(jurisdictions)}")
    report_lines.append(f"- Total jurisdiction profiles: {len(profiles)}")
    report_lines.append("")

    quality_counter = Counter(str(row.get("source_quality") or "unknown").lower() for row in fetched)
    report_lines.append("## Source Quality Summary")
    if not fetched:
        report_lines.append("- No fetched sources available.")
    else:
        for quality in sorted(quality_counter):
            report_lines.append(f"- {quality}: {quality_counter[quality]}")
    report_lines.append("")

    report_lines.append("## State Status (seeded/fetched/normalized/extracted)")
    states = sorted(set(by_state_seeded) | set(fetched_states) | set(normalized_states) | set(extracted_states))
    if not states:
        report_lines.append("- No state data yet.")
    else:
        for state in states:
            report_lines.append(
                f"- {state}: seeded={by_state_seeded[state]} fetched={fetched_states[state]} "
                f"normalized={normalized_states[state]} extracted={extracted_states[state]}"
            )
    report_lines.append("")

    report_lines.append("## Profile Topic Completeness")
    if not profiles:
        report_lines.append("- No profiles found.")
    else:
        for profile in profiles:
            key = (profile["jurisdiction"], profile["community_type"], profile["entity_form"])
            topics = rules_by_scope[key]
            missing = []
            for topic in ("records_access", "records_sharing_limits", "proxy_voting"):
                if topics[topic] <= 0:
                    missing.append(topic)
            missing_text = ", ".join(missing) if missing else "none"
            all_rules = all_rules_by_scope.get(key, [])
            proxy_rules = [r for r in all_rules if r["topic_family"] == "proxy_voting"]
            clusters = clusters_for_scope(profile["jurisdiction"], profile["community_type"])
            missing_proxy_clusters, matched_proxy_clusters = evaluate_proxy_coverage(proxy_rules, clusters)
            proxy_cluster_missing = ", ".join(missing_proxy_clusters) if missing_proxy_clusters else "none"
            report_lines.append(
                f"- {profile['jurisdiction']} {profile['community_type']} {profile['entity_form']}: "
                f"records_access={topics['records_access']} "
                f"records_sharing_limits={topics['records_sharing_limits']} "
                f"proxy_voting={topics['proxy_voting']} "
                f"missing={missing_text} "
                f"proxy_clusters_missing={proxy_cluster_missing} "
                f"proxy_clusters_matched={matched_proxy_clusters} "
                f"confidence={profile['confidence']}"
            )

    report_lines.append("")
    report_lines.append("## Electronic Proxy Question Coverage")
    assignment_counter = Counter()
    signature_counter = Counter()
    unclear_profiles: list[dict] = []
    topic_minimum_failures: list[dict] = []
    aggregator_only_profiles: list[dict] = []
    blocker_states: set[str] = set()
    for error in fetch_errors[-200:]:
        state = str(error.get("jurisdiction") or "").upper()
        if state:
            blocker_states.add(state)
    for profile in profiles:
        key = (profile["jurisdiction"], profile["community_type"], profile["entity_form"])
        all_rules = all_rules_by_scope.get(key, [])
        topics = rules_by_scope.get(key, Counter())
        if topics.get("records_access", 0) <= 0 or topics.get("proxy_voting", 0) <= 0:
            topic_minimum_failures.append(
                {
                    "jurisdiction": profile["jurisdiction"],
                    "community_type": profile["community_type"],
                    "entity_form": profile["entity_form"],
                    "records_access": topics.get("records_access", 0),
                    "proxy_voting": topics.get("proxy_voting", 0),
                }
            )
        if all_rules:
            if all(str(rule.get("source_type") or "").lower() == "secondary_aggregator" for rule in all_rules):
                aggregator_only_profiles.append(
                    {
                        "jurisdiction": profile["jurisdiction"],
                        "community_type": profile["community_type"],
                        "entity_form": profile["entity_form"],
                    }
                )
        rule_types = {str(rule.get("rule_type") or "") for rule in all_rules}
        assignment_status = _electronic_status_from_rule_types(rule_types, "proxy_electronic_assignment")
        signature_status = _electronic_status_from_rule_types(rule_types, "proxy_electronic_signature")
        assignment_counter[assignment_status] += 1
        signature_counter[signature_status] += 1
        if assignment_status == "unclear" or signature_status == "unclear":
            unclear_profiles.append(
                {
                    "jurisdiction": profile["jurisdiction"],
                    "community_type": profile["community_type"],
                    "entity_form": profile["entity_form"],
                }
            )
        report_lines.append(
            f"- {profile['jurisdiction']} {profile['community_type']} {profile['entity_form']}: "
            f"electronic_assignment={assignment_status} electronic_signature={signature_status}"
        )
    report_lines.append(
        "- assignment_status_counts: "
        + ", ".join(f"{status}={assignment_counter[status]}" for status in sorted(assignment_counter))
    )
    report_lines.append(
        "- signature_status_counts: "
        + ", ".join(f"{status}={signature_counter[status]}" for status in sorted(signature_counter))
    )
    report_lines.append("")
    report_lines.append("## Release Gates")
    gate_electronic_non_unclear = len(unclear_profiles) == 0
    gate_min_topic_coverage = len(topic_minimum_failures) == 0
    gate_no_aggregator_only_profiles = len(aggregator_only_profiles) == 0
    report_lines.append(
        f"- gate_electronic_non_unclear: {'PASS' if gate_electronic_non_unclear else 'FAIL'} "
        f"(unclear_profiles={len(unclear_profiles)})"
    )
    report_lines.append(
        f"- gate_min_topic_coverage: {'PASS' if gate_min_topic_coverage else 'FAIL'} "
        f"(failures={len(topic_minimum_failures)})"
    )
    report_lines.append(
        f"- gate_no_aggregator_only_profiles: {'PASS' if gate_no_aggregator_only_profiles else 'FAIL'} "
        f"(aggregator_only_profiles={len(aggregator_only_profiles)})"
    )
    national_unclear_states = sorted(
        {
            str(row.get("jurisdiction") or "").upper()
            for row in electronic_summary_rows
            if row.get("electronic_assignment_status") == "unclear"
            or row.get("electronic_signature_status") == "unclear"
        }
    )
    gate_national_electronic_coverage = len(national_unclear_states) == 0
    report_lines.append(
        f"- gate_national_electronic_coverage: {'PASS' if gate_national_electronic_coverage else 'FAIL'} "
        f"(unclear_states={len(national_unclear_states)})"
    )
    report_lines.append(f"- blocker_states_from_recent_fetch_errors: {', '.join(sorted(blocker_states)) or 'none'}")
    health_payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "source_map_rows": len(source_map),
            "seeded_source_rows": sum(by_state_seeded.values()),
            "fetched_sources": len(fetched),
            "normalized_sources": len(normalized),
            "extracted_rule_rows": len(extracted),
            "jurisdictions_with_profiles": len(jurisdictions),
            "total_profiles": len(profiles),
        },
        "source_quality_counts": dict(sorted(quality_counter.items())),
        "electronic_status_counts": {
            "assignment": dict(sorted(assignment_counter.items())),
            "signature": dict(sorted(signature_counter.items())),
        },
        "gates": {
            "electronic_non_unclear": {
                "pass": gate_electronic_non_unclear,
                "unclear_profiles": unclear_profiles,
            },
            "min_topic_coverage": {
                "pass": gate_min_topic_coverage,
                "failures": topic_minimum_failures,
            },
            "no_aggregator_only_profiles": {
                "pass": gate_no_aggregator_only_profiles,
                "failures": aggregator_only_profiles,
            },
            "national_electronic_coverage": {
                "pass": gate_national_electronic_coverage,
                "unclear_states": national_unclear_states,
                "state_count": len(electronic_summary_rows),
            },
        },
        "blockers": {
            "states_from_recent_fetch_errors": sorted(blocker_states),
            "recent_fetch_errors": fetch_errors[-50:],
        },
    }

    report = "\n".join(report_lines).strip() + "\n"
    report_path = args.report or (settings.legal_corpus_root / "metadata" / "validation_report.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    health_path = Path("data/legal/coverage_health.json")
    health_path.parent.mkdir(parents=True, exist_ok=True)
    health_path.write_text(json.dumps(health_payload, indent=2) + "\n", encoding="utf-8")
    print(report)
    print(f"\nWrote report: {report_path}")
    print(f"Wrote health: {health_path}")


if __name__ == "__main__":
    main()

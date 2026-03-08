#!/usr/bin/env python3
"""Export needs_human_review rules to a structured markdown review queue.

Reads extracted_rules.jsonl and groups rules flagged needs_human_review=1
by state and topic, writing a markdown file that serves as a checklist
for human spot-checking before the corpus is considered production-ready.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware.config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Export human review queue from extracted_rules.jsonl.")
    parser.add_argument("--extracted-jsonl", type=Path, default=None, help="Path to extracted_rules.jsonl")
    parser.add_argument("--out", type=Path, default=None, help="Output markdown path (default: data/legal/human_review_queue.md)")
    parser.add_argument("--state", type=str, default=None, help="Optional: only export rules for this state")
    parser.add_argument(
        "--all-rules",
        action="store_true",
        help="Include all rules (not just needs_human_review=1)",
    )
    args = parser.parse_args()

    settings = load_settings()
    extracted_path = args.extracted_jsonl or (settings.legal_corpus_root / "metadata" / "extracted_rules.jsonl")
    out_path = args.out or (settings.legal_corpus_root.parent / "data" / "legal" / "human_review_queue.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not extracted_path.exists():
        raise SystemExit(f"extracted_rules.jsonl not found: {extracted_path}")

    rows: list[dict] = []
    state_filter = args.state.strip().upper() if args.state else None
    for line in extracted_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if state_filter and str(row.get("jurisdiction", "")).upper() != state_filter:
            continue
        if not args.all_rules and not row.get("needs_human_review"):
            continue
        rows.append(row)

    if not rows:
        print("No rules to review found.")
        out_path.write_text("# Human Review Queue\n\nNo rules requiring review.\n", encoding="utf-8")
        return

    # Group: state -> topic_family -> list of rules
    by_state_topic: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        jur = str(row.get("jurisdiction") or "UNKNOWN").upper()
        topic = str(row.get("topic_family") or "unknown")
        by_state_topic[jur][topic].append(row)

    topic_order = ["records_access", "records_sharing_limits", "proxy_voting"]

    lines: list[str] = [
        "# Human Review Queue",
        "",
        f"Generated from: `{extracted_path}`",
        "",
        f"Total rules to review: **{len(rows)}**",
        "",
        "Each rule was flagged `needs_human_review=1` by the extraction pipeline",
        "(typically because the extracted sentence is longer than 280 chars or",
        "matches multiple rule type patterns). Review for accuracy before marking",
        "the corpus production-ready.",
        "",
        "---",
        "",
    ]

    for state in sorted(by_state_topic.keys()):
        topic_map = by_state_topic[state]
        total_state = sum(len(v) for v in topic_map.values())
        lines.append(f"## {state} ({total_state} rules)")
        lines.append("")

        for topic in topic_order + sorted(set(topic_map.keys()) - set(topic_order)):
            rules = topic_map.get(topic)
            if not rules:
                continue
            lines.append(f"### {state} / {topic} ({len(rules)})")
            lines.append("")
            for i, rule in enumerate(rules[:50], 1):  # cap at 50 per topic per state
                rule_type = rule.get("rule_type", "unknown")
                value_text = rule.get("value_text", "")
                citation = rule.get("citation", "")
                citation_url = rule.get("citation_url", "")
                confidence = rule.get("confidence", "?")
                link = f"[{citation}]({citation_url})" if citation_url else citation
                lines.append(f"**{i}. `{rule_type}`** (confidence: {confidence})")
                lines.append(f"> {value_text}")
                lines.append(f"— {link}")
                lines.append("")
            if len(rules) > 50:
                lines.append(f"_... {len(rules) - 50} more rules not shown. Run with `--state {state}` to see all._")
                lines.append("")

        lines.append("---")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Review queue written to {out_path} ({len(rows)} rules, {len(by_state_topic)} states)")


if __name__ == "__main__":
    main()

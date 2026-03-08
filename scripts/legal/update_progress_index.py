#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware import db
from hoaware.config import load_settings


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main() -> None:
    settings = load_settings()
    source_map_path = settings.legal_source_map_path
    source_map = json.loads(source_map_path.read_text()) if source_map_path.exists() else []

    fetched = _load_jsonl(settings.legal_corpus_root / "metadata" / "sources.jsonl")
    normalized = _load_jsonl(settings.legal_corpus_root / "metadata" / "normalized_sources.jsonl")
    extracted = _load_jsonl(settings.legal_corpus_root / "metadata" / "extracted_rules.jsonl")

    seeded = Counter()
    pending = Counter()
    for row in source_map:
        state = str(row.get("jurisdiction", "")).upper()
        if not state:
            continue
        if row.get("retrieval_status") == "seeded":
            seeded[state] += 1
        else:
            pending[state] += 1
    fetched_counter = Counter(str(r.get("jurisdiction", "")).upper() for r in fetched)
    quality_by_state: dict[str, Counter] = defaultdict(Counter)
    for row in fetched:
        state = str(row.get("jurisdiction", "")).upper()
        if not state:
            continue
        quality = str(row.get("source_quality") or "unknown").lower()
        quality_by_state[state][quality] += 1
    normalized_counter = Counter(str(r.get("jurisdiction", "")).upper() for r in normalized)
    extracted_counter = Counter(str(r.get("jurisdiction", "")).upper() for r in extracted)

    with db.get_connection(settings.db_path) as conn:
        profiles = db.list_jurisdiction_profiles(conn)
        profile_counter = Counter(str(p.get("jurisdiction", "")).upper() for p in profiles)
        profile_conf = defaultdict(list)
        for p in profiles:
            profile_conf[str(p["jurisdiction"]).upper()].append(p["confidence"])

    states = sorted(
        set(seeded.keys())
        | set(pending.keys())
        | set(fetched_counter.keys())
        | set(normalized_counter.keys())
        | set(extracted_counter.keys())
        | set(profile_counter.keys())
    )
    by_state = []
    for state in states:
        by_state.append(
            {
                "jurisdiction": state,
                "seeded_sources": seeded[state],
                "pending_source_slots": pending[state],
                "fetched_snapshots": fetched_counter[state],
                "fetched_source_quality_counts": dict(sorted(quality_by_state[state].items())),
                "normalized_snapshots": normalized_counter[state],
                "extracted_rule_rows": extracted_counter[state],
                "profiles": profile_counter[state],
                "profile_confidence": sorted(profile_conf[state]),
            }
        )
    out = {
        "generated_from": {
            "source_map": str(source_map_path),
            "fetched_jsonl": str(settings.legal_corpus_root / "metadata" / "sources.jsonl"),
            "normalized_jsonl": str(settings.legal_corpus_root / "metadata" / "normalized_sources.jsonl"),
            "extracted_jsonl": str(settings.legal_corpus_root / "metadata" / "extracted_rules.jsonl"),
            "db_path": str(settings.db_path),
        },
        "states": by_state,
    }
    out_path = Path("data/legal/progress_index.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} with {len(by_state)} states")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.legal.build_source_map import US_STATES
from scripts.legal.proxy_matrix import DEFAULT_PROXY_REQUIREMENT_CLUSTERS


COMMUNITY_TYPES = ("hoa", "condo", "coop")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build proxy requirement matrix used for coverage gating.")
    parser.add_argument("--out", type=Path, default=Path("data/legal/proxy_requirement_matrix.json"))
    args = parser.parse_args()

    overrides: dict[str, list[dict]] = {}

    # FL override keeps defaults but explicitly emphasizes direction + duration clusters.
    overrides["FL:hoa"] = [
        {"cluster_id": "permission", "any_of": ["proxy_allowed", "proxy_disallowed"]},
        {"cluster_id": "form", "any_of": ["proxy_form_requirement", "proxy_delivery_requirement"]},
        {"cluster_id": "assignment", "any_of": ["proxy_assignment_rule"]},
        {"cluster_id": "direction", "any_of": ["proxy_directed_option", "proxy_undirected_option"]},
        {"cluster_id": "duration", "any_of": ["proxy_validity_duration"]},
        {"cluster_id": "revocation", "any_of": ["proxy_revocability"]},
        {"cluster_id": "quorum_or_ballot", "any_of": ["proxy_quorum_counting", "proxy_ballot_interaction"]},
        {"cluster_id": "recording_or_inspection", "any_of": ["proxy_record_retention", "proxy_inspection_right"]},
        {
            "cluster_id": "electronic_assignment_policy",
            "any_of": [
                "proxy_electronic_assignment_allowed",
                "proxy_electronic_assignment_required_acceptance",
                "proxy_electronic_assignment_prohibited",
            ],
        },
        {
            "cluster_id": "electronic_signature_policy",
            "any_of": [
                "proxy_electronic_signature_allowed",
                "proxy_electronic_signature_required_acceptance",
                "proxy_electronic_signature_prohibited",
            ],
        },
    ]

    # Add explicit fallback entries for every state/community so matrix is visibly complete.
    for state in US_STATES:
        for community_type in COMMUNITY_TYPES:
            key = f"{state}:{community_type}"
            overrides.setdefault(key, [])

    payload = {
        "version": 1,
        "defaults": DEFAULT_PROXY_REQUIREMENT_CLUSTERS,
        "overrides": overrides,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote proxy requirement matrix to {args.out}")


if __name__ == "__main__":
    main()

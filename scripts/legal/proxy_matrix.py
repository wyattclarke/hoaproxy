#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


DEFAULT_PROXY_REQUIREMENT_CLUSTERS = [
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


def _matrix_path() -> Path:
    return Path("data/legal/proxy_requirement_matrix.json")


def _normalize_state(state: str) -> str:
    return state.strip().upper()


def _normalize_community(community_type: str) -> str:
    return community_type.strip().lower()


def load_proxy_requirement_matrix() -> dict:
    path = _matrix_path()
    if not path.exists():
        return {"version": 1, "defaults": DEFAULT_PROXY_REQUIREMENT_CLUSTERS, "overrides": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "defaults": DEFAULT_PROXY_REQUIREMENT_CLUSTERS, "overrides": {}}
    defaults = payload.get("defaults")
    overrides = payload.get("overrides")
    if not isinstance(defaults, list):
        defaults = DEFAULT_PROXY_REQUIREMENT_CLUSTERS
    if not isinstance(overrides, dict):
        overrides = {}
    return {"version": int(payload.get("version") or 1), "defaults": defaults, "overrides": overrides}


def clusters_for_scope(jurisdiction: str, community_type: str) -> list[dict]:
    matrix = load_proxy_requirement_matrix()
    defaults = list(matrix["defaults"])
    overrides = matrix["overrides"]
    key = f"{_normalize_state(jurisdiction)}:{_normalize_community(community_type)}"
    scoped = overrides.get(key)
    if isinstance(scoped, list) and scoped:
        return scoped
    return defaults


def evaluate_proxy_coverage(proxy_rules: Iterable[dict], clusters: list[dict]) -> tuple[list[str], dict[str, list[str]]]:
    rule_types = {str(rule.get("rule_type") or "").strip().lower() for rule in proxy_rules if rule.get("rule_type")}
    missing_clusters: list[str] = []
    matched: dict[str, list[str]] = {}
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "unknown")
        any_of = [str(item).strip().lower() for item in cluster.get("any_of", []) if str(item).strip()]
        hits = sorted([rule for rule in any_of if rule in rule_types])
        if hits:
            matched[cluster_id] = hits
        else:
            missing_clusters.append(cluster_id)
    return missing_clusters, matched

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware import db
from hoaware.config import load_settings
from scripts.legal.source_quality import classify_source_quality, extraction_allowed


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+")
DAY_COUNT_RE = re.compile(r"\b(within|not later than)\s+(\d+)\s+(business\s+)?days?\b", re.IGNORECASE)
FEE_CAP_RE = re.compile(r"\$\s*[\d]+(?:\.\d+)?\s*(?:per\s+(?:page|copy|hour|photo|item))?", re.IGNORECASE)
ELECTRONIC_PROXY_TERMS = [
    "electronic",
    "e-mail",
    "email",
    "electronic transmission",
    "facsimile",
    "fax",
    "remote communication",
    "reliable reproduction",
    "digital signature",
    "docusign",
]
ELECTRONIC_OVERLAY_TERMS = [
    "electronic signature",
    "electronic record",
    "electronic form",
    "electronic means",
    "digital signature",
]
ELECTRONIC_REQUIRE_ACCEPT_TERMS = [
    "shall accept",
    "shall be accepted",
    "must accept",
    "must be accepted",
    "may not reject",
    "is valid",
    "is deemed",
]
ELECTRONIC_RESTRICT_TERMS = [
    "may reject",
    "not valid",
    "invalid",
    "reject",
    "doubting the validity",
]
ELECTRONIC_LEGAL_EFFECT_TERMS = [
    "may not be denied legal effect",
    "shall not be denied legal effect",
    "may not be denied enforceability",
    "shall not be denied enforceability",
    "satisfies the law",
    "satisfy the law",
    "satisfies any law",
    "satisfy any law",
]
ELECTRONIC_RECORD_EQUIVALENCE_TERMS = [
    "record satisfies",
    "electronic record satisfies",
    "requirement is satisfied by an electronic record",
    "if a law requires a record to be in writing",
    "writing, an electronic record satisfies",
]
ELECTRONIC_SIGNATURE_EQUIVALENCE_TERMS = [
    "signature satisfies",
    "electronic signature satisfies",
    "requirement is satisfied by an electronic signature",
    "if a law requires a signature",
]
ELECTRONIC_OVERLAY_EXCLUSION_TERMS = [
    "does not apply to",
    "excluded transaction",
    "except as otherwise provided",
]


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _classify_electronic_proxy_rule(lower: str) -> tuple[str, str] | None:
    has_electronic = any(term in lower for term in ELECTRONIC_PROXY_TERMS)
    if not has_electronic:
        return None
    signature_context = "signature" in lower or "signed" in lower or "execute" in lower
    require_accept = any(term in lower for term in ELECTRONIC_REQUIRE_ACCEPT_TERMS)
    restrict = any(term in lower for term in ELECTRONIC_RESTRICT_TERMS)
    if signature_context:
        if require_accept:
            return ("proxy_voting", "proxy_electronic_signature_required_acceptance")
        if restrict:
            return ("proxy_voting", "proxy_electronic_signature_prohibited")
        return ("proxy_voting", "proxy_electronic_signature_allowed")
    if require_accept:
        return ("proxy_voting", "proxy_electronic_assignment_required_acceptance")
    if restrict:
        return ("proxy_voting", "proxy_electronic_assignment_prohibited")
    return ("proxy_voting", "proxy_electronic_assignment_allowed")


def _infer_electronic_overlay_proxy_rules(lower: str, *, bucket: str | None = None) -> list[tuple[str, str]]:
    bucket_lower = (bucket or "").lower()
    if bucket_lower != "electronic_transactions_overlay":
        return []
    if not any(term in lower for term in ELECTRONIC_OVERLAY_TERMS):
        return []
    if any(term in lower for term in ELECTRONIC_OVERLAY_EXCLUSION_TERMS):
        # Many exclusion clauses are meta/limiting language and should not alone
        # drive a proxy e-signature rule.
        return []

    inferences: list[tuple[str, str]] = []
    has_signature = "signature" in lower or "signed" in lower
    has_record = "record" in lower or "writing" in lower or "written" in lower
    legal_effect = any(term in lower for term in ELECTRONIC_LEGAL_EFFECT_TERMS)
    signature_equivalence = any(term in lower for term in ELECTRONIC_SIGNATURE_EQUIVALENCE_TERMS)
    record_equivalence = any(term in lower for term in ELECTRONIC_RECORD_EQUIVALENCE_TERMS)
    require_accept = any(term in lower for term in ELECTRONIC_REQUIRE_ACCEPT_TERMS)
    restrict = any(term in lower for term in ELECTRONIC_RESTRICT_TERMS)

    if has_signature:
        if restrict:
            inferences.append(("proxy_voting", "proxy_electronic_signature_prohibited"))
        elif require_accept or legal_effect or signature_equivalence:
            inferences.append(("proxy_voting", "proxy_electronic_signature_required_acceptance"))
        else:
            inferences.append(("proxy_voting", "proxy_electronic_signature_allowed"))

    if has_record:
        if restrict:
            inferences.append(("proxy_voting", "proxy_electronic_assignment_prohibited"))
        elif require_accept or legal_effect or record_equivalence:
            inferences.append(("proxy_voting", "proxy_electronic_assignment_required_acceptance"))
        else:
            inferences.append(("proxy_voting", "proxy_electronic_assignment_allowed"))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in inferences:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _classify_sentence(sentence: str, *, bucket: str | None = None) -> tuple[str, str] | None:
    lower = sentence.lower()
    bucket_lower = (bucket or "").lower()
    proxy_context = "proxy" in lower or "ballot" in lower or "quorum" in lower
    record_context = any(
        word in lower
        for word in [
            "inspect",
            "inspection",
            "copy",
            "records",
            "record book",
            "books and records",
            "association records",
            "governing documents",
            "membership list",
            "member list",
            "unit owner list",
            "roster",
            "financial statement",
            "financial record",
            "financial document",
            "balance sheet",
            "meeting minutes",
            "board minutes",
            "annual report",
            "audit report",
            "tax return",
            "budget",
            "reserve fund",
            "examine and copy",
            "right to examine",
            "available for inspection",
            "available for examination",
            "open to inspection",
            "public record",
        ]
    )

    if proxy_context:
        if "proxy" not in lower and bucket_lower not in {"proxy_voting", "nonprofit_corp_overlay", "business_corp_overlay"}:
            return None
        electronic_rule = _classify_electronic_proxy_rule(lower)
        if electronic_rule is not None:
            return electronic_rule
        if "directed" in lower or "limited proxy" in lower:
            return ("proxy_voting", "proxy_directed_option")
        if "undirected" in lower or "discretion" in lower or "general proxy" in lower:
            return ("proxy_voting", "proxy_undirected_option")
        if any(word in lower for word in ["appoint", "designate", "holder", "assign"]):
            return ("proxy_voting", "proxy_assignment_rule")
        if "revok" in lower or "at the pleasure" in lower:
            return ("proxy_voting", "proxy_revocability")
        if any(
            phrase in lower
            for phrase in [
                "expire",
                "valid for",
                "validity",
                "11 month",
                "eleven month",
                "unless otherwise provided",
                "effective only for the specific meeting",
                "lawfully adjourned",
            ]
        ):
            return ("proxy_voting", "proxy_validity_duration")
        if any(word in lower for word in ["record", "retain", "minutes", "maintain"]):
            return ("proxy_voting", "proxy_record_retention")
        if any(word in lower for word in ["signed", "signature", "writing", "written", "electronic transmission"]):
            return ("proxy_voting", "proxy_form_requirement")
        if "deliver" in lower or "submitted" in lower:
            return ("proxy_voting", "proxy_delivery_requirement")
        if "quorum" in lower:
            return ("proxy_voting", "proxy_quorum_counting")
        if "ballot" in lower and "proxy" in lower:
            return ("proxy_voting", "proxy_ballot_interaction")
        if any(word in lower for word in ["inspect", "inspection", "available to members"]):
            return ("proxy_voting", "proxy_inspection_right")
        return ("proxy_voting", "proxy_allowed")

    if record_context:
        # Withheld/exempt records — classify before general sharing limits
        if any(
            word in lower
            for word in [
                "attorney-client",
                "privileged",
                "personnel",
                "private",
                "social security",
                "medical",
                "litigation",
                "executive session",
            ]
        ):
            return ("records_sharing_limits", "sharing_privacy_redaction")

        # Member list use restrictions
        if "member list" in lower or "membership list" in lower or "unit owner list" in lower or "roster" in lower:
            if any(word in lower for word in ["commercial", "solicit", "sale", "shall not be used", "may not be used", "limited to"]):
                return ("records_sharing_limits", "sharing_member_list_use_restriction")
            return ("records_access", "records_member_list_access")

        # General sharing restrictions
        if any(
            phrase in lower
            for phrase in ["shall not be used for", "may not be used for", "may not be sold", "not be disclosed"]
        ):
            return ("records_sharing_limits", "sharing_use_restriction")

        # Response deadlines
        if DAY_COUNT_RE.search(sentence) is not None:
            return ("records_access", "records_response_deadline")

        # Request formality
        if "written request" in lower or "request in writing" in lower or "written demand" in lower:
            return ("records_access", "records_request_form")

        # Fee limits — check dollar amount regex first for precision
        if FEE_CAP_RE.search(sentence) or any(
            phrase in lower
            for phrase in ["reasonable fee", "actual cost", "direct cost", "no charge", "without charge", "free of charge", "at cost"]
        ):
            return ("records_access", "records_fee_limit")

        # More general fee terms as fallback
        if any(word in lower for word in ["fee", "charge", "cost of copying", "copying fee"]):
            return ("records_access", "records_fee_limit")

        # Specific document types entitled to inspection
        if any(
            phrase in lower
            for phrase in [
                "financial statement",
                "balance sheet",
                "income statement",
                "budget",
                "audit report",
                "tax return",
                "invoices",
                "receipts",
                "canceled check",
                "purchase order",
                "credit card statement",
                "meeting minutes",
                "board minutes",
                "annual report",
                "treasurer",
                "reserve fund",
            ]
        ):
            return ("records_access", "records_document_types")

        # Public recording / county recorder
        if any(word in lower for word in ["recorded", "register of deeds", "county recorder", "recording", "public record"]):
            return ("records_access", "records_governing_docs_public_recording")

        # Retention requirements
        if any(
            phrase in lower
            for phrase in ["shall keep", "must keep", "shall maintain", "must maintain", "retain", "retained for", "preservation of"]
        ):
            return ("records_access", "records_retention_requirement")

        # Electronic delivery / format options
        if any(
            phrase in lower
            for phrase in ["electronic transmission", "electronic format", "email", "e-mail", "digital copy", "photocopying"]
        ):
            return ("records_access", "records_delivery_format")

        return ("records_access", "records_inspection_right")
    return None


def _extract_rules_from_text(text: str, *, bucket: str | None = None) -> list[dict]:
    rules: list[dict] = []
    for sentence in SENTENCE_SPLIT_RE.split(text):
        cleaned = " ".join(sentence.strip().split())
        if len(cleaned) < 25:
            continue
        if len(cleaned) > 800:
            continue
        lower = cleaned.lower()
        classifications: list[tuple[str, str]] = []
        classifications.extend(_infer_electronic_overlay_proxy_rules(lower, bucket=bucket))
        classification = _classify_sentence(cleaned, bucket=bucket)
        if classification is not None:
            classifications.append(classification)
        if not classifications:
            continue
        confidence = "high" if len(cleaned) <= 280 else "medium"
        for topic_family, rule_type in classifications:
            rules.append(
                {
                    "topic_family": topic_family,
                    "rule_type": rule_type,
                    "value_text": cleaned,
                    "confidence": confidence,
                    "needs_human_review": 0 if confidence == "high" else 1,
                }
            )
    return rules


def _checksum_for_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dedupe_normalized_rows(rows: list[dict]) -> list[dict]:
    deduped: dict[tuple[str, str, str, str, str, str, str], dict] = {}
    for row in rows:
        key = (
            str(row.get("jurisdiction") or "").upper(),
            str(row.get("community_type") or "").lower(),
            str(row.get("entity_form") or "unknown").lower(),
            str(row.get("governing_law_bucket") or "").lower(),
            str(row.get("citation") or ""),
            str(row.get("source_url") or ""),
            str(row.get("raw_text_checksum_sha256") or ""),
        )
        previous = deduped.get(key)
        if previous is None or str(row.get("normalized_at") or "") > str(previous.get("normalized_at") or ""):
            deduped[key] = row
    return list(deduped.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract normalized legal rules and load into SQLite legal tables.")
    parser.add_argument("--normalized-jsonl", type=Path, default=None, help="Path to normalized_sources.jsonl")
    parser.add_argument("--state", type=str, default=None, help="Optional state filter")
    parser.add_argument("--limit", type=int, default=0, help="Optional max rows")
    parser.add_argument(
        "--include-aggregators",
        action="store_true",
        help="Include aggregator sources during extraction (default: off).",
    )
    args = parser.parse_args()

    settings = load_settings()
    normalized_path = args.normalized_jsonl or (settings.legal_corpus_root / "metadata" / "normalized_sources.jsonl")
    if not normalized_path.exists():
        raise SystemExit(f"Normalized metadata not found: {normalized_path}")
    source_map_rows = json.loads(settings.legal_source_map_path.read_text(encoding="utf-8")) if settings.legal_source_map_path.exists() else []
    allowed_scopes: set[tuple[str, str, str, str, str]] = set()
    for row in source_map_rows:
        if str(row.get("retrieval_status") or "").lower() not in {"seeded", "verified"}:
            continue
        source_url = str(row.get("source_url") or "").strip()
        if not source_url:
            continue
        allowed_scopes.add(
            (
                str(row.get("jurisdiction") or "").upper(),
                str(row.get("community_type") or "").lower(),
                str(row.get("entity_form") or "unknown").lower(),
                str(row.get("governing_law_bucket") or "").lower(),
                source_url,
            )
        )

    rows: list[dict] = []
    state_filter = args.state.strip().upper() if args.state else None
    skipped_not_seeded = 0
    for line in normalized_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if state_filter and str(row.get("jurisdiction", "")).upper() != state_filter:
            continue
        scope = (
            str(row.get("jurisdiction") or "").upper(),
            str(row.get("community_type") or "").lower(),
            str(row.get("entity_form") or "unknown").lower(),
            str(row.get("governing_law_bucket") or "").lower(),
            str(row.get("source_url") or "").strip(),
        )
        if allowed_scopes and scope not in allowed_scopes:
            skipped_not_seeded += 1
            continue
        rows.append(row)
    rows = _dedupe_normalized_rows(rows)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    extracted_out = settings.legal_corpus_root / "metadata" / "extracted_rules.jsonl"
    by_scope_and_topic: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    extracted_rows_out: list[dict] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with db.get_connection(settings.db_path) as conn:
        run_id = db.create_legal_ingest_run(
            conn,
            run_phase="extract_rules",
            status="running",
            details={"rows": len(rows), "state_filter": state_filter},
        )
        try:
            skipped_quality = 0
            for row in rows:
                jurisdiction = str(row["jurisdiction"]).upper()
                community_type = str(row["community_type"]).lower()
                entity_form = str(row.get("entity_form") or "unknown").lower()
                bucket = str(row["governing_law_bucket"]).lower()
                citation = str(row.get("citation") or "Unknown citation")
                citation_url = str(row.get("source_url") or "")
                source_quality = str(row.get("source_quality") or "").strip().lower() or classify_source_quality(
                    source_type=str(row.get("source_type") or "unknown"),
                    source_url=citation_url,
                )
                if not extraction_allowed(
                    source_quality=source_quality,
                    include_aggregators=args.include_aggregators,
                ):
                    skipped_quality += 1
                    continue
                source_id = db.upsert_legal_source(
                    conn,
                    jurisdiction=jurisdiction,
                    community_type=community_type,
                    entity_form=entity_form,
                    governing_law_bucket=bucket,
                    source_type=str(row.get("source_type") or "statute"),
                    citation=citation,
                    citation_url=citation_url,
                    publisher=str(row.get("publisher")) if row.get("publisher") else None,
                    last_verified_date=str(row.get("last_verified_date") or now),
                    checksum_sha256=str(row.get("raw_text_checksum_sha256") or ""),
                    snapshot_path=str(row.get("snapshot_path") or ""),
                    parser_version="normalize_law_texts_v1",
                    notes=f"Auto-loaded from normalized legal corpus. source_quality={source_quality}",
                )
                sections = row.get("sections", [])
                section_rows = []
                for section in sections:
                    text = str(section.get("text") or "").strip()
                    if not text:
                        continue
                    section_rows.append(
                        {
                            "section_key": str(section.get("section_key") or "unknown"),
                            "heading": section.get("heading"),
                            "text": text,
                            "checksum_sha256": _checksum_for_text(text),
                        }
                    )
                db.replace_legal_sections(conn, source_id=source_id, sections=section_rows)
                scope_rules = []
                for section in section_rows:
                    rule_rows = _extract_rules_from_text(section["text"], bucket=bucket)
                    for rule in rule_rows:
                        normalized_rule = {
                            "topic_family": rule["topic_family"],
                            "rule_type": rule["rule_type"],
                            "value_text": rule["value_text"],
                            "citation": citation,
                            "citation_url": citation_url or None,
                                "source_id": source_id,
                                "source_quality": source_quality,
                                "last_verified_date": str(row.get("last_verified_date") or now),
                                "confidence": rule["confidence"],
                                "needs_human_review": rule["needs_human_review"],
                        }
                        scope_rules.append(normalized_rule)
                        extracted_rows_out.append(
                            {
                                "extracted_at": now,
                                "jurisdiction": jurisdiction,
                                "community_type": community_type,
                                "entity_form": entity_form,
                                **normalized_rule,
                            }
                        )
                for rule in scope_rules:
                    key = (jurisdiction, community_type, entity_form, rule["topic_family"])
                    by_scope_and_topic[key].append(rule)

            inserted_total = 0
            for key, rules in by_scope_and_topic.items():
                jurisdiction, community_type, entity_form, topic_family = key
                deduped = []
                seen = set()
                for rule in rules:
                    sig = (rule["rule_type"], rule["value_text"], rule["citation"])
                    if sig in seen:
                        continue
                    seen.add(sig)
                    deduped.append(rule)
                inserted = db.replace_legal_rules_for_scope(
                    conn,
                    jurisdiction=jurisdiction,
                    community_type=community_type,
                    entity_form=entity_form,
                    topic_family=topic_family,
                    rules=deduped,
                )
                inserted_total += inserted

            db.finalize_legal_ingest_run(
                conn,
                run_id=run_id,
                status="completed",
                details={
                    "sources_processed": len(rows),
                    "scope_topic_rows": len(by_scope_and_topic),
                    "rules_inserted": inserted_total,
                    "skipped_quality": skipped_quality,
                    "skipped_not_seeded": skipped_not_seeded,
                },
            )
            existing_extracted = _load_jsonl(extracted_out)
            processed_states = {str(row.get("jurisdiction") or "").upper() for row in rows}
            merged_rows = [
                row
                for row in existing_extracted
                if str(row.get("jurisdiction") or "").upper() not in processed_states
            ]
            merged_rows.extend(extracted_rows_out)
            dedupe_map: dict[tuple[str, str, str, str, str, str, str], dict] = {}
            for row in merged_rows:
                key = (
                    str(row.get("jurisdiction") or "").upper(),
                    str(row.get("community_type") or "").lower(),
                    str(row.get("entity_form") or "unknown").lower(),
                    str(row.get("topic_family") or ""),
                    str(row.get("rule_type") or ""),
                    str(row.get("value_text") or ""),
                    str(row.get("citation") or ""),
                )
                previous = dedupe_map.get(key)
                if previous is None or str(row.get("extracted_at") or "") > str(previous.get("extracted_at") or ""):
                    dedupe_map[key] = row
            rows_out = sorted(
                dedupe_map.values(),
                key=lambda row: (
                    str(row.get("jurisdiction") or ""),
                    str(row.get("community_type") or ""),
                    str(row.get("topic_family") or ""),
                    str(row.get("rule_type") or ""),
                    str(row.get("citation") or ""),
                ),
            )
            _write_jsonl(extracted_out, rows_out)
            print(
                "Extraction complete. "
                f"sources_processed={len(rows)} scope_topic_rows={len(by_scope_and_topic)} "
                f"rules_inserted={inserted_total} extracted_jsonl_rows={len(rows_out)} "
                f"skipped_quality={skipped_quality} skipped_not_seeded={skipped_not_seeded}"
            )
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

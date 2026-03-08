from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable

from . import db
from .config import Settings, load_settings


DISCLAIMER = (
    "This summary is for informational purposes only and is not legal advice. "
    "Consult a licensed attorney in the relevant jurisdiction for legal interpretation."
)

QUESTION_FAMILIES = {"records_and_sharing", "proxy_voting"}
COMMUNITY_TYPES = {"hoa", "condo", "coop"}
ENTITY_FORMS = {"nonprofit_corp", "for_profit_corp", "unincorporated", "unknown"}


@dataclass
class LawAnswer:
    answer: str
    checklist: list[str]
    citations: list[dict]
    known_unknowns: list[str]
    confidence: str
    last_verified_date: str | None
    disclaimer: str = DISCLAIMER


@dataclass
class ElectronicProxyStatus:
    status: str
    evidence_rules: list[dict]
    citations: list[dict]


@dataclass
class ElectronicProxyAnswer:
    jurisdiction: str
    community_type: str
    entity_form: str
    electronic_assignment: ElectronicProxyStatus
    electronic_signature: ElectronicProxyStatus
    known_unknowns: list[str]
    confidence: str
    last_verified_date: str | None
    disclaimer: str = DISCLAIMER


def normalize_jurisdiction(value: str) -> str:
    cleaned = value.strip().upper()
    if len(cleaned) != 2 or not cleaned.isalpha():
        raise ValueError("jurisdiction must be a 2-letter code, e.g. NC")
    return cleaned


def normalize_community_type(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned not in COMMUNITY_TYPES:
        raise ValueError("community_type must be one of: hoa, condo, coop")
    return cleaned


def normalize_entity_form(value: str | None) -> str:
    cleaned = (value or "unknown").strip().lower()
    if cleaned not in ENTITY_FORMS:
        raise ValueError("entity_form must be one of: nonprofit_corp, for_profit_corp, unincorporated, unknown")
    return cleaned


def normalize_question_family(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned not in QUESTION_FAMILIES:
        raise ValueError("question_family must be one of: records_and_sharing, proxy_voting")
    return cleaned


def list_jurisdictions(settings: Settings | None = None) -> list[dict]:
    settings = settings or load_settings()
    with db.get_connection(settings.db_path) as conn:
        return db.list_law_jurisdictions(conn)


def list_profiles(
    *,
    jurisdiction: str | None = None,
    community_type: str | None = None,
    entity_form: str | None = None,
    settings: Settings | None = None,
) -> list[dict]:
    settings = settings or load_settings()
    with db.get_connection(settings.db_path) as conn:
        return db.list_jurisdiction_profiles(
            conn,
            jurisdiction=normalize_jurisdiction(jurisdiction) if jurisdiction else None,
            community_type=normalize_community_type(community_type) if community_type else None,
            entity_form=normalize_entity_form(entity_form) if entity_form else None,
        )


def _topic_families_for_question(question_family: str) -> list[str]:
    if question_family == "records_and_sharing":
        return ["records_access", "records_sharing_limits"]
    return ["proxy_voting"]


def _dedupe_citations(rules: Iterable[dict], *, cap: int = 25) -> list[dict]:
    citations: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for rule in rules:
        citation = str(rule.get("citation") or "").strip()
        citation_url = str(rule.get("citation_url") or "").strip()
        if not citation:
            continue
        key = (citation, citation_url)
        if key in seen:
            continue
        seen.add(key)
        excerpt = str(rule.get("value_text") or "").strip()
        if len(excerpt) > 240:
            excerpt = excerpt[:237].rstrip() + "..."
        citations.append(
            {
                "citation": citation,
                "citation_url": citation_url or None,
                "source_type": str(rule.get("source_type") or "unknown"),
                "excerpt": excerpt,
                "last_verified_date": rule.get("last_verified_date"),
            }
        )
        if len(citations) >= cap:
            break
    return citations


def _confidence_from(profile: dict | None, rules: list[dict]) -> str:
    if not rules:
        return "low"
    if profile is None:
        return "low"
    base = str(profile.get("confidence") or "low").lower()
    if profile.get("known_gaps"):
        return "medium" if base == "high" else "low"
    return base


def _status_from_rule_types(rule_types: set[str], *, prefix: str) -> str:
    required = f"{prefix}_required_acceptance"
    prohibited = f"{prefix}_prohibited"
    allowed = f"{prefix}_allowed"
    if required in rule_types:
        return "required_to_accept"
    if prohibited in rule_types:
        return "restricted_or_rejectable"
    if allowed in rule_types:
        return "allowed"
    return "unclear"


def _electronic_proxy_status(rules: list[dict], *, prefix: str) -> ElectronicProxyStatus:
    filtered = [rule for rule in rules if str(rule.get("rule_type", "")).startswith(prefix)]
    rule_types = {str(rule.get("rule_type") or "") for rule in filtered}
    return ElectronicProxyStatus(
        status=_status_from_rule_types(rule_types, prefix=prefix),
        evidence_rules=filtered[:12],
        citations=_dedupe_citations(filtered),
    )


def _records_answer(profile: dict | None, rules: list[dict]) -> tuple[str, list[str], list[str]]:
    access = [r for r in rules if r.get("topic_family") == "records_access"]
    sharing = [r for r in rules if r.get("topic_family") == "records_sharing_limits"]
    checklist: list[str] = []
    checklist.extend([f"{r.get('rule_type')}: {r.get('value_text')}" for r in access[:8]])
    checklist.extend([f"{r.get('rule_type')}: {r.get('value_text')}" for r in sharing[:8]])
    known_unknowns = list(profile.get("known_gaps", [])) if profile else []
    if not access:
        known_unknowns.append("No extracted records_access rules were found for this scope.")
    if not sharing:
        known_unknowns.append("No extracted records_sharing_limits rules were found for this scope.")
    summary_access = (profile or {}).get("records_access_summary")
    summary_sharing = (profile or {}).get("records_sharing_limits_summary")
    parts = []
    if summary_access:
        parts.append(f"Records access: {summary_access}")
    if summary_sharing:
        parts.append(f"Sharing limits: {summary_sharing}")
    if not parts:
        parts.append("No jurisdiction profile summary is available yet; checklist and citations reflect extracted rules only.")
    return "\n\n".join(parts), checklist, known_unknowns


def _proxy_answer(profile: dict | None, rules: list[dict]) -> tuple[str, list[str], list[str]]:
    checklist = [f"{r.get('rule_type')}: {r.get('value_text')}" for r in rules[:12]]
    known_unknowns = list(profile.get("known_gaps", [])) if profile else []
    if not rules:
        known_unknowns.append("No extracted proxy_voting rules were found for this scope.")
    summary_proxy = (profile or {}).get("proxy_voting_summary")
    answer = (
        f"Proxy voting: {summary_proxy}"
        if summary_proxy
        else "No jurisdiction profile summary is available yet; checklist and citations reflect extracted proxy rules only."
    )
    return answer, checklist, known_unknowns


def answer_law_question(
    *,
    jurisdiction: str,
    community_type: str,
    question_family: str,
    entity_form: str | None = None,
    settings: Settings | None = None,
) -> LawAnswer:
    settings = settings or load_settings()
    jurisdiction_norm = normalize_jurisdiction(jurisdiction)
    community_norm = normalize_community_type(community_type)
    family_norm = normalize_question_family(question_family)
    entity_norm = normalize_entity_form(entity_form)

    with db.get_connection(settings.db_path) as conn:
        profile = db.get_jurisdiction_profile(
            conn,
            jurisdiction=jurisdiction_norm,
            community_type=community_norm,
            entity_form=entity_norm,
        )
        topics = _topic_families_for_question(family_norm)
        all_rules: list[dict] = []
        for topic in topics:
            topic_rules = db.list_legal_rules_for_scope(
                conn,
                jurisdiction=jurisdiction_norm,
                community_type=community_norm,
                entity_form=entity_norm,
                topic_family=topic,
            )
            all_rules.extend(topic_rules)

    if family_norm == "records_and_sharing":
        answer, checklist, known_unknowns = _records_answer(profile, all_rules)
    else:
        answer, checklist, known_unknowns = _proxy_answer(profile, all_rules)

    citations = _dedupe_citations(all_rules)
    confidence = _confidence_from(profile, all_rules)
    return LawAnswer(
        answer=answer,
        checklist=checklist,
        citations=citations,
        known_unknowns=known_unknowns,
        confidence=confidence,
        last_verified_date=(profile or {}).get("last_verified_date"),
    )


def answer_electronic_proxy_questions(
    *,
    jurisdiction: str,
    community_type: str,
    entity_form: str | None = None,
    settings: Settings | None = None,
) -> ElectronicProxyAnswer:
    settings = settings or load_settings()
    jurisdiction_norm = normalize_jurisdiction(jurisdiction)
    community_norm = normalize_community_type(community_type)
    entity_norm = normalize_entity_form(entity_form)
    with db.get_connection(settings.db_path) as conn:
        profile = db.get_jurisdiction_profile(
            conn,
            jurisdiction=jurisdiction_norm,
            community_type=community_norm,
            entity_form=entity_norm,
        )
        proxy_rules = db.list_legal_rules_for_scope(
            conn,
            jurisdiction=jurisdiction_norm,
            community_type=community_norm,
            entity_form=entity_norm,
            topic_family="proxy_voting",
        )
    assignment = _electronic_proxy_status(proxy_rules, prefix="proxy_electronic_assignment")
    signature = _electronic_proxy_status(proxy_rules, prefix="proxy_electronic_signature")
    known_unknowns = list((profile or {}).get("known_gaps", []))
    if assignment.status == "unclear":
        known_unknowns.append("No extracted rule clearly states whether electronic proxy assignment must or may be accepted.")
    if signature.status == "unclear":
        known_unknowns.append("No extracted rule clearly states whether electronic signatures for proxy assignments must or may be accepted.")
    return ElectronicProxyAnswer(
        jurisdiction=jurisdiction_norm,
        community_type=community_norm,
        entity_form=entity_norm,
        electronic_assignment=assignment,
        electronic_signature=signature,
        known_unknowns=known_unknowns,
        confidence=_confidence_from(profile, proxy_rules),
        last_verified_date=(profile or {}).get("last_verified_date"),
    )


def electronic_proxy_summary(
    *,
    community_type: str,
    entity_form: str = "unknown",
    states: list[str] | None = None,
    settings: Settings | None = None,
) -> list[dict]:
    settings = settings or load_settings()
    states_input = [normalize_jurisdiction(state) for state in states] if states else None
    if states_input is None:
        if settings.legal_source_map_path.exists():
            try:
                source_rows = json.loads(settings.legal_source_map_path.read_text(encoding="utf-8"))
                states_input = sorted(
                    {
                        normalize_jurisdiction(str(row.get("jurisdiction")))
                        for row in source_rows
                        if row.get("jurisdiction")
                    }
                )
            except Exception:
                states_input = None
    with db.get_connection(settings.db_path) as conn:
        if states_input is None:
            states_input = [row["jurisdiction"] for row in db.list_law_jurisdictions(conn)]
    rows = []
    for state in states_input:
        answer = answer_electronic_proxy_questions(
            jurisdiction=state,
            community_type=community_type,
            entity_form=entity_form,
            settings=settings,
        )
        rows.append(
            {
                "jurisdiction": answer.jurisdiction,
                "community_type": answer.community_type,
                "entity_form": answer.entity_form,
                "electronic_assignment_status": answer.electronic_assignment.status,
                "electronic_signature_status": answer.electronic_signature.status,
                "confidence": answer.confidence,
                "last_verified_date": answer.last_verified_date,
                "known_unknowns": answer.known_unknowns,
            }
        )
    rows.sort(key=lambda row: row["jurisdiction"])
    return rows

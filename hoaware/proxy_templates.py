"""Proxy form template engine.

Queries legal_rules for a given jurisdiction + community_type, then renders
a legally-compliant proxy authorization form as HTML.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from hoaware import db
from hoaware.config import load_settings

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)

# Community type display names
_COMMUNITY_TYPE_DISPLAY = {
    "hoa": "Homeowners Association",
    "condo": "Condominium Association",
    "coop": "Cooperative",
    "poa": "Property Owners Association",
    "planned_community": "Planned Community",
}

# Jurisdictions that require directed voting instructions on a separate page,
# detached from the proxy authorization form (e.g. California Corp. Code § 7613(b)).
_SEPARATE_INSTRUCTIONS_PAGE_JURISDICTIONS = {"CA"}


def _get_rule_value(rules: list[dict], rule_type: str) -> str | None:
    """Find a rule by rule_type and return its value_text, or None."""
    for r in rules:
        if r.get("rule_type") == rule_type:
            return r.get("value_text")
    return None


def _get_rule_citation(rules: list[dict], rule_type: str) -> str | None:
    """Find the citation for a rule_type."""
    for r in rules:
        if r.get("rule_type") == rule_type:
            return r.get("citation")
    return None


def _compute_expiry(validity_duration: str | None, meeting_date: str | None) -> str | None:
    """Compute proxy expiry date from state rules and meeting date."""
    if not validity_duration:
        return None

    duration_lower = validity_duration.lower()

    # If meeting date is given, use it
    if meeting_date:
        return meeting_date

    # Parse common duration patterns
    today = date.today()
    if "11 month" in duration_lower:
        return (today + timedelta(days=335)).isoformat()
    if "1 year" in duration_lower or "12 month" in duration_lower or "one year" in duration_lower:
        return (today + timedelta(days=365)).isoformat()
    if "180 day" in duration_lower or "6 month" in duration_lower:
        return (today + timedelta(days=180)).isoformat()
    if "90 day" in duration_lower or "3 month" in duration_lower:
        return (today + timedelta(days=90)).isoformat()

    return None


def get_proxy_rules(jurisdiction: str, community_type: str = "hoa") -> list[dict]:
    """Fetch proxy-related legal rules for a jurisdiction."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rules = db.list_legal_rules_for_scope(
            conn,
            jurisdiction=jurisdiction,
            community_type=community_type,
            topic_family="proxy_voting",
        )
    return rules


def _build_template_context(
    jurisdiction: str,
    community_type: str,
    rules: list[dict],
    *,
    grantor_name: str | None,
    grantor_unit: str | None,
    delegate_name: str | None,
    hoa_name: str | None,
    meeting_date: str | None,
    direction: str,
    voting_instructions: list[dict] | None,
    expires_at: str | None,
) -> dict:
    """Build the shared Jinja2 context dict for proxy templates."""
    proxy_allowed = _get_rule_value(rules, "proxy_allowed")
    form_requirement = _get_rule_value(rules, "proxy_form_requirement")
    directed_option = _get_rule_value(rules, "proxy_directed_option")
    validity_duration = _get_rule_value(rules, "proxy_validity_duration")
    electronic_allowed = _get_rule_value(rules, "proxy_electronic_assignment_allowed")
    holder_restrictions = _get_rule_value(rules, "proxy_holder_restrictions")

    statutory_citation = None
    for rt in ["proxy_allowed", "proxy_form_requirement", "proxy_directed_option"]:
        cit = _get_rule_citation(rules, rt)
        if cit:
            statutory_citation = cit
            break

    if not expires_at:
        expires_at = _compute_expiry(validity_duration, meeting_date)

    undirected_fallback = True
    if directed_option and "must be directed" in directed_option.lower():
        undirected_fallback = False

    electronic_assignment_note = None
    if electronic_allowed:
        if "yes" in (electronic_allowed or "").lower() or "permitted" in (electronic_allowed or "").lower():
            electronic_assignment_note = (
                f"Electronic proxy assignment is permitted under {jurisdiction} law. "
                "This form may be signed electronically pursuant to the ESIGN Act "
                "(15 U.S.C. \u00a7 7001) and the Uniform Electronic Transactions Act."
            )

    required_disclosures = []
    if proxy_allowed and "not" in proxy_allowed.lower():
        required_disclosures.append(
            f"WARNING: Proxy voting may not be permitted for {community_type} associations "
            f"in {jurisdiction}. Verify with your governing documents before using this form."
        )

    parsed_instructions = None
    if voting_instructions:
        parsed_instructions = voting_instructions if isinstance(voting_instructions, list) else []

    return dict(
        jurisdiction=jurisdiction.upper(),
        community_type=community_type,
        community_type_display=_COMMUNITY_TYPE_DISPLAY.get(community_type, community_type.title()),
        statutory_citation=statutory_citation,
        grantor_name=grantor_name,
        grantor_unit=grantor_unit,
        delegate_name=delegate_name,
        hoa_name=hoa_name,
        meeting_date=meeting_date,
        direction=direction,
        voting_instructions=parsed_instructions,
        validity_duration=validity_duration,
        expires_at=expires_at,
        holder_restrictions=holder_restrictions,
        form_requirements=form_requirement,
        electronic_assignment_note=electronic_assignment_note,
        required_disclosures=required_disclosures,
        undirected_fallback=undirected_fallback,
        revocation_notes=None,
        generated_date=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def requires_separate_instructions_page(jurisdiction: str) -> bool:
    """Return True if this jurisdiction requires directed voting instructions on a separate page."""
    return jurisdiction.upper() in _SEPARATE_INSTRUCTIONS_PAGE_JURISDICTIONS


def render_proxy_form(
    jurisdiction: str,
    community_type: str = "hoa",
    *,
    grantor_name: str | None = None,
    grantor_unit: str | None = None,
    delegate_name: str | None = None,
    hoa_name: str | None = None,
    meeting_date: str | None = None,
    direction: str = "directed",
    voting_instructions: list[dict] | None = None,
    expires_at: str | None = None,
) -> str:
    """Render the main proxy authorization form as HTML.

    For jurisdictions that require voting instructions on a separate page (e.g. CA),
    directed voting instructions are omitted from this form — call
    render_directed_instructions() separately to get that page.

    Returns rendered HTML string.
    """
    rules = get_proxy_rules(jurisdiction, community_type)
    ctx = _build_template_context(
        jurisdiction, community_type, rules,
        grantor_name=grantor_name, grantor_unit=grantor_unit,
        delegate_name=delegate_name, hoa_name=hoa_name,
        meeting_date=meeting_date, direction=direction,
        voting_instructions=voting_instructions, expires_at=expires_at,
    )

    # For jurisdictions requiring a separate instructions page, suppress inline
    # voting instructions so the form itself doesn't include them.
    if direction == "directed" and requires_separate_instructions_page(jurisdiction):
        ctx = dict(ctx, direction="directed_separate_page")

    template = _jinja_env.get_template("proxy_base.html")
    return template.render(**ctx)


def render_directed_instructions(
    jurisdiction: str,
    community_type: str = "hoa",
    *,
    grantor_name: str | None = None,
    grantor_unit: str | None = None,
    delegate_name: str | None = None,
    hoa_name: str | None = None,
    meeting_date: str | None = None,
    voting_instructions: list[dict] | None = None,
    expires_at: str | None = None,
) -> str:
    """Render the directed proxy voting instructions as a standalone HTML page.

    This is the separate-page voting instruction sheet required by some jurisdictions
    (notably California). It can also be used for any directed proxy to produce a
    clean, printable voting instructions document.

    Returns rendered HTML string.
    """
    rules = get_proxy_rules(jurisdiction, community_type)
    ctx = _build_template_context(
        jurisdiction, community_type, rules,
        grantor_name=grantor_name, grantor_unit=grantor_unit,
        delegate_name=delegate_name, hoa_name=hoa_name,
        meeting_date=meeting_date, direction="directed",
        voting_instructions=voting_instructions, expires_at=expires_at,
    )
    template = _jinja_env.get_template("proxy_directed.html")
    return template.render(**ctx)

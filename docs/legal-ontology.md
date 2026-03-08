# Legal Ontology for HOA/Condo/Co-op Corpus

Last updated: 2026-02-28
Status: Active

## Purpose
Define a stable, machine-readable ontology so legal extraction and QA stay deterministic across jurisdictions.

## Core Dimensions
- `jurisdiction`: two-letter US state code (e.g., `NC`, `CA`) plus optional `DC`.
- `community_type`: `hoa`, `condo`, `coop`.
- `entity_form`: `nonprofit_corp`, `for_profit_corp`, `unincorporated`, `unknown`.
- `topic_family`: `records_access`, `records_sharing_limits`, `proxy_voting`.
- `source_type`: `statute`, `regulation`, `case_law`, `ag_opinion`, `agency_guidance`, `other`.

## Rule Model
Each extracted rule is one atomic legal statement with a canonical `rule_type`.

### Common fields
- `rule_id`: unique, stable identifier.
- `rule_type`: one of canonical types listed below.
- `applies_to`: owner/member, director, association/board, manager, third-party.
- `value_text`: exact legal requirement summary.
- `value_numeric`: optional numeric quantity (days, months, dollars, etc.).
- `value_unit`: `days`, `months`, `years`, `usd`, `none`, or custom string.
- `conditions`: trigger predicates (if requested in writing, if purpose is proper, etc.).
- `exceptions`: explicit carve-outs.
- `effective_date`: effective date if known.
- `last_verified_date`: date the source text checksum was verified.
- `confidence`: `high`, `medium`, `low`.
- `needs_human_review`: `0` or `1`.

### `records_access` rule types
- `records_inspection_right`
- `records_copy_right`
- `records_request_form`
- `records_response_deadline`
- `records_format_requirement`
- `records_fee_limit`
- `records_retention_requirement`
- `records_member_list_access`
- `records_governing_docs_public_recording`
- `records_enforcement_remedy`

### `records_sharing_limits` rule types
- `sharing_privacy_redaction`
- `sharing_attorney_client_exclusion`
- `sharing_personnel_exclusion`
- `sharing_contract_negotiation_exclusion`
- `sharing_member_list_use_restriction`
- `sharing_third_party_distribution_limit`
- `sharing_copyright_or_ip_limit`
- `sharing_court_order_override`

### `proxy_voting` rule types
- `proxy_allowed`
- `proxy_disallowed`
- `proxy_form_requirement`
- `proxy_signature_requirement`
- `proxy_delivery_requirement`
- `proxy_validity_duration`
- `proxy_revocability`
- `proxy_assignment_rule`
- `proxy_directed_option`
- `proxy_undirected_option`
- `proxy_quorum_counting`
- `proxy_ballot_interaction`
- `proxy_record_retention`
- `proxy_inspection_right`
- `proxy_election_notice_interaction`
- `proxy_electronic_assignment_allowed`
- `proxy_electronic_assignment_required_acceptance`
- `proxy_electronic_assignment_prohibited`
- `proxy_electronic_signature_allowed`
- `proxy_electronic_signature_required_acceptance`
- `proxy_electronic_signature_prohibited`

## Source Model
- `source_id`: internal identifier.
- `citation`: official citation text (e.g., `N.C. Gen. Stat. § 47F-3-118`).
- `citation_url`: direct URL to official source.
- `publisher`: legislature/court/agency.
- `checksum_sha256`: text checksum for drift detection.
- `snapshot_path`: local snapshot artifact path.
- `parser_version`: parser identifier.

## Jurisdiction Profile Model
Denormalized profile keyed by:
- `(jurisdiction, community_type, entity_form)`

Profile sections:
- `governing_law_stack`: ordered list of controlling source citations.
- `records_access_summary`
- `records_sharing_limits_summary`
- `proxy_voting_summary`
- `conflict_resolution_notes`
- `known_gaps`
- `last_verified_date`

## Conflict Resolution Policy
Default ordering when multiple rules overlap:
1. Specific community statute over general corporation statute
2. More recent effective provision (if direct conflict and same hierarchy)
3. Explicit carve-out controls general permission
4. If unresolved, mark `needs_human_review=1` and include both citations

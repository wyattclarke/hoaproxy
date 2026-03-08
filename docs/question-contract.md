# Question Contract: Jurisdiction Legal QA

Last updated: 2026-02-28
Status: Active

## Scope
This contract governs two question families:

1. `records_and_sharing`
2. `proxy_voting`

Inputs:
- `jurisdiction` (required; e.g., `NC`)
- `community_type` (required; `hoa` | `condo` | `coop`)
- `entity_form` (optional; defaults to `unknown`)
- `question_family` (required)

## Output Schema
- `answer`: plain-English synthesized answer.
- `checklist`: ordered list of actionable rule bullets.
- `citations`: array of citation objects:
  - `citation`
  - `citation_url`
  - `source_type`
  - `excerpt`
  - `last_verified_date`
- `known_unknowns`: array of unresolved items.
- `confidence`: `high` | `medium` | `low`
- `last_verified_date`: max verified date across cited sources.
- `disclaimer`: fixed string indicating informational, non-legal-advice use.

## Family Template: `records_and_sharing`
The answer must include:
- Who can request records and in what form.
- Which records are generally accessible.
- Deadlines and fees for inspection/copying.
- What categories can be withheld/redacted.
- Whether downstream sharing/reuse is restricted.

Checklist minimum fields:
- `inspection_right`
- `response_timing`
- `copying_costs`
- `withholding_categories`
- `sharing_limits`

## Family Template: `proxy_voting`
The answer must include:
- Whether proxies are allowed and for whom.
- Assignment and form/signature requirements.
- Directed vs. undirected proxy treatment.
- Revocation, expiration, and meeting/quorum usage.
- Whether proxies must be recorded/retained and if members may inspect them.

Checklist minimum fields:
- `proxy_permitted`
- `form_and_delivery`
- `directed_vs_undirected`
- `validity_and_revocation`
- `recordkeeping_and_inspection`

## Required Additional Cross-State Questions
For each jurisdiction profile, the system must produce explicit statuses for:

1. `electronic_proxy_assignment_status`:
   - `required_to_accept`
   - `allowed`
   - `restricted_or_rejectable`
   - `unclear`
2. `electronic_proxy_signature_status`:
   - `required_to_accept`
   - `allowed`
   - `restricted_or_rejectable`
   - `unclear`

Each status must include at least one citation when not `unclear`.

## Gating Rules
- No citation, no rule: do not include uncited legal claims.
- If profile coverage is incomplete, answer with explicit gaps and `confidence=low`.
- If corporate overlay law may change the outcome, include it in `known_unknowns`.

## Required Disclaimer
`This summary is for informational purposes only and is not legal advice. Consult a licensed attorney in the relevant jurisdiction for legal interpretation.`

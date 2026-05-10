# Quality Audit and Cleanup — 2026-05-09

## Trigger
User flagged RI as poor quality (likely SoS-led model). Asked for:
1. Rescrape RI if gains are big.
2. Sample other states; rescrape any with same issue.
3. End state: as-good-as-possible live on the site.

## Constraint (refined)
Quality is measured by **LLM grading of actual document text**, not by chunk
count. HOAs intentionally added with no documents (e.g. DC stubs) are kept.
Only HOAs whose docs are unrelated/junk are deleted.

## Workflow
For each state under review:
1. `scripts/audit/grade_hoa_text_quality.py --state XX --out grades.json`
   — grades every live HOA (with docs) via DeepSeek-v4-flash.
2. `scripts/audit/delete_junk_hoas.py --grades grades.json --apply`
   — bulk-deletes HOAs verdicted "junk" (entire doc set is non-governing).
3. If state is depleted, optionally rescrape with the right playbook
   (`docs/multi-state-ingestion-playbook.md` keyword-Serper, or
   `docs/name-list-first-ingestion-playbook.md` for registry-first).

## Risk-stack (per Explore agent + retros)
- **HIGH risk** (RI-style junk): RI, IL, SD, ND
- **MEDIUM-HIGH**: AR
- **MEDIUM**: NH, ID, MS, KY, UT, WY
- **LOW** (don't grade-full unless sample says otherwise): HI, DC, MT, OK,
  NM, NV, NE, AL, IA, LA, GA, TN, AK, WV, ME, VT
- Large states FL/IN/CT not yet audited (Tier 3, in-progress per playbook).

## Per-state outputs
state_scrapers/{state}/results/audit_2026_05_09/{state}_grades.json
state_scrapers/{state}/results/audit_2026_05_09/{state}_delete_outcome.json

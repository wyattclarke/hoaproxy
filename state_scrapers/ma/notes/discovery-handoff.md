# {STATE} Discovery Handoff

Updated: {YYYY-MM-DD}

Instruction: continue autonomously for {STATE}. Do not stop at checkpoints.
Commit or hand off as needed, then immediately keep scraping. Only send a
final response if blocked, out of budget, or asked for status.

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/{STATE}/`
- Current cleaned count: {N} manifests with documents, {N} PDFs.
- OpenRouter credits: ~`$X / $Y` used, ~`$Z` remaining.
- Active work: {status line — e.g. "running county sweeps; Hamilton next"}

## Source Families Attempted

List each family, the result class, and whether it is still worth running.

| Source family | Queries run | Net manifests | Net PDFs | Yield | Status |
|---|---|---|---|---|---|
| {family name} | {N} | {N} | {N} | high/med/low/zero | active / exhausted / blocked |

## Productive Sources

Detail the highest-yield families here, including query patterns that worked,
URL shapes worth promoting to deterministic mode, and per-family stop rules.

## Dry Sources

Brief note on each family that returned zero or near-zero useful leads, so the
next session does not repeat them.

## Stop Reasons by Branch

For each stopped branch, record the exact stop trigger (two-sweep rule, budget
exhausted, blocked auth, etc.) and the last sweep's metrics.

## Next Branches

Ordered list of branches to try next, with justification. If active discovery
is stopped under the two-sweep rule, list only allowed follow-ups (dedup audit,
unknown-county repair, name repair, re-mining existing result sets).

## Useful Commands

```bash
# Count bank manifests and PDFs
gsutil ls 'gs://hoaproxy-bank/v1/{STATE}/**/manifest.json' 2>/dev/null | wc -l
gsutil ls 'gs://hoaproxy-bank/v1/{STATE}/*/*/doc-*/original.pdf' 2>/dev/null | wc -l

# OpenRouter credit check
set -a; source settings.env; set +a
curl -s https://openrouter.ai/api/v1/credits \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | python3 -m json.tool
```

## Autonomy Reminder

The turn boundary is not a blocker. If no real blocker exists, keep launching
the next concrete scrape/probe/validation step. Do not send a final answer
just to summarize progress.

"""Append IL-specific noise exclusions to Wave B query files.

Exclusions derived from:
- Brief's generic legal/blog/news patterns
- IL first-pass discovery ledger mining (benchmark/results/il_serper_docpages_*)

Bad hosts confirmed in first-pass leads: illinoiscourts.gov (court opinions),
idfpr.illinois.gov (regulatory pamphlets — produced "Illinois Compiled Statutes
HOA"-style live entries), dicklerlaw.com / oflaherty-law.com / robbinsdimonte.com
/ sfbbg.com (legal blogs), broadshouldersmgt.com (Chicago municipal code),
secure.associationvoice.com (paywall portal).

Idempotent — re-running on an already-tightened file is a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

WAVE_B_COUNTIES = [
    "winnebago", "sangamon", "champaign", "peoria", "mclean",
    "st-clair", "madison", "rock-island", "tazewell", "kankakee",
]

GENERIC_INURL = " ".join([
    "-inurl:case", "-inurl:caselaw", "-inurl:opinion",
    "-inurl:news", "-inurl:articles", "-inurl:press",
    "-inurl:blog", "-inurl:learn-about-law",
])

GENERIC_SITE = " ".join([
    "-site:caselaw.findlaw.com",
    "-site:casetext.com",
    "-site:law.justia.com",
    "-site:scholar.google.com",
])

IL_SPECIFIC_SITE = " ".join([
    "-site:illinoiscourts.gov",
    "-site:idfpr.illinois.gov",
    "-site:dicklerlaw.com",
    "-site:oflaherty-law.com",
    "-site:robbinsdimonte.com",
    "-site:sfbbg.com",
    "-site:broadshouldersmgt.com",
    "-site:associationvoice.com",
])

EXCLUSIONS = " ".join([GENERIC_INURL, GENERIC_SITE, IL_SPECIFIC_SITE])
SENTINEL = "# tightened-wave-b-v1"


def tighten_file(path: Path) -> tuple[bool, int]:
    """Return (changed, line_count). No-op if SENTINEL already present."""
    text = path.read_text()
    if SENTINEL in text:
        return False, len(text.splitlines())
    out_lines = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s or s.startswith("#"):
            out_lines.append(s)
            continue
        # Append exclusions to every active query line.
        if "-site:caselaw" in s:
            # Already has some exclusion suffix — leave as-is.
            out_lines.append(s)
        else:
            out_lines.append(f"{s} {EXCLUSIONS}")
    out_lines.append(f"{SENTINEL}")
    path.write_text("\n".join(out_lines) + "\n")
    return True, len(out_lines)


def main() -> int:
    queries_dir = Path(__file__).resolve().parent.parent / "queries"
    changed = 0
    skipped = 0
    for c in WAVE_B_COUNTIES:
        path = queries_dir / f"il_{c}_serper_queries.txt"
        if not path.exists():
            print(f"  MISSING: {path}", file=sys.stderr)
            continue
        was_changed, n = tighten_file(path)
        status = "tightened" if was_changed else "already tightened"
        print(f"  {status:18s} {path.name} ({n} lines)")
        if was_changed:
            changed += 1
        else:
            skipped += 1
    print(f"\nchanged: {changed}  skipped (already tightened): {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

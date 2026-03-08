#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware.config import load_settings
from hoaware.law import electronic_proxy_summary


def _states_from_source_map(path: Path) -> list[str]:
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    states = sorted({str(row.get("jurisdiction") or "").upper() for row in rows if row.get("jurisdiction")})
    return [state for state in states if len(state) == 2 and state.isalpha()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build per-state answers for electronic proxy assignment/signature questions.")
    parser.add_argument("--community-type", default="hoa", help="hoa|condo|coop")
    parser.add_argument("--entity-form", default="unknown")
    parser.add_argument("--out", type=Path, default=Path("data/legal/electronic_proxy_summary.json"))
    args = parser.parse_args()

    settings = load_settings()
    states = _states_from_source_map(settings.legal_source_map_path)
    rows = electronic_proxy_summary(
        community_type=args.community_type,
        entity_form=args.entity_form,
        states=states,
        settings=settings,
    )
    payload = {
        "community_type": args.community_type,
        "entity_form": args.entity_form,
        "state_count": len(rows),
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out} with {len(rows)} states")


if __name__ == "__main__":
    main()


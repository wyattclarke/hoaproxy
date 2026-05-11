#!/usr/bin/env python3
"""LLM-grade a sample of HOA *entity names* (not content) to detect
non-HOA entries that slipped in via overly-broad county-GIS or
subdivision-list sources.

For each name, the LLM decides:
  - "hoa"        : a real residential community with mandatory membership
                   in a democratic neighborhood association, including
                   condominium associations and residential housing
                   cooperatives.
  - "subdivision": likely just a recorded land subdivision / plat with
                   no mandatory association (or unknown if has one).
  - "other"      : not residential at all (commercial condos, business
                   parks, utility districts, master-planned districts
                   without HOAs, civic organizations, etc.).
  - "uncertain"  : name is genuinely ambiguous.
"""
from __future__ import annotations

import argparse, json, os, random, re, time
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / "settings.env")

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = (
    "You are auditing entity names on a public HOA / condo association "
    "directory. The site only represents real residential communities with "
    "mandatory membership in a democratic neighborhood association — i.e. "
    "homeowners associations (HOAs), property owners associations (POAs), "
    "condominium associations, and residential housing cooperatives. "
    "It explicitly does NOT want: commercial condominiums or office "
    "condos; business parks; recorded land subdivisions or plats with no "
    "mandatory association; utility/water/special districts; civic groups; "
    "metropolitan districts; community development districts (FL CDDs); "
    "PUDs without an active HOA; chambers of commerce; etc."
)

INSTR = (
    "For each name below, classify it as one of:\n"
    "  hoa         — real residential HOA/POA/condo association/coop with mandatory membership\n"
    "  subdivision — likely just a recorded subdivision/plat (no mandatory association apparent)\n"
    "  other       — not residential at all (commercial, utility, civic, etc.)\n"
    "  uncertain   — name is genuinely ambiguous\n"
    "\n"
    "Respond with STRICT JSON: {\"results\": [{\"name\": \"...\", \"verdict\": \"hoa\", \"reason\": \"one short clause\"}, ...]}\n"
    "Use the name only — don't speculate beyond it. If the name carries an explicit residential-association suffix "
    "(Homeowners Association, Property Owners Association, Condominium Association, Owners Corp, Tenants Corp, "
    "Cooperative, etc.) → hoa. Bare subdivision names like 'Pinebrook Meadows' that lack any association suffix → "
    "subdivision. Names that suggest commercial, utility, or other non-residential entities → other."
)


def call_llm(names: list[str], model: str = "deepseek/deepseek-v4-flash") -> list[dict]:
    api_key = os.environ["OPENROUTER_API_KEY"]
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM + "\n\n" + INSTR},
            {"role": "user", "content": "Names to classify:\n" + "\n".join(f"- {n}" for n in names)},
        ],
        "temperature": 0,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    for attempt in range(4):
        try:
            r = requests.post(OPENROUTER, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://hoaproxy.org",
                "X-Title": "hoaproxy entity-name audit",
            }, json=body, timeout=120)
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
                return json.loads(content).get("results", [])
        except Exception as e:
            pass
        time.sleep(3 + attempt * 3)
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--source-filter", default=None,
                    help="Only sample rows whose hoa_locations.source matches this string")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    random.seed(args.seed)

    # Pull rows from the live API. /hoas/summary returns metadata but not
    # source — fetch with the admin /admin/list-corruption-targets if we
    # need to filter by source, otherwise sample from /hoas/summary.
    state = args.state.upper()
    if args.source_filter:
        # Use admin endpoint to get rows by source string
        api_key = os.environ.get("RENDER_API_KEY")
        sid = os.environ.get("RENDER_SERVICE_ID")
        r = requests.get(
            f"https://api.render.com/v1/services/{sid}/env-vars",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
        )
        token = next(
            e["value"] for env in r.json()
            for e in [env.get("envVar", env)]
            if e.get("key") == "JWT_SECRET" and e.get("value")
        )
        r = requests.post("https://hoaproxy.org/admin/list-corruption-targets",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"sources": [args.source_filter]}, timeout=120)
        rows = r.json() if isinstance(r.json(), list) else r.json().get("rows", [])
        rows = [r for r in rows if (r.get("state") or "").upper() == state]
    else:
        # Get the state's full list via /hoas/summary
        rows = []
        offset = 0
        while True:
            r = requests.get(
                f"https://hoaproxy.org/hoas/summary?state={state}&limit=500&offset={offset}",
                timeout=30,
            )
            if r.status_code != 200:
                break
            chunk = r.json().get("results", [])
            if not chunk:
                break
            rows.extend(chunk)
            offset += len(chunk)
            if offset > 30000:
                break

    if not rows:
        print(f"{state}: no rows found")
        return 1
    print(f"{state}: {len(rows)} rows total (source_filter={args.source_filter})")

    sample = random.sample(rows, min(args.sample, len(rows)))
    names = [r.get("hoa") or r.get("name") or "" for r in sample]

    results = call_llm(names)
    if not results:
        print("LLM returned no results")
        return 2

    counts = {"hoa": 0, "subdivision": 0, "other": 0, "uncertain": 0}
    for r in results:
        v = (r.get("verdict") or "").lower()
        counts[v] = counts.get(v, 0) + 1

    print(f"\n=== {state} sample (n={len(sample)}, source={args.source_filter or 'any'}) ===")
    print(f"verdicts: {counts}")
    print()
    for r in results:
        print(f"  {r.get('verdict'):<12}  {(r.get('name') or '')[:55]:<55}  | {(r.get('reason') or '')[:60]}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "state": state, "sample_size": len(sample), "counts": counts,
            "source_filter": args.source_filter, "results": results,
        }, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

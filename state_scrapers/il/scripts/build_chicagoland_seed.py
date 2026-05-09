#!/usr/bin/env python3
"""Build Chicagoland (Cook + collar) condo/HOA seed JSONL via mgmt-co harvesting.

Cook County has no public, queryable named-condo registry (the Assessor's
3r7i-mrz4 dataset is PIN-only with no association names). Per the
name-list-first playbook §2d fallback, we harvest mgmt-co press release /
portfolio pages where named associations appear in URL slugs and page titles.

Outputs state_scrapers/il/leads/il_chicagoland_seed.jsonl, one line per
canonical entity, schema matching DC reference (state_scrapers/dc/leads/
dc_cama_condo_seed.jsonl).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

SERPER_ENDPOINT = "https://google.serper.dev/search"

# Mgmt-co target source families. Each entry: (label, queries, name_extractors)
# A name_extractor takes (title, url, snippet) and returns a candidate name or None.
MGMT_COS = [
    {
        "label": "fsresidential",
        "queries": [
            'site:fsresidential.com/illinois/news-events/press-releases "Condominium Association"',
            'site:fsresidential.com/illinois/news-events/press-releases "Homeowners Association"',
            'site:fsresidential.com/illinois/news-events/press-releases "Master Association"',
            'site:fsresidential.com/illinois/news-events/press-releases "selected to manage"',
            'site:fsresidential.com/illinois/news-events/press-releases "to manage"',
            'site:fsresidential.com/illinois/news-events/press-releases',
        ],
    },
    {
        "label": "fosterpremier",
        "queries": [
            'site:fosterpremier.com "Condominium Association"',
            'site:fosterpremier.com "Homeowners Association"',
            'site:fosterpremier.com "Master Association"',
            'site:fosterpremier.com "Townhome Association"',
            'site:fosterpremier.com Chicago condominium',
        ],
    },
    {
        "label": "sudler",
        "queries": [
            'site:sudlerchicago.com "Condominium"',
            'site:sudler.com Chicago "Condominium Association"',
            '"Sudler Property Management" Chicago condominium',
            '"managed by Sudler" Chicago condominium',
        ],
    },
    {
        "label": "associa-chicagoland",
        "queries": [
            'site:associaonline.com Chicagoland "Condominium Association"',
            'site:associaonline.com Illinois "Homeowners Association"',
            '"Associa Chicagoland" "Condominium Association" property',
            '"Associa Chicagoland" "Homeowners Association"',
        ],
    },
    {
        "label": "lieberman",
        "queries": [
            'site:liebermanmgmt.com "Condominium Association"',
            'site:liebermanmgmt.com "Association"',
            '"Lieberman Management" Chicago condominium',
        ],
    },
    {
        "label": "vanguard",
        "queries": [
            'site:vanguardcm.com "Association"',
            'site:vanguardcommunitymanagement.com "Association"',
            '"Vanguard Community Management" Illinois',
        ],
    },
    {
        "label": "habitat",
        "queries": [
            'site:habitat.com Chicago "Condominium Association"',
            '"Habitat Company" Chicago "Condominium Association"',
        ],
    },
    {
        "label": "acm",
        "queries": [
            'site:acmweb.com "Association"',
            'site:acmcommunities.com "Association"',
            '"ACM Community Management" Chicago condominium',
        ],
    },
    {
        "label": "inland",
        "queries": [
            'site:inlandresidential.com Illinois condominium',
            '"Inland Residential" Chicago "Condominium Association"',
        ],
    },
    {
        "label": "klein",
        "queries": [
            'site:klein-management.com Chicago condominium',
            '"Klein Management" Chicago "Condominium Association"',
        ],
    },
    {
        "label": "wolinlevin",
        "queries": [
            'site:wolinlevin.com "Condominium"',
            '"Wolin-Levin" Chicago "Condominium Association"',
        ],
    },
    {
        "label": "hhsg",
        "queries": [
            'site:hhsg.com Chicago condominium',
            '"Heil Heil Smart Golee" Chicago condominium',
        ],
    },
    {
        "label": "draperkramer",
        "queries": [
            'site:draperandkramer.com Chicago "Condominium Association"',
            '"Draper and Kramer" Chicago "Condominium Association"',
        ],
    },
    # Aggregator/directory style
    {
        "label": "hoa-usa-il",
        "queries": [
            'site:hoa-usa.com/illinois "Condominium Association"',
            'site:hoa-usa.com/illinois "Homeowners Association"',
        ],
    },
    {
        "label": "managementcompanies-il",
        "queries": [
            'site:hoamanagementcompanies.net Illinois Chicago "Condominium"',
            'site:hoamanagementcompanies.net Illinois "Homeowners Association"',
        ],
    },
]

# Junk-host blocklist for source URLs we definitely don't want to seed from.
JUNK_HOST_RE = re.compile(
    r"(facebook|linkedin|twitter|instagram|youtube|reddit|"
    r"yelp\.com|bbb\.org|glassdoor|indeed|zoominfo|crunchbase|"
    r"google\.com/maps|google\.com/search|tripadvisor)",
    re.IGNORECASE,
)

# Recognized canonical-entity name suffixes. Required for a candidate to be kept.
ENTITY_SUFFIX_RE = re.compile(
    r"\b("
    r"condominium\s+association(?:,?\s+inc\.?)?|"
    r"condominium\s+(?:no\.?|number)\s*\d+\s+association(?:,?\s+inc\.?)?|"
    r"condominium(?:s)?(?:,?\s+inc\.?)?|"
    r"homeowners?\s+association(?:,?\s+inc\.?)?|"
    r"home\s+owners?\s+association(?:,?\s+inc\.?)?|"
    r"home\s+owners?\s+assoc\.?|"
    r"property\s+owners?\s+association(?:,?\s+inc\.?)?|"
    r"townhome(?:s)?\s+association(?:,?\s+inc\.?)?|"
    r"townhouse(?:s)?\s+association(?:,?\s+inc\.?)?|"
    r"unit\s+owners?\s+association(?:,?\s+inc\.?)?|"
    r"community\s+association(?:,?\s+inc\.?)?|"
    r"master\s+association(?:,?\s+inc\.?)?|"
    r"cooperative(?:,?\s+inc\.?)?|"
    r"co-?op\s+association|"
    r"owners?\s+association(?:,?\s+inc\.?)?"
    r")\b",
    re.IGNORECASE,
)

# Strip trailing common phrases that may follow the name in titles/snippets.
TITLE_TAIL_RE = re.compile(
    r"\s*[\|\-–—:]\s*(FirstService\s+Residential|Foster\s+Premier|Sudler|"
    r"Associa|Lieberman|Vanguard|Habitat|ACM|Inland|Klein|Wolin-?Levin|Heil|Draper|"
    r"HOA-USA|Press\s+Release|News).*$",
    re.IGNORECASE,
)

# Generic-token reject: candidate name is too generic. The pattern is
# (geo-descriptor|generic-modifier)+ + entity-suffix.
GENERIC_GEO_PREFIX_RE = re.compile(
    r"^("
    r"chicago(?:land|[-\s]area)?|illinois|il|cook|cook\s+county|"
    r"gold\s+coast|loop|west\s+loop|south\s+loop|north\s+side|"
    r"lincoln\s+park|lakeview|streeterville|downtown|midwest|"
    r"a|an|the|your|our|are|is|to|from|with|by|of|that|this|about|"
    r"master[-\s]planned|master|sub|new|expanded|expanding|expands?|"
    r"premier|select|home\s+type"
    r")\s+("
    r"condominium(?:\s+association)?|"
    r"homeowners?\s+association|"
    r"home\s+owners?\s+association|"
    r"community\s+association|"
    r"property\s+owners?\s+association|"
    r"townhome(?:s)?\s+association|"
    r"townhouse(?:s)?\s+association|"
    r"unit\s+owners?\s+association|"
    r"master\s+association|"
    r"owners?\s+association|"
    r"condo\s+association"
    r")$",
    re.IGNORECASE,
)

# Pure boilerplate "name minus suffix" reject (e.g. "the association").
GENERIC_REJECT_RE = re.compile(
    r"^("
    r"condominium|condominium\s+association|condo\s+association|"
    r"homeowners?\s+association|community\s+association|"
    r"master\s+association|chicago\s+condominium|illinois\s+condominium|"
    r"the\s+association|our\s+association|your\s+association|"
    r"-?\s*select\s*-?"
    r")$",
    re.IGNORECASE,
)

# Mgmt-co names that should NEVER appear inside an entity name (they leak in
# from press releases like "managed by Foster Premier" being misextracted).
MGMT_CO_LEAK_RE = re.compile(
    r"\b("
    r"firstservice|foster\s+premier|sudler|associa|lieberman|vanguard|"
    r"habitat\s+company|acm\s+community|inland\s+residential|klein|"
    r"wolin[-\s]?levin|heil|draper\s+and\s+kramer|hoa[-\s]?usa|"
    r"property\s+specialists|realmanage|commonwealth\s+edison"
    r")\b",
    re.IGNORECASE,
)

# Junk leading articles / function words that indicate a body-text fragment.
LEADING_GENERIC_RE = re.compile(
    r"^(a|an|the|your|our|are|is|to|from|with|by|of|that|this|"
    r"about|for|expands?|select|home\s+type)\s+",
    re.IGNORECASE,
)


def now_id() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def serper_search(query: str, *, num: int = 10, page: int = 1) -> dict[str, Any]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        raise RuntimeError("SERPER_API_KEY not set")
    body = {"q": query, "num": num, "page": page, "gl": "us"}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    r = requests.post(SERPER_ENDPOINT, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


# Stopword phrases that indicate the candidate is body text / case law / boilerplate.
BOILERPLATE_RE = re.compile(
    r"\b("
    r"consists\s+of|include[ds]?:|to\s+sum\s+it\s+up|for\s+the\s+proposition|"
    r"recently\s+added|portfolio\s+includes?|expands?\s+chicago|"
    r"awarded\s+management|assumed\s+property|partnering\s+with|"
    r"exiting\s+condominium|conversion\s+market|division\s+was\s+sold|"
    r"merged\s+with|simultaneously|citations\s+issued|"
    r"applicant:?|respondent:?|petitioner:?|plaintiff:?|defendant:?|"
    r"v\.\s|vs\.\s|illinois\s+appellate|app\.\s*3d|n\.e\.\s*2d|"
    r"property\s+services|community\s+management,?\s+llc|cities\s+management"
    r")\b",
    re.IGNORECASE,
)

# After-suffix tokens we should also strip (e.g., ", Inc." or trailing comma fragments)
SUFFIX_TAIL_STRIP_RE = re.compile(r"[,;:].*$")

# Capitalized-token regex (proper-noun walking backwards from suffix).
PROPER_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Za-z0-9'&\-/\.]*$")

# Tokens we allow IN BETWEEN proper nouns even though they are lowercase.
NAME_GLUE_TOKENS = {
    "of", "the", "at", "on", "and", "&", "de", "la", "del", "von",
    "no.", "no", "in", "by", "for",
}


def _walk_back_to_name_start(words: list[str], suffix_start_idx: int) -> int:
    """Given a list of tokens and the index of the first suffix token, walk
    backward through capitalized/glue tokens to find the start of the entity
    name. Returns the start index (inclusive)."""
    i = suffix_start_idx - 1
    last_proper = i
    seen_proper = False
    while i >= 0:
        tok = words[i]
        bare = tok.strip(",.:;()[]")
        if not bare:
            i -= 1
            continue
        if PROPER_TOKEN_RE.match(bare):
            last_proper = i
            seen_proper = True
            i -= 1
            continue
        # Numeric-only tokens (street numbers) are part of names: "5400 North..."
        if bare.replace("-", "").isdigit():
            last_proper = i
            seen_proper = True
            i -= 1
            continue
        if bare.lower() in NAME_GLUE_TOKENS and seen_proper:
            i -= 1
            continue
        break
    return last_proper


def normalize_candidate(raw: str) -> str | None:
    """Take a raw title/snippet/URL slug fragment, return a clean canonical name or None."""
    if not raw:
        return None
    # strip URL encoding
    s = unquote(raw)
    # normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # strip a tail like " | FirstService Residential"
    s = TITLE_TAIL_RE.sub("", s)
    # strip wrapping quotes
    s = s.strip('\'"`“”‘’')
    s = re.sub(r"^\s*\d+\s+(of|in)\s+\d+\s*[–\-:]\s*", "", s)  # "1 of 84 - "
    s = s.strip()

    # Reject if obvious boilerplate / case-law / directory listing
    if BOILERPLATE_RE.search(s):
        return None
    # Reject if it contains "..." (ellipsis — almost always means body fragment)
    if "..." in s or "…" in s:
        return None
    # Reject if it contains mgmt-co names (these leaked into body text)
    if MGMT_CO_LEAK_RE.search(s):
        # Allow only if the mgmt-co name appears AFTER the entity suffix
        # (which we'll trim anyway). For simplicity, just reject.
        # Exception: "Sudler Property Management" alone is a mgmt co, but
        # "Sudler Building Condominium Association" wouldn't trigger this.
        # We'll let the post-trim cleanup handle that.
        pass  # don't reject yet; we'll re-check after trimming

    # Find the LAST suffix match (so e.g. "Foo Condominium Association of Bar
    # Condominium" picks the second). But if multiple matches, prefer the one
    # with the most preceding proper-noun context.
    matches = list(ENTITY_SUFFIX_RE.finditer(s))
    if not matches:
        return None
    # Try each match (preferring the latest with non-trivial context)
    best: str | None = None
    for m in reversed(matches):
        # Trim everything after this suffix
        head_plus_suffix = s[: m.end()]
        head_plus_suffix = SUFFIX_TAIL_STRIP_RE.sub("", head_plus_suffix)
        # Tokenize and find the suffix-start token index
        words = head_plus_suffix.split()
        # Find first token that's part of the suffix match.
        # A simpler heuristic: count words in the suffix text (m.group(1)).
        suffix_words = len(m.group(1).split())
        suffix_start_idx = len(words) - suffix_words
        if suffix_start_idx <= 0:
            continue
        name_start = _walk_back_to_name_start(words, suffix_start_idx)
        # Need at least 1 proper-noun token before suffix
        if name_start >= suffix_start_idx:
            continue
        cand_words = words[name_start:]
        cand = " ".join(cand_words).strip(" ,.;:")
        # Trim trailing comma-fragments from the suffix text
        cand = SUFFIX_TAIL_STRIP_RE.sub("", cand).strip()
        # Reject if cand is implausibly short or long
        alnum = re.sub(r"[^A-Za-z0-9]", "", cand)
        if len(alnum) < 5 or len(cand) > 100:
            continue
        # Reject if too many words (likely body fragment)
        if len(cand.split()) > 12:
            continue
        # Reject if cand starts with a glue word
        if cand.split()[0].lower() in NAME_GLUE_TOKENS:
            continue
        # Reject generic (single suffix-ish word)
        if GENERIC_REJECT_RE.match(cand):
            continue
        # Reject geo-only-prefix names ("Chicago Condominium", "Illinois HOA", ...)
        if GENERIC_GEO_PREFIX_RE.match(cand):
            continue
        # Reject if mgmt-co name appears inside
        if MGMT_CO_LEAK_RE.search(cand):
            continue
        # Reject if first token is not capitalized AND not a digit
        first_tok = cand.split()[0]
        if not (first_tok[0].isupper() or first_tok[0].isdigit()):
            continue
        # Reject if cand contains pipe, bullet, or backslash (table/list garbage)
        if any(ch in cand for ch in "|·•\\"):
            continue
        # Reject very-short two-word names where neither word is a proper noun
        # (e.g., "98 unit"). Require at least one alpha-only word OR a digit prefix.
        toks = cand.split()
        non_suffix = []
        for t in toks:
            if ENTITY_SUFFIX_RE.search(t):
                break
            non_suffix.append(t)
        if not non_suffix:
            continue
        # Sentence-case if ALL CAPS
        if cand == cand.upper() and any(c.isalpha() for c in cand):
            cand = cand.title()
            cand = re.sub(r"\bIi\b", "II", cand)
            cand = re.sub(r"\bIii\b", "III", cand)
            cand = re.sub(r"\bIv\b", "IV", cand)
        best = cand
        break
    return best


def extract_candidates_from_serp(result: dict[str, Any]) -> list[tuple[str, str]]:
    """Walk a Serper response, yield (candidate_name, source_url) tuples."""
    out: list[tuple[str, str]] = []
    for r in result.get("organic", []) or []:
        url = r.get("link") or ""
        if not url or JUNK_HOST_RE.search(url):
            continue
        # Try title first, then snippet
        for raw in (r.get("title") or "", r.get("snippet") or ""):
            cand = normalize_candidate(raw)
            if cand:
                out.append((cand, url))
                break
        else:
            # Try URL slug as last resort: e.g.
            # /firstservice-residential-selected-to-manage-pebblewood-condominium-no-1-association/
            slug = urlparse(url).path.rstrip("/").split("/")[-1]
            slug = slug.replace("-", " ").replace("_", " ")
            slug = re.sub(r"\b(firstservice residential|foster premier|sudler|associa)\b", "", slug, flags=re.IGNORECASE)
            slug = re.sub(r"\b(selected to manage|to manage|manages|manages new|press release)\b", "", slug, flags=re.IGNORECASE)
            cand = normalize_candidate(slug)
            if cand:
                out.append((cand, url))
    return out


def slug_for_dedup(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.lower()).strip("-")
    return s


def harvest(
    *,
    serper_budget_usd: float,
    serper_cost_per_call_usd: float,
    output_path: Path,
    delay: float = 0.2,
    pages_per_query: int = 3,
    apply: bool,
) -> dict[str, Any]:
    seen: dict[str, dict[str, Any]] = {}
    spend = 0.0
    calls = 0
    log: list[dict[str, Any]] = []
    for src in MGMT_COS:
        if spend >= serper_budget_usd:
            print(f"[budget] hit Serper budget cap ${serper_budget_usd}; stopping at {src['label']}")
            break
        for q in src["queries"]:
            if spend >= serper_budget_usd:
                break
            for page in range(1, pages_per_query + 1):
                if spend >= serper_budget_usd:
                    break
                try:
                    res = serper_search(q, num=10, page=page)
                except requests.HTTPError as e:
                    print(f"[serper] error on '{q}' page {page}: {e}")
                    log.append({"query": q, "page": page, "error": str(e)})
                    break
                calls += 1
                spend += serper_cost_per_call_usd
                cands = extract_candidates_from_serp(res)
                added = 0
                for name, url in cands:
                    sl = slug_for_dedup(name)
                    if not sl or sl in seen:
                        continue
                    seen[sl] = {
                        "name": name,
                        "state": "IL",
                        "county": None,  # filled later by name-binding step
                        "metadata_type": "condo" if re.search(r"condominium|condo", name, re.I) else "hoa",
                        "address": {"state": "IL"},
                        "source": f"il-mgmt-co-{src['label']}",
                        "source_url": url,
                        "discovery_pattern": "name-list-first-mgmt-harvest",
                    }
                    added += 1
                log.append({
                    "src": src["label"], "query": q, "page": page,
                    "calls_total": calls, "spend_usd": round(spend, 4),
                    "candidates_seen": len(cands), "new_added": added,
                })
                # Stop paginating if a page returned nothing
                if not res.get("organic"):
                    break
                time.sleep(delay)
    summary = {
        "ts": now_id(),
        "calls": calls,
        "approx_spend_usd": round(spend, 4),
        "entities": len(seen),
        "by_source": {},
    }
    for ent in seen.values():
        s = ent["source"]
        summary["by_source"][s] = summary["by_source"].get(s, 0) + 1

    if apply:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            for ent in seen.values():
                f.write(json.dumps(ent, sort_keys=True) + "\n")
    log_path = output_path.with_suffix(".harvest_log.jsonl")
    if apply:
        with log_path.open("w", encoding="utf-8") as f:
            for row in log:
                f.write(json.dumps(row, sort_keys=True) + "\n")
    return summary


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", default=str(ROOT / "state_scrapers/il/leads/il_chicagoland_seed.jsonl"))
    p.add_argument("--serper-budget-usd", type=float, default=1.0)
    p.add_argument("--serper-cost-per-call-usd", type=float, default=0.00167)
    p.add_argument("--pages-per-query", type=int, default=3)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    summary = harvest(
        serper_budget_usd=args.serper_budget_usd,
        serper_cost_per_call_usd=args.serper_cost_per_call_usd,
        output_path=Path(args.output),
        pages_per_query=args.pages_per_query,
        apply=args.apply,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

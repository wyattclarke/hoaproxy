"""Phase 10 cleanup for IL downstate live HOAs.

Adapted from state_scrapers/ga/scripts/dedup_and_clean_ga.py with three
differences for the IL Tier-3 downstate session:

1. **Scope filter.** Only operates on live IL HOAs whose bank prefix maps
   to a downstate county OR to one of the recovery slots (`_unknown-county/`,
   `unresolved-name/`) OR to a cross-state leak prefix (`troup/`, `st-johns/`).
   Chicagoland (cook, dupage, lake, will, kane, mchenry, kendall) is the
   parallel session's territory and is excluded.

2. **Hard-delete null residuals.** When the LLM rename pass returns
   `canonical_name=null`, the entry is not actually an HOA — bank-stage
   misclassification. Per playbook Phase 10 lines 756-778: hard-delete
   via `/admin/delete-hoa`, never tag.

3. **Cross-state-leak hard-delete.** HOAs banked under `v1/IL/troup/` or
   `v1/IL/st-johns/` are GA / FL HOAs that leaked into IL discovery. Hard-
   delete from IL; the docs already live in the correct-state bank.

4. **Doc-filename audit.** Per playbook lines 780-814: flag survivors whose
   docs reference a different HOA, whose source URLs are utility/news/gov,
   or whose pre-run doc accumulation marks them as junk-sinks.

Stages (run all by default; gate with --skip-* flags):
  1. heuristic_delete  — obvious-junk regex deletes (no LLM cost)
  2. llm_rename        — unconditional --no-dirty-filter LLM rename
  3. null_delete       — hard-delete LLM-null canonicals
  4. filename_audit    — hard-delete entries whose docs are foreign/junk
  5. dedup             — suffix-stripped signature dedup + merge

Usage:
  .venv/bin/python state_scrapers/il/scripts/dedup_and_clean_il_downstate.py            # dry run
  .venv/bin/python state_scrapers/il/scripts/dedup_and_clean_il_downstate.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

sys.path.insert(0, str(ROOT / "state_scrapers" / "ga" / "scripts"))
from clean_dirty_hoa_names import (  # noqa: E402
    is_dirty,
    _try_strip_prefix,
    _looks_canonical,
    _live_admin_token,
    _fetch_summaries,
    _fetch_doc_text,
    _llm_client,
    DEFAULT_MODEL,
    FALLBACK_MODEL,
)

BASE_URL = "https://hoaproxy.org"
STATE = "IL"

CHICAGOLAND_COUNTIES = {
    "cook", "dupage", "lake", "will", "kane", "mchenry", "kendall",
}
DOWNSTATE_COUNTIES = {
    "winnebago", "sangamon", "champaign", "peoria", "mclean",
    "st-clair", "madison", "rock-island", "tazewell", "kankakee",
    # Wave-C candidates — eligible if currently in live list.
    "dekalb", "lasalle", "boone", "grundy", "macon", "vermilion",
    "adams", "knox", "williamson", "effingham", "coles", "marion",
    "stephenson", "whiteside", "henry", "lee", "ogle", "jackson",
    "franklin",
}
RECOVERY_PREFIXES = {"_unknown-county", "unresolved-name"}
CROSS_STATE_LEAK_PREFIXES = {
    # These are GA / FL counties; manifests under v1/IL/{this}/ are leaks.
    "troup", "st-johns", "duval", "hall", "pinellas",
}


# --- pre-LLM heuristic deletes (obvious junk where the LLM is overkill) ---

# Names that are clearly not HOAs no matter what the document says.
# These match patterns the bank_hoa() recovery logic produced for IL.
_HEURISTIC_DELETE_PATTERNS = [
    # Statute / law titles
    r"^Illinois\s+(Common\s+Interest|Compiled\s+Statutes|Condominium\s+Property|General\s+Not\s+for\s+Profit|General\s+Assembly)",
    r"^Illinois\s+HOA$",
    r"^SENATE\s+JOURNAL",
    r"^HIGHLIGHTS\s+OF\s+THIS\s+ISSUE",
    # Code / ordinance / resolution numbers (gov boilerplate)
    r"^Chapter\s+\d+[:.]\s",
    r"^Chapter\s+\d+\b.*\bHOA$",
    r"^Ordinance\s+(No\.?|Number)\s+\d",
    r"^Resolution\s+(Number|No\.?|Index)",
    r"^RESOLUTION\s+INDEX",
    r"^Letter\s+Reso\b",
    r"^Public\s+Review\s+Draft",
    r"^R\s+Authorizing\s+Purchase\b",
    # Code books / county/village governance (always junk)
    r"^Village\s+of\s+[A-Z]\w+\s+HOA$",
    r"^Town\s+of\s+[A-Z]\w+\s+HOA$",
    r"^City\s+of\s+[A-Z]\w+\s+HOA$",
    r"^[A-Z]\w+\s+County\s+Code\s+Chapter\b",
    r"^Urbana\s+Zoning\s+Ordinance\b",
    # Court / case / agency
    r"^Case\s+\d{2}-\d{4,}",
    r"^Housing\s+Authority\b",
    r"^Board\s+of\s+Trustees\s+Supplement",
    r"^Development\s+Services\s+Department",
    r"^Region\s+\d+\s+Planning\s+Council",
    r"^STATE\s+OF\s+[A-Z]+\s+COUNTY\s+REC\s+FEE",
    r"^Of\s+the\s+\w+\s+County\s+Board\s+of\s+Review",
    # Article / blog fragments
    r"^Breaking\s+Down\b",
    r"^BUYING\s+A\s+CONDOMINIUM",
    r"^An\s+Introduction\s+to\b",
    r"^Free\s+Policies\b",
    r"^Revisions\s+in\s+These\s+Turbulent\b",
    r"^Real\s+Estate\s+Condos\s+and\s+Community\s+Assoc",
    r"^Stacey\s+Alcorn\b",
    r"^How\s+to\s+Prepare\s+Your\b",
    r"^Resume\s+of\s+\w+",
    # Bankruptcy / docket / case prefixes
    r"^\d{2}-\d{4,}-",
    # Form / boilerplate fragments
    r"^ALTA[®]?\s+Commitment\b",
    r"^Association\s+Complaint\s+Form",
    r"^Sample\s+Association\s+Complaint",
    r"^Resolving\s+Complaints?\s*HOA?$",
    r"^Rights\s+and\s+Responsibilities\s+of\s+Association",
    r"^Plan\s+and\s+Agreement\s+of\s+Merger",
    r"^Of\s+the\s+Association\s+of\s+Problem-Solving",
    r"^Developer\s+Turnover",
    r"^Condominiums?:\s+Deconversion",
    r"^Condos\s+and\s+Common\s+Interest",
    r"^EPA\s+Designations\s+Rule",
    r"^HANDBOOK\s+OF\s+(?:&|and)\s+HOA$",
    r"^Quad\s+Cities\s+Section\s+CONSTITUTION",
    # OCR / very-short / single-token junk
    r"^Assoc\s*$",
    r"^State\s*$",
    r"^History\s*$",
    r"^Review\s*$",
    r"^Newsletters?\s*$",
    r"^Resrictions\s*$",
    r"^Rockton\s*$",
    r"^Untitled\s+HOA\s*$",
    r"^Filings\s*$",
    r"^Bloomington\s+HOA$",
    r"^Foundation\s+Ala\s+April$",
    r"^Iaha\s+Revised\s+Fi$",
    r"^Regs\s+Lake\s+Holiday$",
    r"^Board\s+Review\s+Approved",
    r"^0001\s+HOA",
    r"^Pa\{atine",  # OCR garbage
    r"^Sa\.,~",  # OCR garbage
    r"^Llha\s+Protective",
    r"^Oped\s+Home",
    r"^Eric\s+Schachter",  # person name
    r"^Matthew\s+R\.\s+Henderson",  # person name
    r"^Roscoe\s+C\.\s+Stelford",  # person name
    r"^Mark\s+Monge\s+Homeowners\b",  # person name appended HOA
    # Single-statute references
    r"^Section\s+\d+",
    # Out-of-state .gov leak
    r"^Wichita\.gov\b",
    r"^Filings\s+401\s+North\s+Wabash",  # Cook (Wabash Ave Chicago) leaked into other county
    # "filed <X> county" courthouse stamps
    r"^filed\s+\w+\s+county\b",
    # "And for X" / "For X" prefixes are fragments but the LLM can sometimes
    # salvage when the doc names the HOA — leave to stage 2.
]
_HEURISTIC_DELETE_RE = [re.compile(p, re.I) for p in _HEURISTIC_DELETE_PATTERNS]


def is_heuristic_delete(name: str) -> tuple[bool, str]:
    """Return (matches, reason) — names that are obviously not HOAs."""
    n = name or ""
    for i, pat in enumerate(_HEURISTIC_DELETE_RE):
        if pat.search(n):
            return True, _HEURISTIC_DELETE_PATTERNS[i]
    return False, ""


# --- dedup signature ---

SUFFIX_TOKENS = {
    "homeowners", "homeowner", "association", "associations", "assn",
    "hoa", "poa", "property", "owners", "owner", "community",
    "condominium", "condo", "condos", "condominiums",
    "townhome", "townhomes", "townhouse", "townhouses", "villas", "villa",
    "estates", "homes", "home", "houses", "house",
    "neighborhood", "subdivision", "sub", "ph", "phase",
    "inc", "incorporated", "co", "corp", "corporation", "ltd", "llc",
    "the", "at", "of", "and", "a", "in", "on", "by",
    "illinois", "il",
    "s",
}


def signature(name: str) -> str:
    n = (name or "").lower()
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    toks = [t for t in n.split() if t and t not in SUFFIX_TOKENS]
    return " ".join(toks)


# --- LLM prompts ---

PERMISSIVE_SYSTEM = (
    "You normalize HOA / condominium / property-owners-association names. "
    "Given a possibly-garbled current name plus the first chunk of the HOA's "
    "governing-document OCR text, return the canonical legal name of the "
    "association. Prefer the exact phrase that appears in the document body. "
    "If the document names a subdivision but never spells out the legal entity, "
    "you may infer '<Subdivision Name> Homeowners Association' as long as the "
    "subdivision name is unambiguous in the text. Return null only when no "
    "specific subdivision or community is named at all — for example when the "
    "document is a state statute, a generic policy memo, a court filing, an "
    "article about HOAs in general, or a regulatory pamphlet. Strip OCR "
    "fragments, page headers, all-caps shouting, and document titles like "
    "'BY-LAWS OF', 'DECLARATION OF', 'EXHIBIT A', 'AMENDMENT', or "
    "'AMENDED AND RESTATED'."
)


def name_prompt(name: str, text: str) -> str:
    return (
        f"current_name: {name!r}\n\n"
        f"document_excerpt:\n{text or '(none)'}\n\n"
        "Return strict JSON: {\"canonical_name\": <string or null>, "
        "\"confidence\": <0-1>, \"reason\": <short string>}. "
        "Do not include any text outside the JSON object."
    )


DEDUP_SYSTEM = (
    "You decide whether multiple HOA / condominium / property-owners-"
    "association entries on a website are the same legal association. "
    "Two entries are the same when their proper-name root matches and "
    "neither is clearly a different community in a different town. "
    "Use city, document count, chunk count, and the names themselves. "
    "When they're the same, return the most-canonical name (prefer "
    "fully-spelled-out 'Homeowners Association' over 'HOA', prefer "
    "the spelling closest to a legal-entity record, and avoid noisy "
    "all-caps when an equivalent mixed-case version exists)."
)


def dedup_prompt(group: list[dict]) -> str:
    rows = "\n".join(
        f"  id={g['hoa_id']} city={g.get('city') or '-'} docs={g.get('doc_count')} chunks={g.get('chunk_count')} name={g['hoa']!r}"
        for g in group
    )
    return (
        f"Entries with shared name root:\n{rows}\n\n"
        "Return strict JSON:\n"
        "  {\"same_hoa\": <true|false>,\n"
        "   \"canonical_name\": <string or null>,\n"
        "   \"keep_id\": <int or null>,\n"
        "   \"reason\": <short string>}\n\n"
        "If same_hoa is false, keep_id and canonical_name should be null. "
        "Otherwise pick the entry whose name is closest to canonical "
        "as keep_id and set canonical_name to its preferred form. "
        "Return JSON only."
    )


def _llm_json(client: OpenAI, model: str, system: str, user: str) -> dict | None:
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        return {"_error": str(exc)}
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return {"_error": "empty_choices"}
    raw = (choices[0].message.content or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"_error": "invalid_json", "raw": raw[:200]}


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# --- API helpers ---


def _fetch_documents(base_url: str, hoa: str) -> list[dict]:
    """Return list of doc records ({filename, source_url, ingested_at, ...})."""
    try:
        r = requests.get(
            f"{base_url}/hoas/{requests.utils.quote(hoa, safe='')}/documents",
            timeout=60,
        )
        if r.ok:
            return r.json() or []
    except requests.RequestException:
        pass
    return []


def _name_to_prefix_map(import_report_paths: list[Path]) -> dict[str, str]:
    """Map HOA name -> bank prefix, merged across one or more import reports."""
    out: dict[str, str] = {}
    for p in import_report_paths:
        if not p.exists():
            continue
        report = json.loads(p.read_text())
        for resp in report.get("responses") or []:
            for r in (resp.get("body") or {}).get("results") or []:
                if r.get("hoa") and r.get("prefix"):
                    out[r["hoa"]] = r["prefix"]  # later report overrides earlier
    return out


def _county_for(name: str, name_to_prefix: dict[str, str]) -> str | None:
    p = name_to_prefix.get(name) or ""
    parts = p.split("/")
    if len(parts) >= 3 and parts[0] == "v1" and parts[1] == STATE:
        return parts[2]
    return None


def _is_downstate_eligible(name: str, name_to_prefix: dict[str, str]) -> tuple[bool, str]:
    """Return (eligible, reason). Eligible = not Chicagoland's territory."""
    county = _county_for(name, name_to_prefix)
    if county is None:
        return True, "unmapped"  # safe default — caller can vet
    if county in CHICAGOLAND_COUNTIES:
        return False, f"chicagoland:{county}"
    if county in CROSS_STATE_LEAK_PREFIXES:
        return True, f"cross-state-leak:{county}"
    if county in RECOVERY_PREFIXES:
        return True, f"recovery:{county}"
    if county in DOWNSTATE_COUNTIES:
        return True, f"downstate:{county}"
    return True, f"other:{county}"


# --- doc-filename audit ---

# Tokens that strongly suggest the doc belongs to a different state's HOA.
_FOREIGN_STATE_TOKENS = re.compile(
    r"\b("
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    r"florida|georgia|hawaii|idaho|indiana|iowa|kansas|kentucky|louisiana|maine|"
    r"maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|"
    r"nebraska|nevada|new-?hampshire|new-?jersey|new-?mexico|new-?york|"
    r"north-?carolina|north-?dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode-?island|south-?carolina|south-?dakota|tennessee|texas|utah|vermont|"
    r"virginia|washington|west-?virginia|wisconsin|wyoming"
    r")\b",
    re.I,
)
# Hosts that almost never publish HOA-specific governing docs.
_BAD_HOST_TOKENS = re.compile(
    r"(siouxvalleyenergy|coopextension|cooperative|"
    r"\.gov/AgendaCenter|legis\.|illinoiscourts|idfpr\.illinois|"
    r"oflaherty-law|dicklerlaw|robbinsdimonte|sfbbg\.|"
    r"abclocal|news|tribune|herald|gazette|"
    r"casetext|caselaw\.findlaw|law\.justia|"
    r"sec\.gov|10-?[KQ])",
    re.I,
)


def filename_audit_flags(docs: list[dict]) -> list[str]:
    """Return list of flags for this HOA's docs. Empty list = clean."""
    flags = []
    for d in docs:
        fn = d.get("filename") or d.get("relative_path") or ""
        url = d.get("source_url") or d.get("url") or ""
        if _FOREIGN_STATE_TOKENS.search(fn):
            flags.append(f"foreign_state_filename:{fn[:60]}")
        if url and _BAD_HOST_TOKENS.search(url):
            flags.append(f"bad_host:{urllib.parse.urlparse(url).netloc}")
    return flags


# --- driver ---


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--out-dir",
                   default="state_scrapers/il/results/cleanup_downstate")
    p.add_argument("--name-to-prefix",
                   action="append",
                   default=None,
                   help="Path to a live_import_report.json (used to derive bank prefix per HOA name). May be passed multiple times; later files override earlier on collision.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--fallback-model", default=FALLBACK_MODEL)
    p.add_argument("--max-text-chars", type=int, default=3500)
    p.add_argument("--min-confidence", type=float, default=0.7)
    p.add_argument("--apply", action="store_true",
                   help="Actually call /admin/rename-hoa and /admin/delete-hoa.")
    p.add_argument("--sleep-s", type=float, default=1.0,
                   help="Pause between API calls (Render is fragile under concurrent load).")
    p.add_argument("--llm-sleep-s", type=float, default=0.05)
    p.add_argument("--skip-heuristic-delete", action="store_true")
    p.add_argument("--skip-llm-rename", action="store_true")
    p.add_argument("--skip-null-delete", action="store_true")
    p.add_argument("--skip-filename-audit", action="store_true")
    p.add_argument("--skip-dedup", action="store_true")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = out_dir / "cleanup_decisions.jsonl"

    paths = [Path(p) for p in (args.name_to_prefix or [
        "state_scrapers/il/results/il_20260508_114942_claude_phase2/live_import_report.json",
    ])]
    name_to_prefix = _name_to_prefix_map(paths)
    print(f"name->prefix entries: {len(name_to_prefix)} from {len(paths)} report(s)", file=sys.stderr)

    summaries = _fetch_summaries(args.base_url, STATE)
    print(f"IL live total: {len(summaries)}", file=sys.stderr)

    eligible: list[tuple[dict, str]] = []
    excluded: list[tuple[dict, str]] = []
    for r in summaries:
        ok, why = _is_downstate_eligible(r.get("hoa") or "", name_to_prefix)
        if ok:
            eligible.append((r, why))
        else:
            excluded.append((r, why))
    print(f"eligible (downstate scope): {len(eligible)} | excluded (Chicagoland): {len(excluded)}",
          file=sys.stderr)

    decisions: list[dict] = []
    delete_ids: set[int] = set()
    rename_payloads: list[dict] = []

    # ---- Stage 1: heuristic deletes (no cost) ----
    if not args.skip_heuristic_delete:
        for row, scope_reason in eligible:
            name = row.get("hoa") or ""
            ok, pat = is_heuristic_delete(name)
            if ok:
                decisions.append({
                    "stage": "heuristic_delete",
                    "hoa_id": row["hoa_id"],
                    "old_name": name,
                    "scope_reason": scope_reason,
                    "pattern": pat,
                })
                delete_ids.add(int(row["hoa_id"]))
        # Cross-state-leak: delete unconditionally
        for row, scope_reason in eligible:
            if scope_reason.startswith("cross-state-leak:"):
                if row["hoa_id"] not in delete_ids:
                    decisions.append({
                        "stage": "heuristic_delete",
                        "hoa_id": row["hoa_id"],
                        "old_name": row["hoa"],
                        "scope_reason": scope_reason,
                        "pattern": "cross_state_leak",
                    })
                    delete_ids.add(int(row["hoa_id"]))
        print(f"stage 1 heuristic deletes: {len(delete_ids)}", file=sys.stderr)

    # ---- Stage 2: unconditional LLM rename ----
    if not args.skip_llm_rename:
        client = _llm_client()
        rename_candidates = [
            (row, scope_reason) for row, scope_reason in eligible
            if int(row["hoa_id"]) not in delete_ids
        ]
        print(f"stage 2 LLM rename candidates: {len(rename_candidates)}", file=sys.stderr)
        for i, (row, scope_reason) in enumerate(rename_candidates, 1):
            old = row.get("hoa") or ""
            # Try deterministic prefix-strip first.
            stripped = _try_strip_prefix(old)
            if stripped and _looks_canonical(stripped) and stripped != old:
                decisions.append({
                    "stage": "llm_rename",
                    "hoa_id": row["hoa_id"],
                    "old_name": old,
                    "scope_reason": scope_reason,
                    "canonical_name": stripped,
                    "confidence": 0.95,
                    "method": "deterministic_prefix_strip",
                })
                rename_payloads.append({"hoa_id": row["hoa_id"], "new_name": stripped})
                continue

            text = _fetch_doc_text(args.base_url, old, max_chars=args.max_text_chars)
            time.sleep(args.sleep_s)  # pace Render
            ans = _llm_json(client, args.model, PERMISSIVE_SYSTEM, name_prompt(old, text))
            if ans is None or ans.get("_error"):
                ans = _llm_json(client, args.fallback_model, PERMISSIVE_SYSTEM, name_prompt(old, text)) or {}
            canonical = (ans or {}).get("canonical_name")
            confidence = float((ans or {}).get("confidence") or 0)
            reason = (ans or {}).get("reason") or (ans or {}).get("_error") or ""
            decisions.append({
                "stage": "llm_rename",
                "hoa_id": row["hoa_id"],
                "old_name": old,
                "scope_reason": scope_reason,
                "canonical_name": canonical,
                "confidence": confidence,
                "method": "llm_permissive",
                "llm_reason": reason,
            })
            if (
                _looks_canonical(canonical)
                and confidence >= args.min_confidence
                and _normalize(canonical) != _normalize(old)
            ):
                rename_payloads.append({"hoa_id": row["hoa_id"], "new_name": canonical.strip()})
            elif not canonical:
                # null canonical → stage 3 will hard-delete
                pass
            if i % 5 == 0:
                print(f"  llm_rename {i}/{len(rename_candidates)}", file=sys.stderr)
                _flush_decisions(decisions_path, decisions)
            time.sleep(args.llm_sleep_s)

    # ---- Stage 3: null-delete ----
    if not args.skip_null_delete:
        renamed_ids = {p["hoa_id"] for p in rename_payloads}
        for d in decisions:
            if d.get("stage") != "llm_rename":
                continue
            if d.get("canonical_name") in (None, "", "null"):
                if d["hoa_id"] not in renamed_ids and d["hoa_id"] not in delete_ids:
                    delete_ids.add(int(d["hoa_id"]))
                    decisions.append({
                        "stage": "null_delete",
                        "hoa_id": d["hoa_id"],
                        "old_name": d["old_name"],
                        "scope_reason": d.get("scope_reason"),
                        "llm_reason": d.get("llm_reason"),
                    })
        print(f"stage 3 null-delete total to-delete: {len(delete_ids)}", file=sys.stderr)

    # ---- Stage 4: filename audit ----
    if not args.skip_filename_audit:
        survivors = [
            (row, scope_reason) for row, scope_reason in eligible
            if int(row["hoa_id"]) not in delete_ids
        ]
        rename_lookup = {p["hoa_id"]: p["new_name"] for p in rename_payloads}
        print(f"stage 4 filename-audit survivors: {len(survivors)}", file=sys.stderr)
        for i, (row, scope_reason) in enumerate(survivors, 1):
            # Use proposed new name for the doc fetch if a rename was queued
            fetch_name = rename_lookup.get(row["hoa_id"], row["hoa"])
            docs = _fetch_documents(args.base_url, fetch_name)
            time.sleep(args.sleep_s)  # pace Render
            flags = filename_audit_flags(docs)
            if flags:
                decisions.append({
                    "stage": "filename_audit",
                    "hoa_id": row["hoa_id"],
                    "old_name": row["hoa"],
                    "fetched_as": fetch_name,
                    "flags": flags,
                    "doc_count": len(docs),
                })
                # Hard-delete only if ≥2 flags, OR foreign_state_filename present.
                # Single bad-host docs can be a false positive (e.g. a real
                # HOA whose only doc happens to be linked from a news article).
                hard = (
                    sum(1 for f in flags if f.startswith("foreign_state_filename")) > 0
                    or len(flags) >= 2
                )
                if hard:
                    delete_ids.add(int(row["hoa_id"]))
                    decisions[-1]["delete"] = True
            if i % 10 == 0:
                print(f"  filename_audit {i}/{len(survivors)}", file=sys.stderr)
                _flush_decisions(decisions_path, decisions)

    # ---- Stage 5: dedup ----
    if not args.skip_dedup:
        survivors_for_dedup = [
            row for row, _ in eligible
            if int(row["hoa_id"]) not in delete_ids
        ]
        # Apply renames in-memory so the dedup signature uses post-rename names
        rename_lookup = {p["hoa_id"]: p["new_name"] for p in rename_payloads}
        groups = defaultdict(list)
        for r in survivors_for_dedup:
            new_name = rename_lookup.get(r["hoa_id"], r["hoa"])
            sig = signature(new_name)
            if sig and len(sig) >= 4:
                groups[sig].append({**r, "_post_rename_name": new_name})
        dedup_groups = [(sig, gs) for sig, gs in groups.items() if len(gs) > 1]
        print(f"stage 5 dedup groups (size ≥ 2): {len(dedup_groups)}", file=sys.stderr)
        client = _llm_client()
        for sig, group in dedup_groups:
            present = [{**g, "hoa": g["_post_rename_name"]} for g in group]
            ans = _llm_json(client, args.model, DEDUP_SYSTEM, dedup_prompt(present))
            if ans is None or ans.get("_error"):
                ans = _llm_json(client, args.fallback_model, DEDUP_SYSTEM, dedup_prompt(present)) or {}
            same = bool((ans or {}).get("same_hoa"))
            keep_id = (ans or {}).get("keep_id")
            canonical = (ans or {}).get("canonical_name")
            decisions.append({
                "stage": "dedup",
                "signature": sig,
                "members": [{"id": g["hoa_id"], "name": g["_post_rename_name"]} for g in group],
                "same_hoa": same,
                "keep_id": keep_id,
                "canonical_name": canonical,
                "reason": (ans or {}).get("reason"),
            })
            if same and keep_id and canonical and _looks_canonical(canonical):
                rename_payloads.append({"hoa_id": int(keep_id), "new_name": canonical.strip()})
                for g in group:
                    if int(g["hoa_id"]) != int(keep_id):
                        rename_payloads.append({"hoa_id": int(g["hoa_id"]), "new_name": canonical.strip()})
            time.sleep(args.llm_sleep_s)

    _flush_decisions(decisions_path, decisions)

    # ---- summary ----
    print(json.dumps({
        "live_total": len(summaries),
        "eligible": len(eligible),
        "excluded_chicagoland": len(excluded),
        "rename_payloads": len(rename_payloads),
        "delete_ids": len(delete_ids),
        "decisions_path": str(decisions_path),
    }, sort_keys=True))

    if not args.apply:
        print("dry-run; pass --apply to execute renames + deletes", file=sys.stderr)
        return 0

    token = _live_admin_token()
    if not token:
        print("no admin token; cannot apply", file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bearer {token}"}

    # Apply renames first (so dedup-merge survivors are properly named)
    if rename_payloads:
        # de-dup payloads — last-write-wins per hoa_id
        seen: dict[int, str] = {}
        for p in rename_payloads:
            seen[int(p["hoa_id"])] = p["new_name"]
        chunks = [
            [{"hoa_id": k, "new_name": v} for k, v in list(seen.items())[i:i + 50]]
            for i in range(0, len(seen), 50)
        ]
        for i, chunk in enumerate(chunks, 1):
            r = requests.post(
                f"{args.base_url}/admin/rename-hoa",
                headers=headers,
                json={"renames": chunk},
                timeout=300,
            )
            r.raise_for_status()
            payload = r.json()
            print(f"rename chunk {i}/{len(chunks)}: {payload.get('renamed')} renamed, "
                  f"{payload.get('merged')} merged, {payload.get('errors')} errors",
                  file=sys.stderr)
            time.sleep(args.sleep_s)

    # Apply deletes
    if delete_ids:
        ids = sorted(delete_ids)
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            r = requests.post(
                f"{args.base_url}/admin/delete-hoa",
                headers=headers,
                json={"hoa_ids": chunk, "dry_run": False},
                timeout=300,
            )
            r.raise_for_status()
            payload = r.json()
            print(f"delete chunk {i//50 + 1}: {payload.get('deleted')} deleted, "
                  f"{payload.get('errors')} errors", file=sys.stderr)
            time.sleep(args.sleep_s)

    return 0


def _flush_decisions(path: Path, decisions: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))


if __name__ == "__main__":
    sys.exit(main())

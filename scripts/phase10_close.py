#!/usr/bin/env python3
"""Phase 10 close module — runs after a state's run_state_ingestion.py finishes.

Order of operations:
  1. LLM rename pass (clean_dirty_hoa_names.py --no-dirty-filter --apply).
  2. Hard-delete LLM canonical_name=null residuals via /admin/delete-hoa.
  3. Doc-filename audit — flag and delete entries whose docs reference a
     different state, or whose source URLs are utility/.gov/news, or who are
     pre-run junk-sinks accumulating mismatched docs.
  4. Bbox audit — log any /hoas/map-points entry whose lat/lon falls outside
     the state bbox (warning only; the canonical bucket-binds-bbox fix is in
     prepare_bank_for_ingest.py).
  5. Write final_state_report.json + notes/retrospective.md scaffold.

Designed to be called from the orchestrator as:
    python scripts/phase10_close.py --state DC --bbox-json '{"min_lat":...}' \
        --run-id dc_20260507_overnight
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_BASE_URL = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")
RENAME_SCRIPT = ROOT / "state_scrapers/ga/scripts/clean_dirty_hoa_names.py"

# Doc-filename audit — flag-and-delete heuristics
US_STATE_TOKENS = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY",
    "LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC",
}
JUNK_HOST_RE = re.compile(
    r"(siouxvalleyenergy|valleyenergy|cooperative|"
    r"\.gov/AgendaCenter|\.gov/DocumentCenter/View/[0-9]+/[A-Z][a-z]+-Newsletter|"
    r"newsletter|press[- ]release|"
    r"legis\..*\.gov|\.legislature\.|"
    r"news\.|\.news\.|tribune\.|gazette\.|herald\.|times\.|post\.|press\.|"
    r"realtor|zillow|redfin|trulia|homes\.com)",
    re.IGNORECASE,
)


# Cumulative non-HOA-name regex set — accumulated across the May 2026 batch
# (SD/ND/AK/AR/MS/WV/NE/NM/OK/MT). Each pattern caught real bank-stage
# misclassifications that the LLM rename pass left as renamed-but-still-junk.
# Names are checked AS-IS against the live HOA name; safelist below protects
# real all-caps HOAs.
NON_HOA_NAME_PATTERNS = [
    # Recording stamps / pagination
    r"^\d+ of \d+\b", r"^\d+\.\s+", r"^\d{4}r-?\d", r"^Doc[#]",
    r"\bRecording Dist", r"\bRecorded HOA$", r"\.pdf HOA$",
    # "Of " / "of " fragment prefixes (NM particularly)
    r"^Of\s+", r"^of [A-Z]", r"^of\s*,", r"^of \d",
    # Government / municipal
    r"\b(City|Town|Municipality|Borough) of\b",
    r"\bCounty of [A-Z]", r"\bCity and (Borough|County) of\b",
    r"\b(Code|Ordinance|Zoning|Subdivision Regulations?|Submittal Guidelines)\b",
    r"\b(Land Development|Maintenance Agreement|Growth Area|Overlay District|Comprehensive Plan)\b",
    r"\bUnified Development\b", r"\bChapter Five\b", r"\bChapter Two\b",
    r"\bCHAPTER [V\d]", r"\bArticle [VIX\d]+\b", r"\bAo No\.",
    r"\bPlanning (Department|Commission|Board)\b",
    # Plat-page extracts (OK / AR)
    r"\bCurve Table\b", r"\bBLOCKS \d+-\d+\b",
    r"\bCertificate of Dedication\b", r"\bDEED OF DEDICATION AND RESTRICTIVE\b",
    r"\bBill of Assurance\b",
    # Public-lands records (MT/WY/NM)
    r"\bWilderness Area\b", r"\bForest Service\b",
    r"\bConservation Easement\b", r"\bIrrigation District\b",
    r"\bGrazing\b", r"\bFishing Access\b", r"\bAcequia\b",
    # Utility / financial / legal-scholarship
    r"\bENERGY (INC|CORP)\b", r"\bWater (Association|Authority|District)\b",
    r"\bBar Rag\b", r"\bColumns Featured\b", r"\bIn This Issue\b",
    r"\bThree secrets\b", r"\bForeclosure\b",
    r"\bSchool (District|Board)\b", r"\bAffordable Housing\b",
    r"\bHOUSING (FINANCE CORP|AUTHORITY)\b",
    r"\bProperty Trust, Inc\b", r"\bForm 10[-]?[KQ]\b", r"\bSEC Filing\b",
    r"\bMERS\b", r"\bIRS\b",
    # Title insurance / closing
    r"\bTitle Insurance\b", r"\bTitle Company\b",
    r"\bAbstract Company\b", r"\bClosing Company\b",
    r"\bHOMEOWNER.S POLICY\b",
    # Legal / scholarship / publications
    r"\bAttorney", r"\bLAW UPDATE\b", r"\bLaw Update\b",
    r"\bSection Law\b", r"\bAnnual Report\b",
    r"\bLandmark Nomination\b", r"\bGrassroots\b",
    r"\bChamber of Commerce\b", r"\bCommunity Health\b",
    r"\bRecorder\b", r"\bRegister of Deeds\b",
    r"\bTransfer Fees\b",
    # Boilerplate / filing
    r"\bAccess Agreement\b", r"\bAgreement HOA$",
    r"\bExhibit [A-Z]\b", r"\bDue Diligence\b", r"\b\.xlsx\b",
    r"\bRequest for (Proposal|Quote)\b", r"\bOpen Records\b",
    # Person-name only (very narrow — single first+last+HOA)
    r"^Candi Ussery\b", r"^Becky\b",
    # OCR garbage
    r"\bilil\b", r"\billil\b", r"\bH OA$",
    r"\bSeconp\b",
    # Generic single-word fragments + suffix fragments
    r"^Conditions?$", r"^Restrictions?$", r"^Restrictive\b",
    r"^Protective\b", r"^PROTECTIVE\b", r"^Subdivision\b",
    r"^Untitled\b", r"^Test\b",
    r"^[A-Z][a-z]+$",  # single capitalized word
    r"^Powder Ridge$", r"^Bentonville$",
    r"^Northbrook$", r"^Subdivision HOA$",
    # Street-address-only HOA names
    r"^\d{3,5}\s+\w+\s+(Street|Road|Avenue|Drive|Lane|Way|Place|Boulevard)\b",
]
_NON_HOA_REGEXES = [re.compile(p, re.IGNORECASE) for p in NON_HOA_NAME_PATTERNS]

# All-caps HOA names that ARE real (don't match `^\w+\s+HOA$`)
NON_HOA_NAME_SAFELIST = {
    "BELLA VISTA TOWNHOUSE ASSOCIATION",
    "ALASKAN BAY OWNERS ASSOCIATION",
    "EASTRIDGE 4 CONDOMINIUM ASSOCIATION",
    "POTTER CREEK HOMEOWNER ASSOCIATION",
    "PALISADES HOMEOWNERS ASSOCIATION, INC.",
    "WINDJAMMER CONDOMINIUM ASSOCIATION, INC.",
}


def regex_match_non_hoa(name: str) -> str | None:
    """Return the first matching pattern (string), or None if name looks HOA-shaped."""
    if not name:
        return "empty_name"
    if name.upper() in NON_HOA_NAME_SAFELIST:
        return None
    # Structural safelist: names ending in an unambiguous community-type
    # suffix are real entities — registry-derived names like "3025 Porter
    # Street Condo" or "Kewalo Tower Condominium" are otherwise tripped by
    # the street-address-prefix and single-word patterns. Apply BEFORE the
    # regex sweep.
    if re.search(
        r"\b(condo|condominium|condominiums|cooperative|coop|co-?op|association|"
        r"council|homeowners?|owners|townhomes?|townhouse|villas?|tower|"
        r"apartments?|units?\s+owners)\b\s*(?:,?\s*inc\.?|,?\s*l\.?l\.?c\.?)?\.?\s*$",
        name, re.IGNORECASE,
    ):
        return None
    for r in _NON_HOA_REGEXES:
        if r.search(name):
            return r.pattern
    return None


def regex_delete_candidates(state: str, base_url: str) -> dict[str, Any]:
    """Sweep current live entries for the cumulative non-HOA regex set.

    Returns {"flags": [...], "delete_ids": [...]} matching doc_filename_audit shape.
    """
    summary = fetch_summary(state, base_url)
    flags: list[dict[str, Any]] = []
    delete_ids: list[int] = []
    for row in summary:
        hoa_id = row.get("hoa_id")
        hoa_name = row.get("hoa") or ""
        pattern = regex_match_non_hoa(hoa_name)
        if pattern:
            flags.append({"hoa_id": hoa_id, "hoa": hoa_name, "matched_pattern": pattern})
            if isinstance(hoa_id, int):
                delete_ids.append(hoa_id)
    return {"flags": flags, "delete_ids": delete_ids}


# --- Dedupe-merge -----------------------------------------------------------

def _dedupe_normalize(s: str) -> str:
    """Aggressive normalization for substring-containment matching."""
    n = s.upper()
    # Drop common HOA suffix variants and punctuation noise
    n = re.sub(r"[,.\-'’]", " ", n)
    n = re.sub(r"\bINC(?:ORPORATED)?\b\.?", "", n)
    n = re.sub(r"\bLLC\b\.?", "", n)
    n = re.sub(r"\b(HOA|H\.O\.A\.|POA|P\.O\.A\.)\b", "", n)
    n = re.sub(r"\bHOMEOWNERS?\s+ASSOCIATION\b", "", n)
    n = re.sub(r"\bHOMEOWNER\b", "", n)
    n = re.sub(r"\bPROPERTY\s+OWNERS?\s+ASSOCIATION\b", "", n)
    n = re.sub(r"\bOWNERS?\s+ASSOCIATION\b", "", n)
    n = re.sub(r"\bCONDOMINIUM\s+ASSOCIATION\b", "", n)
    n = re.sub(r"\bCONDOMINIUM\b", "", n)
    n = re.sub(r"\bCOMMUNITY\s+ASSOCIATION\b", "", n)
    n = re.sub(r"\bASSOCIATION\b", "", n)
    n = re.sub(r"\bSUBDIVISION\b", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def find_dedupe_pairs(state: str, base_url: str) -> list[dict[str, Any]]:
    """Find HOA pairs that are likely the same community under different names.

    Returns a list of merge-actions: {source_id, source_name, target_id,
    target_name, normalized_overlap}. The intended call order: rename
    `source_name` to `target_name` via /admin/rename-hoa, which triggers the
    merge-on-collision path.
    """
    summary = fetch_summary(state, base_url)
    # Build (id, name, normalized) tuples for entries that survived earlier
    # delete passes. Skip empties.
    rows = []
    for row in summary:
        hoa_id = row.get("hoa_id")
        hoa_name = (row.get("hoa") or "").strip()
        if not isinstance(hoa_id, int) or not hoa_name:
            continue
        norm = _dedupe_normalize(hoa_name)
        if len(norm) < 4:  # too short to dedupe safely
            continue
        rows.append({"hoa_id": hoa_id, "hoa": hoa_name, "norm": norm,
                     "doc_count": row.get("doc_count") or 0,
                     "chunk_count": row.get("chunk_count") or 0})

    pairs: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a["hoa_id"] == b["hoa_id"]:
                continue
            key = tuple(sorted([a["hoa_id"], b["hoa_id"]]))
            if key in seen_pairs:
                continue
            # Substring containment OR identical normalized form
            same = a["norm"] == b["norm"]
            contained = (
                len(a["norm"]) >= 6 and len(b["norm"]) >= 6
                and (a["norm"] in b["norm"] or b["norm"] in a["norm"])
            )
            if not (same or contained):
                continue
            # Pick target = longer original name (more canonical), or
            # higher chunk_count when names are same length. Source = the
            # shorter / lower-quality one, which gets renamed into target.
            if len(a["hoa"]) > len(b["hoa"]) or (
                len(a["hoa"]) == len(b["hoa"]) and a["chunk_count"] > b["chunk_count"]
            ):
                source, target = b, a
            else:
                source, target = a, b
            pairs.append({
                "source_id": source["hoa_id"],
                "source_name": source["hoa"],
                "target_id": target["hoa_id"],
                "target_name": target["hoa"],
                "shared_norm": a["norm"] if same else (
                    a["norm"] if a["norm"] in b["norm"] else b["norm"]
                ),
            })
            seen_pairs.add(key)
    return pairs


def apply_dedupe_merges(pairs: list[dict[str, Any]], *, apply: bool, base_url: str) -> dict[str, Any]:
    """Rename each source -> target_name; the endpoint merges on collision."""
    if not pairs:
        return {"merged": 0, "reason": "no_pairs"}
    if not apply:
        return {"would_merge": len(pairs), "samples": pairs[:8]}
    token = live_admin_token()
    if not token:
        return {"skipped": True, "reason": "missing_admin_bearer"}
    merged = 0
    errors: list[dict[str, Any]] = []
    for pair in pairs:
        body = {"renames": [{"hoa_id": pair["source_id"], "new_name": pair["target_name"]}], "dry_run": False}
        last_err = None
        for attempt in range(6):
            try:
                r = requests.post(
                    f"{base_url}/admin/rename-hoa",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=body, timeout=180,
                )
                if r.status_code == 200:
                    j = r.json()
                    if j.get("merged") or any(
                        (item.get("status") == "merged") for item in j.get("results", [])
                    ):
                        merged += 1
                    last_err = None
                    break
                last_err = f"http {r.status_code}: {r.text[:200]}"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(20 + 5 * attempt)
        if last_err:
            errors.append({"pair": pair, "error": last_err})
    return {"merged": merged, "errors": errors, "attempted": len(pairs)}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def live_admin_token() -> str | None:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if api_key and service_id:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{service_id}/env-vars",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except Exception:
            pass
    return os.environ.get("JWT_SECRET")


def run_rename_pass(state: str, out_path: Path, *, apply: bool, base_url: str) -> dict[str, Any]:
    """Step 1: LLM rename pass (--no-dirty-filter)."""
    if not RENAME_SCRIPT.exists():
        return {"skipped": True, "reason": "missing_rename_script"}
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(RENAME_SCRIPT),
        "--state", state,
        "--base-url", base_url,
        "--out", str(out_path),
        "--no-dirty-filter",
    ]
    if apply:
        cmd.append("--apply")
    log_path = out_path.with_suffix(".log")
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=log, stderr=subprocess.STDOUT)
    return {"command": cmd, "returncode": proc.returncode, "log": str(log_path), "ledger": str(out_path)}


def parse_null_canonical_ids(ledger: Path) -> list[int]:
    """Step 2 helper: parse rename ledger for entries the LLM declined to rename."""
    null_ids: list[int] = []
    if not ledger.exists():
        return null_ids
    for line in ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        canonical = row.get("canonical_name")
        hoa_id = row.get("hoa_id")
        if canonical in (None, "", "null") and isinstance(hoa_id, int):
            null_ids.append(hoa_id)
    return null_ids


def hard_delete(state: str, hoa_ids: list[int], *, apply: bool, base_url: str, label: str) -> dict[str, Any]:
    if not hoa_ids:
        return {"deleted": 0, "reason": "no_candidates", "label": label}
    token = live_admin_token()
    if not token:
        return {"skipped": True, "reason": "missing_admin_bearer", "label": label}
    if not apply:
        return {"would_delete": len(hoa_ids), "ids_sample": hoa_ids[:10], "label": label}
    deleted = 0
    errors: list[dict[str, Any]] = []
    # 6× retries to survive sqlite write-locks per playbook guidance
    for hoa_id in hoa_ids:
        last_err = None
        for attempt in range(6):
            try:
                r = requests.post(
                    f"{base_url}/admin/delete-hoa",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"hoa_ids": [hoa_id], "dry_run": False},
                    timeout=120,
                )
                if r.status_code == 200:
                    deleted += 1
                    last_err = None
                    break
                last_err = f"http {r.status_code}: {r.text[:200]}"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(20 + 5 * attempt)
        if last_err:
            errors.append({"hoa_id": hoa_id, "error": last_err})
    return {"deleted": deleted, "errors": errors, "label": label, "attempted": len(hoa_ids)}


def fetch_summary(state: str, base_url: str) -> list[dict[str, Any]]:
    try:
        r = requests.get(f"{base_url}/hoas/summary", params={"state": state}, timeout=60)
        if r.status_code == 200:
            data = r.json()
            return data.get("results") if isinstance(data, dict) else (data or [])
    except Exception:
        pass
    return []


def fetch_documents(hoa_name: str, base_url: str) -> list[dict[str, Any]]:
    try:
        r = requests.get(f"{base_url}/hoas/{requests.utils.quote(hoa_name)}/documents", timeout=60)
        if r.status_code == 200:
            data = r.json()
            return data.get("results") if isinstance(data, dict) else (data or [])
    except Exception:
        pass
    return []


def doc_filename_audit(state: str, base_url: str, run_started_at: str) -> dict[str, Any]:
    """Step 3: flag entries whose docs belong to a different HOA/state, are
    utility/news/.gov junk, or are pre-run junk-sinks. Return list of hoa_ids
    to delete plus an audit log."""
    summary = fetch_summary(state, base_url)
    flags: list[dict[str, Any]] = []
    delete_ids: list[int] = []
    foreign_state_re = re.compile(
        r"\b(?P<st>" + "|".join(s for s in US_STATE_TOKENS if s != state) + r")\b"
    )
    started_at_dt = None
    try:
        started_at_dt = datetime.fromisoformat(run_started_at.replace("Z", "+00:00"))
    except Exception:
        pass
    for row in summary:
        hoa_id = row.get("hoa_id")
        hoa_name = row.get("hoa") or ""
        doc_count = row.get("doc_count") or 0
        last_ingested = row.get("last_ingested") or ""
        # Junk-sink heuristic: doc_count > 3 and earliest doc predates run started_at
        # (pre-existing accumulation; rename pass masks the contamination by picking
        # one document's name). Conservative — only flag obvious cases.
        is_junk_sink_candidate = doc_count > 5
        # Fetch docs to inspect filenames
        docs = fetch_documents(hoa_name, base_url) if isinstance(hoa_id, int) else []
        if not docs:
            continue
        foreign_state_hits = 0
        junk_host_hits = 0
        for d in docs:
            fname = (d.get("filename") or d.get("path") or "").upper()
            url = d.get("source_url") or ""
            if foreign_state_re.search(fname):
                foreign_state_hits += 1
            if JUNK_HOST_RE.search(url):
                junk_host_hits += 1
        # Decision: delete if majority of docs are foreign-state OR junk-host;
        # OR all docs are junk-host on a doc_count<=3 entry.
        delete_reason = None
        total = len(docs) or 1
        if foreign_state_hits and foreign_state_hits >= max(2, total // 2):
            delete_reason = f"foreign_state_filenames:{foreign_state_hits}/{total}"
        elif junk_host_hits and junk_host_hits == total:
            delete_reason = f"junk_host_only:{junk_host_hits}/{total}"
        elif is_junk_sink_candidate and foreign_state_hits >= 2:
            delete_reason = f"junk_sink_with_foreign:{foreign_state_hits}/{total}"
        flag = {
            "hoa_id": hoa_id,
            "hoa": hoa_name,
            "doc_count": doc_count,
            "foreign_state_hits": foreign_state_hits,
            "junk_host_hits": junk_host_hits,
            "delete_reason": delete_reason,
        }
        flags.append(flag)
        if delete_reason and isinstance(hoa_id, int):
            delete_ids.append(hoa_id)
    return {"flags": flags, "delete_ids": delete_ids}


def bbox_audit(state: str, base_url: str, bbox: dict[str, float]) -> dict[str, Any]:
    """Step 4: log any map points outside the state bbox."""
    out_of_bbox: list[dict[str, Any]] = []
    try:
        r = requests.get(f"{base_url}/hoas/map-points", params={"state": state}, timeout=60)
        if r.status_code != 200:
            return {"error": f"http {r.status_code}", "out_of_bbox": []}
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else []
        if isinstance(data, list):
            for p in data:
                if not isinstance(p, dict):
                    continue
                lat = p.get("latitude")
                lon = p.get("longitude")
                if lat is None or lon is None:
                    continue
                try:
                    lat_f = float(lat)
                    lon_f = float(lon)
                except Exception:
                    continue
                if not (
                    bbox["min_lat"] <= lat_f <= bbox["max_lat"]
                    and bbox["min_lon"] <= lon_f <= bbox["max_lon"]
                ):
                    out_of_bbox.append(p)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "out_of_bbox": []}
    return {"out_of_bbox_count": len(out_of_bbox), "samples": out_of_bbox[:10]}


def write_retrospective(state: str, run_dir: Path, report: dict[str, Any]) -> Path:
    notes_dir = ROOT / f"state_scrapers/{state.lower()}/notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    canonical = notes_dir / "retrospective.md"
    # Don't clobber a manually-curated retrospective. If `retrospective.md`
    # exists and is not itself an earlier auto-generated stub, write to a
    # sibling `retrospective_auto.md` instead so the operator can compare/merge.
    if canonical.exists():
        head = canonical.read_text(encoding="utf-8", errors="ignore")[:300]
        if "Auto-generated by phase10_close.py" in head:
            path = canonical
        else:
            path = notes_dir / "retrospective_auto.md"
    else:
        path = canonical
    summary = report.get("verify", {}).get("summary") or {}
    total = (
        summary.get("total")
        if isinstance(summary, dict) and isinstance(summary.get("total"), int)
        else (len(summary.get("results", [])) if isinstance(summary, dict) else None)
    )
    body = f"""# {state} HOA Scrape Retrospective

Auto-generated by phase10_close.py at {now_iso()}.

## Final state

- Run dir: `{run_dir}`
- Live HOA count (post-Phase 10): {total}
- Out-of-bbox map points: {report.get("bbox_audit", {}).get("out_of_bbox_count")}
- Rename pass: {report.get("rename", {}).get("returncode")}
- Hard-deleted (null canonical residuals): {report.get("delete_null", {}).get("deleted") or report.get("delete_null", {}).get("would_delete")}
- Hard-deleted (doc-filename audit): {report.get("delete_audit", {}).get("deleted") or report.get("delete_audit", {}).get("would_delete")}

## Source families attempted

- Per-county Serper (metro counties >50k)
- State-wide host-family + mgmt-co + statute-anchored Serper

## Lessons learned

(populate manually after reviewing the run.)

## Files

- Final state report: `final_state_report.json` in run dir
- Rename ledger: `name_cleanup_unconditional.jsonl` in run dir
- Doc-filename audit: `doc_filename_audit.json` in run dir
- Bbox audit: `bbox_audit.json` in run dir
"""
    path.write_text(body, encoding="utf-8")
    return path


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="Two-letter state code, e.g. DC")
    parser.add_argument("--bbox-json", required=True, help='JSON dict with min_lat,max_lat,min_lon,max_lon')
    parser.add_argument("--run-id", required=True, help="Same run_id used in run_state_ingestion.py")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-rename", action="store_true")
    parser.add_argument("--skip-audit", action="store_true")
    args = parser.parse_args()

    state = args.state.upper()
    bbox = json.loads(args.bbox_json)
    run_dir = ROOT / f"state_scrapers/{state.lower()}/results" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started_at = now_iso()
    report: dict[str, Any] = {
        "state": state,
        "run_id": args.run_id,
        "phase10_started_at": started_at,
        "apply": args.apply,
    }

    # 1. LLM rename pass
    if not args.skip_rename:
        ledger = run_dir / "name_cleanup_unconditional.jsonl"
        report["rename"] = run_rename_pass(state, ledger, apply=args.apply, base_url=args.base_url)
        # 2. Hard-delete null canonicals
        null_ids = parse_null_canonical_ids(ledger)
        report["delete_null"] = hard_delete(
            state, null_ids, apply=args.apply, base_url=args.base_url, label="null_canonical"
        )

    # 2b. Cumulative-regex non-HOA delete sweep (catches what the LLM null
    # decision missed: gov ordinances, plat extracts, OCR fragments,
    # state-specific leak patterns from May 2026 batch).
    regex_sweep = regex_delete_candidates(state, args.base_url)
    (run_dir / "regex_delete_candidates.json").write_text(
        json.dumps(regex_sweep, indent=2, sort_keys=True), encoding="utf-8"
    )
    report["regex_flag_count"] = len(regex_sweep.get("flags", []))
    report["delete_regex"] = hard_delete(
        state, regex_sweep.get("delete_ids", []),
        apply=args.apply, base_url=args.base_url, label="non_hoa_regex",
    )

    # 3. Doc-filename audit
    if not args.skip_audit:
        audit = doc_filename_audit(state, args.base_url, started_at)
        (run_dir / "doc_filename_audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
        report["audit_flag_count"] = len(audit.get("flags", []))
        report["delete_audit"] = hard_delete(
            state, audit.get("delete_ids", []), apply=args.apply, base_url=args.base_url, label="doc_filename_audit"
        )

    # 3b. Dedupe-merge — same HOA appearing under two near-identical names
    # (Hillcrest Condominium vs Hillcrest Condominium Association; XYZ Estates
    # HOA vs XYZ Estates Homeowners Association, Inc.). May 2026 batch had
    # 11 such pairs in MT, 3 in AK, 3 in MS.
    dedupe_pairs = find_dedupe_pairs(state, args.base_url)
    (run_dir / "dedupe_pairs.json").write_text(
        json.dumps(dedupe_pairs, indent=2, sort_keys=True), encoding="utf-8"
    )
    report["dedupe_pair_count"] = len(dedupe_pairs)
    report["dedupe_merge"] = apply_dedupe_merges(
        dedupe_pairs, apply=args.apply, base_url=args.base_url
    )

    # 4. Bbox audit
    bb = bbox_audit(state, args.base_url, bbox)
    (run_dir / "bbox_audit.json").write_text(json.dumps(bb, indent=2, sort_keys=True), encoding="utf-8")
    report["bbox_audit"] = bb

    # 5. Verify post-cleanup
    try:
        r = requests.get(f"{args.base_url}/hoas/summary", params={"state": state}, timeout=60)
        report["verify"] = {"status": r.status_code, "summary": r.json() if r.status_code == 200 else r.text[:500]}
    except Exception as exc:
        report["verify"] = {"error": f"{type(exc).__name__}: {exc}"}

    report["phase10_finished_at"] = now_iso()

    # Final state report
    final_report_path = run_dir / "final_state_report.json"
    existing = {}
    if final_report_path.exists():
        try:
            existing = json.loads(final_report_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    merged = {**existing, "phase10": report}
    final_report_path.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")

    # Retrospective
    retro = write_retrospective(state, run_dir, report)
    summary_keys = (
        "audit_flag_count", "regex_flag_count", "dedupe_pair_count",
        "bbox_audit", "delete_null", "delete_regex", "delete_audit", "dedupe_merge",
    )
    print(json.dumps({
        "state": state, "run_id": args.run_id, "retrospective": str(retro),
        **{k: v for k, v in report.items() if k in summary_keys},
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

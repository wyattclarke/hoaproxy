#!/usr/bin/env python3
"""LLM-based content-relevance grader for live HOAs.

For each HOA in a state (or list), fetch all chunk text via the public
`/hoas/{name}/documents/searchable` HTML endpoint, ask an LLM whether the
content is a real HOA governing document for the named HOA, and output
JSON verdicts.

Verdicts (per HOA):
  - "real"     — at least one document contains substantive HOA governing
                 content (CC&Rs, declaration, bylaws, articles, rules,
                 amendments, master deed, board resolutions).
  - "junk_only"— every document is junk (annual report receipt, delinquency
                 notice, government cover sheet, unrelated content, doc
                 about a different HOA, court doc, etc.).
  - "no_docs"  — HOA has 0 documents (kept; intentional in some states).
  - "error"    — fetch/grading failed; review manually.

Output: JSON file with the full per-HOA grading + a summary block.
The companion script `delete_junk_hoas.py` consumes the output.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / "settings.env")


class _RateLimiter:
    """Process-wide simple token bucket — at most N requests per second."""
    def __init__(self, rps: float = 3.0):
        self.min_gap = 1.0 / rps
        self._next = 0.0
        import threading as _t
        self._lock = _t.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.time()
            self._next = now + self.min_gap


_LIMITER = _RateLimiter(rps=float(os.environ.get("GRADER_RPS", "2.5")))

DEFAULT_BASE_URL = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

CHUNK_RE = re.compile(
    r"Chunk\s+\d+\s*·\s*Page[^<]*?</header>\s*<pre[^>]*>([\s\S]*?)</pre>",
    re.IGNORECASE,
)
ALT_CHUNK_RE = re.compile(r"<pre[^>]*>([\s\S]*?)</pre>", re.IGNORECASE)


def http_get_json(url: str, *, timeout: int = 60, retries: int = 4) -> Any:
    last_err = None
    for i in range(retries):
        _LIMITER.wait()
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            last_err = f"http {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(5 + i * 5)
    raise RuntimeError(f"GET {url} failed: {last_err}")


def http_get_text(url: str, *, timeout: int = 60, retries: int = 4) -> str | None:
    last_err = None
    for i in range(retries):
        _LIMITER.wait()
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
            last_err = f"http {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(5 + i * 5)
    raise RuntimeError(f"GET {url} failed: {last_err}")


def fetch_state_hoas(state: str, base_url: str) -> list[dict]:
    """Page through /hoas/summary, returning every entry for the state."""
    out: list[dict] = []
    offset = 0
    page = 200
    while True:
        url = f"{base_url}/hoas/summary?state={state}&limit={page}&offset={offset}"
        data = http_get_json(url)
        if not data or not data.get("results"):
            break
        out.extend(data["results"])
        if len(data["results"]) < page:
            break
        offset += page
        if offset > 50000:
            break
    return out


def fetch_doc_list(hoa_name: str, base_url: str) -> list[dict]:
    enc = quote(hoa_name, safe="")
    data = http_get_json(f"{base_url}/hoas/{enc}/documents")
    return data or []


def extract_chunk_texts(html_body: str) -> list[str]:
    """Pull <pre>…</pre> chunk bodies from the searchable view HTML."""
    matches = CHUNK_RE.findall(html_body)
    if not matches:
        # Fallback: grab every <pre> (works on simpler templates).
        matches = ALT_CHUNK_RE.findall(html_body)
    out: list[str] = []
    for m in matches:
        text = html.unescape(m)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return out


def fetch_doc_text(hoa_name: str, doc_path: str, base_url: str, *, max_chars: int = 4000) -> str:
    enc_name = quote(hoa_name, safe="")
    enc_path = quote(doc_path, safe="")
    body = http_get_text(f"{base_url}/hoas/{enc_name}/documents/searchable?path={enc_path}")
    if not body:
        return ""
    chunks = extract_chunk_texts(body)
    text = "\n\n".join(chunks).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


GRADER_SYSTEM = (
    "You audit document text on a public HOA / condo association website. "
    "For each HOA, you receive its name and the extracted text from its document(s). "
    "Decide whether the text constitutes useful governing-document content "
    "for THAT HOA, or junk. "
    "Respond with strict JSON: "
    '{"verdict": "real" | "junk", "category": "<short>", "reason": "<one line>"}'
)

GRADER_INSTRUCTIONS = (
    "REAL governing content includes: declaration, declaration of covenants, "
    "CC&Rs, restrictive covenants, master deed, articles of incorporation "
    "(only when substantive — not a 1-page filing receipt), bylaws, rules and "
    "regulations, amendments, supplemental declarations, board resolutions, "
    "operating agreements, plat with covenants, owner-rights provisions. "
    "JUNK includes: annual-report filing receipts (Form 631, ASN), "
    "non-filing / delinquency notices, certificates of good standing, "
    "filing cover sheets / state filing letters with no body content, "
    "blank or near-blank pages, court rulings, tax forms, financial reports "
    "alone, membership lists, ballots, property listings, news articles, "
    "newsletters, local government ordinances/zoning, paywalled stub pages, "
    "or content clearly about a DIFFERENT HOA / different state / unrelated "
    "organization. "
    "If the text is mostly headers/letterhead and the HOA's name appears only "
    "as the addressee with no governing body content, it is JUNK. "
    "If multiple documents are concatenated and AT LEAST ONE has substantive "
    "governing content for this HOA, return REAL. "
    "Output ONLY the JSON object. No prose."
)


def call_openrouter(prompt: str, *, api_key: str, model: str, timeout: int = 90) -> dict:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": GRADER_SYSTEM + "\n\n" + GRADER_INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
    }
    last_err = None
    for attempt in range(4):
        try:
            r = requests.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://hoaproxy.org",
                    "X-Title": "hoaproxy text quality grader",
                },
                json=body,
                timeout=timeout,
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"].get("content") or ""
                content = content.strip()
                # Some models wrap JSON in code fences
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
                if not content:
                    return {"verdict": "error", "category": "empty_content", "reason": "LLM returned empty"}
                # Try a strict parse first; if it fails, slice the first {...}
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    m = re.search(r"\{[\s\S]*\}", content)
                    if m:
                        try:
                            return json.loads(m.group(0))
                        except Exception:
                            pass
                    return {"verdict": "error", "category": "bad_json", "reason": content[:200]}
            last_err = f"http {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(3 + attempt * 3)
    return {"verdict": "error", "category": "grader_failed", "reason": last_err or "unknown"}


def grade_hoa(
    hoa: dict,
    *,
    base_url: str,
    api_key: str,
    model: str,
    sample_chars: int,
) -> dict:
    name = hoa.get("hoa") or ""
    hoa_id = hoa.get("hoa_id")
    doc_count = hoa.get("doc_count") or 0
    out: dict[str, Any] = {
        "hoa_id": hoa_id,
        "hoa": name,
        "city": hoa.get("city"),
        "state": hoa.get("state"),
        "doc_count": doc_count,
        "chunk_count": hoa.get("chunk_count") or 0,
    }
    if doc_count == 0:
        out["verdict"] = "no_docs"
        out["reason"] = "no documents — kept (intentional in some states)"
        return out
    try:
        docs = fetch_doc_list(name, base_url)
    except Exception as e:
        out["verdict"] = "error"
        out["reason"] = f"doc_list: {e}"
        return out
    if not docs:
        out["verdict"] = "no_docs"
        out["reason"] = "doc list empty"
        return out

    text_blobs: list[str] = []
    # Sample up to 4 docs per HOA to keep wall time bounded.
    for d in docs[:4]:
        path = d.get("relative_path")
        if not path:
            continue
        try:
            t = fetch_doc_text(name, path, base_url, max_chars=sample_chars)
        except Exception:
            t = ""
        if t:
            text_blobs.append(f"--- DOC: {path} ---\n{t}")
    combined = "\n\n".join(text_blobs).strip()
    if not combined:
        out["verdict"] = "junk"
        out["category"] = "no_extractable_text"
        out["reason"] = "all docs returned no chunks"
        return out

    # Cap total context
    if len(combined) > 9000:
        combined = combined[:9000] + "…"

    prompt = f"HOA name: {name}\nState: {hoa.get('state')}\n\nDocument text:\n{combined}"
    verdict = call_openrouter(prompt, api_key=api_key, model=model)
    # Auto-retry with claude-haiku if the primary model returned empty/bad-json
    if verdict.get("verdict") == "error" and model != "anthropic/claude-haiku-4.5":
        verdict = call_openrouter(prompt, api_key=api_key, model="anthropic/claude-haiku-4.5")
    out.update({
        "verdict": verdict.get("verdict") or "error",
        "category": verdict.get("category"),
        "reason": verdict.get("reason"),
    })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--out", required=True, help="Path to write JSON results")
    ap.add_argument("--limit", type=int, default=0, help="Max HOAs to grade (0=all)")
    ap.add_argument("--sample", type=int, default=0,
                    help="If >0, randomly sample N HOAs (for state quality probe)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--sample-chars", type=int, default=2500,
                    help="Max chars per document text passed to grader")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--with-docs-only", action="store_true",
                    help="Skip HOAs with 0 docs entirely")
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY missing", file=sys.stderr)
        return 2

    print(f"[grader] fetching HOAs for state={args.state}", flush=True)
    hoas = fetch_state_hoas(args.state, args.base_url)
    print(f"[grader] {len(hoas)} HOAs in state", flush=True)

    if args.with_docs_only:
        hoas = [h for h in hoas if (h.get("doc_count") or 0) > 0]
        print(f"[grader] {len(hoas)} HOAs with doc_count>0", flush=True)

    if args.sample and args.sample < len(hoas):
        import random
        random.seed(args.seed)
        hoas = random.sample(hoas, args.sample)
        print(f"[grader] random sample of {len(hoas)}", flush=True)
    elif args.limit and args.limit < len(hoas):
        hoas = hoas[: args.limit]
        print(f"[grader] limited to first {len(hoas)}", flush=True)

    results: list[dict] = []
    done = 0
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                grade_hoa, h,
                base_url=args.base_url, api_key=api_key,
                model=args.model, sample_chars=args.sample_chars,
            ): h for h in hoas
        }
        for fut in as_completed(futures):
            try:
                row = fut.result()
            except Exception as e:
                src = futures[fut]
                row = {
                    "hoa_id": src.get("hoa_id"), "hoa": src.get("hoa"),
                    "verdict": "error", "reason": f"{type(e).__name__}: {e}",
                }
            results.append(row)
            done += 1
            if done % 25 == 0 or done == len(hoas):
                print(f"[grader] {done}/{len(hoas)} graded", flush=True)
                # checkpoint
                tmp = out_path.with_suffix(out_path.suffix + ".tmp")
                tmp.write_text(json.dumps({"state": args.state, "in_progress": True,
                                            "results": results}, indent=2))

    counts: dict[str, int] = {}
    for r in results:
        v = r.get("verdict") or "unknown"
        counts[v] = counts.get(v, 0) + 1

    summary = {
        "state": args.state,
        "model": args.model,
        "total_graded": len(results),
        "verdict_counts": counts,
        "results": sorted(results, key=lambda r: (r.get("verdict") or "", r.get("hoa") or "")),
    }
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"[grader] wrote {out_path}", flush=True)
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

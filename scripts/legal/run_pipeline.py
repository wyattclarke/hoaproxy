#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LEGAL_DIR = ROOT / "scripts" / "legal"


def _run(step: str, args: list[str]) -> None:
    cmd = [sys.executable, str(LEGAL_DIR / step), *args]
    print(f"[run] {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run end-to-end legal corpus pipeline.")
    parser.add_argument("--state", type=str, default=None, help="Optional state filter for fetch/normalize/extract/assemble")
    parser.add_argument("--limit", type=int, default=0, help="Optional max sources for fetch/normalize/extract")
    parser.add_argument(
        "--rebuild-source-map",
        action="store_true",
        help="Rebuild source_map.json before running (off by default).",
    )
    parser.add_argument(
        "--refresh-fetch",
        action="store_true",
        help="Re-fetch sources even when source_url already exists in sources metadata.",
    )
    parser.add_argument(
        "--force-normalize",
        action="store_true",
        help="Re-normalize snapshots even when already normalized.",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip validation/progress-index update steps.",
    )
    parser.add_argument(
        "--rebuild-proxy-matrix",
        action="store_true",
        help="Rebuild proxy requirement matrix before run.",
    )
    parser.add_argument(
        "--include-aggregators",
        action="store_true",
        help="Include aggregator sources during fetch/extract (default: off).",
    )
    args = parser.parse_args()

    state_args = ["--state", args.state] if args.state else []
    limit_args = ["--limit", str(args.limit)] if args.limit and args.limit > 0 else []
    source_map_path = ROOT / "data" / "legal" / "source_map.json"
    proxy_matrix_path = ROOT / "data" / "legal" / "proxy_requirement_matrix.json"
    if args.rebuild_source_map or not source_map_path.exists():
        _run("build_source_map.py", [])
    if args.rebuild_proxy_matrix or not proxy_matrix_path.exists():
        _run("build_proxy_requirement_matrix.py", [])
    fetch_args = [*state_args, *limit_args]
    normalize_args = [*state_args, *limit_args]
    extract_args = [*state_args, *limit_args]
    if args.refresh_fetch:
        fetch_args.append("--refresh")
    if args.force_normalize:
        normalize_args.append("--force")
    if args.include_aggregators:
        fetch_args.append("--include-aggregators")
        extract_args.append("--include-aggregators")

    _run("fetch_law_texts.py", fetch_args)
    _run("normalize_law_texts.py", normalize_args)
    _run("extract_rules.py", extract_args)
    _run("assemble_profiles.py", [*state_args])
    if not args.skip_validate:
        _run("build_electronic_proxy_summary.py", [])
        _run("validate_corpus.py", [])
        _run("update_progress_index.py", [])
    print("Legal corpus pipeline complete.")


if __name__ == "__main__":
    main()

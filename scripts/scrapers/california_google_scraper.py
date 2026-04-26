"""Backward-compatible wrapper — use google_ccr_scraper.py --state ca instead."""
from __future__ import annotations
import sys
sys.argv = [sys.argv[0], "--state", "ca"] + sys.argv[1:]
from scrapers.google_ccr_scraper import main
main()

# State HOA Document Scrapers — Progress Tracker

## Texas
- [x] Download TREC HOA Management Certificate CSV (~16,400 records)
  - Source: `data.texas.gov/dataset/TREC-HOA-Management-Certificates/8auc-hzdi`
  - Fields: Name, County, City, Zip, Type (POA/COA), Certificate PDF URL
  - Saved to `data/TREC_HOA_Management_Certificates_20260329.csv`
- [x] Build certificate URL extraction pipeline (`scripts/trec_extract_urls.py`)
  - Downloads certificate PDFs in batches, OCRs with tesseract (parallel), extracts website URLs via regex
  - Disk-friendly: deletes PDFs after OCR, resumes on interrupt
  - 44% hit rate on test batch (100 certs → 44 HOA website URLs)
  - Output: `data/trec_texas/extracted_urls.jsonl`
- [x] Run full extraction (16K certs)
  - 8,350 HOAs processed, ~3,300 with website URLs
- [x] Scrape CC&Rs from discovered HOA websites (`scripts/trec_scrape_docs.py`)
  - Crawls HOA websites for governing doc PDFs (CC&Rs, bylaws, declarations)
  - Skips login-walled management portals (ConnectResident, AppFolio, etc.)
  - Playwright retry (`scripts/trec_playwright_retry.py`) for JS-rendered portals (TownSq, etc.) — gained 154 additional HOAs
  - Result: 842 HOAs with docs, 4,287 uploadable files (10-file cap per HOA, priority-ranked)
  - Output: `scraped_hoa_docs/trec_texas/{slug}/` + `data/trec_texas/import.json`
- [x] Bulk-import metadata via `/admin/bulk-import` (8,350 HOA records)
- [x] Upload documents — 734 HOAs via API (`scripts/upload_trec_to_site.py`), remaining ~108 via local ingestion (`scripts/ingest.py --mode local`)
  - Server-side ingestion semaphore added to prevent OOM
  - Backup throttled (VACUUM INTO → incremental sqlite3 backup API)
  - Local ingestion with parallel workers (4x) using per-worker Qdrant paths

## Florida
- [ ] Download DBPR condo CSV bulk data (~27K records)
  - Source: `myfloridalicense.com/condos-timeshares-mobile-homes/public-records/`
  - Weekly-refreshed CSVs with 16 fields including managing entity name/address
  - Direct download, no scraping needed
  - Note: Only condos/co-ops/timeshares — HOA registry expired 2016
- [ ] Build scraper pipeline
  - Parse CSVs → extract management company names → search for websites → reuse `trec_scrape_docs.py` doc-discovery logic
  - Blocked by CSV download

## Colorado
- [x] Obtain CIC registration data
  - CORA request bypassed — full active-HOA CSV downloaded directly from DORA website
  - ~11,700 records with name, address, county, zip, units, management company, assessment data
  - Saved to `data/colorado_hoa_active.csv`
  - Note: CSV has duplicate rows per HOA (one per management company name variant) — dedupe on credential number
- [ ] Build metadata import pipeline
  - Dedupe CSV on credential number, import HOA profiles to site via `/admin/bulk-import`
- [ ] Document scraping via county recorder offices
  - CC&Rs are recorded documents — county is already in the CSV
  - Same approach as California Track B, but Colorado recorder portals may differ
  - Assess feasibility before building (California recorders were index-only, no downloadable docs)
- [ ] Fallback: web search for CC&R PDFs (same pattern as California Track C)

## Maryland (low priority)
- [ ] Evaluate keyword search approach
  - No dedicated HOA registry — HOAs mixed into general business entity search
  - Portal: `egov.maryland.gov/BusinessExpress` (JS-rendered, needs Playwright)
  - Would require keyword searches ("homeowners", "property owners", etc.)
  - ~6–8K HOAs estimated but no way to isolate them
  - Deprioritized: poor effort/reward ratio

## California
- [x] Scrape california-homeowners-associations.com (`scripts/scrapers/scrape_california_hoa.py`)
  - 5,778 HOAs with management company info, board members, cities
  - Output: `data/california/hoa_details.jsonl`
- [x] Track A: Management company website scraping (`scripts/scrapers/california_mgmt_scraper.py`)
  - Crawled management company websites for CC&R PDFs
  - Result: 0 docs — most management portals require login
- [x] Track B: County recorder portals (abandoned)
  - Explored Orange County and San Diego County recorder portals
  - Finding: portals are index-only, no viewable/downloadable document images
  - CC&Rs filed by developers (not HOAs), making name-based search unreliable
- [x] Track C: Web search for CC&R PDFs (`scripts/scrapers/california_google_scraper.py`)
  - Searches Serper.dev Google API for `"HOA_NAME" filetype:pdf CC&R OR declaration`
  - Also crawls result pages for embedded PDF links
  - Post-download validation via pdfminer: rejects court filings, IRS 990s, city docs, newspapers
  - Requires `SERPER_API_KEY` in settings.env (2,500 free queries at serper.dev, no credit card)
  - First run (2,296 of 5,778 searched): **513 HOAs with docs, 1,614 PDFs, 5.9 GB** (22% hit rate)
  - 185 junk PDFs auto-rejected, 204 search errors at tail (credits exhausted)
  - Resumable — re-run after topping up Serper credits to cover remaining ~3,300 HOAs
  - Output: `scraped_hoa_docs/california/{slug}/` + `data/california/google_import.json`
- [ ] Bulk-import to site — 513 HOAs queued in `data/ingest_queue/pending/`
  - Local ingestion via `scripts/ingest.py --mode local --source california_google_ccr_scrape --workers 4`
  - Then SCP artifacts (PDFs, SQLite, Qdrant) to Render
- [ ] Top up Serper credits and scrape remaining ~3,300 CA HOAs

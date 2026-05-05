# Tennessee Discovery Handoff

Updated: 2026-05-05

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/TN/`
- Starting count: 0 manifests, 0 PDFs.
- Active strategy: deterministic county/city and source-family search first; OpenRouter only for compact validation if deterministic selection flattens.
- Reusable scraper: `benchmark/scrape_state_serper_docpages.py`.
- Initial query file: `benchmark/tn_initial_queries.txt`.

## Guardrails

- Leads must use `state="TN"` so documents land under the Tennessee bank prefix.
- Use `hoaware.discovery.probe.probe()` / `hoaware.bank.bank_hoa()` as the write path.
- Do not use Gemini or Qwen Flash.
- Do not send secrets, cookies, resident data, private portal content, emails, payment data, or internal/work data to any model.
- Respect robots.txt with `HOA_DISCOVERY_RESPECT_ROBOTS=1` and practical delays.

## Counties Started

- Davidson / Nashville
- Williamson / Franklin / Brentwood
- Rutherford / Murfreesboro
- Knox / Knoxville
- Hamilton / Chattanooga
- Shelby / Memphis / Collierville
- Sumner / Hendersonville
- Wilson / Mt. Juliet / Lebanon

## Running Log

- 2026-05-05: Starting TN bank coverage was 0 manifests and 0 PDFs.
- 2026-05-05: Initial deterministic Serper pass used `benchmark/tn_initial_queries.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_initial_direct_1/`
  - Search calls: 82; raw results: 591; unique URLs: 453; raw leads: 80.
  - Deterministic direct-PDF cleaning accepted 37 candidates, then compact OpenRouter name repair kept 36.
  - OpenRouter usage: 5 `deepseek/deepseek-v4-flash` calls for metadata-only name repair; no fallback model used.
  - Banked 36 PDFs into 35 manifests. Fox Run merged two PDFs into one manifest.
  - Count after pass: 35 manifests, 36 PDFs.
- 2026-05-05: Source-family pass used `benchmark/tn_source_family_queries.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_source_family_1/`
  - Search calls: 90; raw results: 541; unique URLs: 393; raw leads: 120.
  - Deterministic direct-PDF cleaning accepted 70 candidates; 14 exact URLs were already banked.
  - Compact OpenRouter name repair ran on 56 new URLs and kept 50 leads, all with `deepseek/deepseek-v4-flash`.
  - Before banking, local corrections normalized `CareIlton` to `Carellton` and merged Whistle Stop variants under `Whistle Stop Farms Homeowners Association`.
  - Banked 50 PDFs with 0 skips. Count after pass: 75 manifests, 85 PDFs.
- 2026-05-05: Static-host pass used `benchmark/tn_static_host_queries.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_static_hosts_1/`
  - Raw leads: 54; deterministic direct-PDF cleaning accepted 15; 2 exact URLs were already banked.
  - Compact OpenRouter name repair ran on 13 new URLs and kept 11. It rejected a municipal subdivision-regulations PDF and one unclear update/newsletter-style candidate.
  - Banked 11 PDFs with 0 skips. Count after pass: 86 manifests, 96 PDFs.
- 2026-05-05: Deep legal/recorder phrase pass used `benchmark/tn_deep_legal_phrase_queries.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_deep_legal_1/`
  - Search calls: 72; raw results: 359; unique URLs: 270; raw leads: 140.
  - Dedupe against prior TN repaired URLs left 100 new URLs for deterministic cleaning.
  - Deterministic direct-PDF cleaning accepted 40 candidates; compact OpenRouter name repair kept 33 with `deepseek/deepseek-v4-flash`.
  - Before banking, local normalization merged `Fredonia Nature Resort` into `Fredonia Mountain Nature Resort`.
  - Banked 33 PDFs with 0 skips. Count after pass: 114 manifests, 128 PDFs.
- 2026-05-05: Management-host expansion used `benchmark/tn_management_host_queries.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_management_hosts_1/`
  - Search calls: 78; raw results: 225; unique URLs: 200; raw leads: 76.
  - Dedupe against prior TN repaired URLs left 54 new URLs; deterministic direct-PDF cleaning accepted 15.
  - Compact OpenRouter name repair kept 14 and rejected one unclear McKay's Mill supplemental document.
  - Banked 14 PDFs with 0 skips. Count after pass: 126 manifests, 142 PDFs.

## Productive Source Families

- `psmtllc.com/wp-content/uploads/` direct PDFs: Jamison Place, Ridge at Carters Station, Clearview Acres, Walden Woods, Three Rivers, Muirwood.
- `wmco.net/wp-content/uploads/` / `wmco.net/assets/uploads/` direct PDFs: Barefoot Bay, Rutherford Green, Bonbrook, Stonecrest Brentwood, Highland View.
- HOA-owned WordPress/static sites: Fox Run, White Plains, Hidden Harbor, Berryhill, Legacy Bay, Riverwalk, Savannah Ridge, Sedgefield.
- `irp.cdn-website.com/.../files/uploaded/` direct PDFs: Brush Creek and Lee Crossing.
- `irp-cdn.multiscreensite.com/.../files/uploaded/` direct PDFs: Creekstone Village, Nashboro Village, Estates of Hickory Woods.
- GoDaddy blob downloads can work when title/snippet identify the association; first pass added Wyngate.
- Smithbilt/builder-hosted PDFs produced several Knoxville/Sumner-area communities: Hayden Farms, Manor in the Foothills, Winchester Commons, Honey Oaks, Carellton, Canterbury.
- HOA-owned WordPress/Squarespace/static domains in Knox/Shelby/Williamson remain productive when direct PDFs are preflighted.
- CloudFront/static-host PDFs produced Sunset Pointe, River Watch, RiverBend Hills, Retreats at White Oak, and related direct covenants.
- HOA Express-style `/file/document` and `/file/document-page` URLs produced Meadows Condominium, Fredericksburg/Brentwood Pointe-style documents, Chesney Hills, and similar direct PDFs.
- Legal/recorder phrase searches are still productive when direct PDFs are cleaned first. Latest additions included Braystone Park, Pine Creek Estates, Buckingham Place, Silver Springs, Victoria Park, Padgett Hill, Hatties Place, Park Run, Veterans Cove, Pennfield, Ivan Creek, Bakertown Woods, Providence Landing, Halle Plantation, Westwind Reserve, Millgate, Lone Mountain Shores, Featherfoot Point, Splendor Oaks, Lake Meadows, Chestnut Cove, and Chapel Creek.
- Management-host expansion remains useful but is starting to duplicate prior finds. Latest additions included Estates of Primm Springs, Breckenridge, Polk Place, Belvoir, Reserve at Spencer Creek, Amerine Station, Creek Bend Farms, Belltown, Carrington Place, Ambrose, Benelli Park, and Hawks Landing.

## False Positives / Reject Patterns

- State/government reports and packets: `tn.gov`, legislative studies, county subdivision regulations, public utility dockets.
- CAI legislative reports and court filings.
- Real-estate/listing hosts such as LandHub, Showcase, Chicago Title, auction/property packet hosts.
- Generic welcome packages, forms, applications, minutes, budgets, newsletters, and pool/lease documents.
- Out-of-state hits triggered by city names like Franklin or Brentwood.

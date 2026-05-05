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
- 2026-05-05: Secondary-metro expansion used `benchmark/tn_secondary_metro_queries.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_secondary_metros_1/`
  - Search calls: 68; raw results: 332; unique URLs: 294; raw leads: 160.
  - Dedupe against prior TN repaired URLs left 147 new URLs; deterministic direct-PDF cleaning accepted 20.
  - One AWS signed URL was excluded before model repair or banking; signed URLs must stay out of model prompts and the bank.
  - Compact OpenRouter name repair ran on the remaining 19 public URLs and kept 10 with `deepseek/deepseek-v4-flash`.
  - Banked 10 PDFs with 0 skips. Count after pass: 136 manifests, 152 PDFs.
- 2026-05-05: Public static-host expansion used `benchmark/tn_public_static_pdf_queries_2.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_tn_serper_docpages_public_static_2/`
  - Search calls: 80; raw results: 516; unique URLs: 447; raw leads: 149.
  - Exact GCS source-URL prefilter removed 24 already-banked URLs and found 0 signed URLs, leaving 125 new public direct PDFs.
  - Deterministic direct-PDF cleaning accepted 14; local manual filtering removed 5 duplicate/newsletter candidates before model repair.
  - Compact OpenRouter name repair ran on 9 candidates with `deepseek/deepseek-v4-flash`. One response shape dropped two keep decisions from the helper output, so final names were normalized locally from the public source domains, URLs, and audit.
  - Banked 9 PDFs with 0 skips. Count after pass: 143 manifests, 161 PDFs.
- 2026-05-05: Regional legal-phrase expansion used `benchmark/tn_regional_legal_phrase_queries_2.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_tn_serper_docpages_regional_legal_2/`
  - Search calls: 64; raw results: 61; unique URLs: 59; raw leads: 49.
  - Exact GCS source-URL prefilter removed 8 already-banked URLs and found 0 signed URLs, leaving 41 new public direct PDFs.
  - Deterministic direct-PDF cleaning accepted 4; local manual filtering removed 2 Tellico Tell-E-Gram newsletter PDFs.
  - Compact OpenRouter name repair kept 2 with `deepseek/deepseek-v4-flash` after passing Serper audit snippets for state/name context.
  - Banked 2 PDFs with 0 skips. Count after pass: 145 manifests, 163 PDFs.
- 2026-05-05: Builder/realtor host expansion used `benchmark/tn_builder_realtor_host_queries_2.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_tn_serper_docpages_builder_realtor_2/`
  - Search calls: 60; raw results: 107; unique URLs: 67; raw leads: 17.
  - Exact GCS source-URL prefilter removed 7 already-banked URLs and found 0 signed URLs, leaving 10 new public direct PDFs.
  - Deterministic direct-PDF cleaning accepted 1. Compact OpenRouter repair returned a single kept decision for Calla Crossing, which was applied from the audit.
  - Banked 1 PDF with 0 skips. Count after pass: 146 manifests, 164 PDFs.
- 2026-05-05: HOA Express-style document expansion used `benchmark/tn_hoaexpress_document_queries_2.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_tn_serper_docpages_hoaexpress_2/`
  - Search calls: 56; raw results: 227; unique URLs: 118; raw leads: 44.
  - Exact GCS source-URL prefilter removed 3 already-banked URLs and found 0 signed URLs, leaving 41 new public document endpoints.
  - Deterministic PDF/text cleaning accepted 6; compact OpenRouter repair kept 4 and rejected a newsletter-style Fredericksburg candidate plus a generic HOAleader-style guide.
  - Local normalization merged two River Sound name variants before banking.
  - Banked 4 PDFs with 0 skips. Count after pass: 149 manifests, 168 PDFs.
- 2026-05-05: HOA Express-style governing-term extension used `benchmark/tn_hoaexpress_document_queries_3.txt`.
  - Raw search output: `benchmark/results/tn_serper_docpages_tn_serper_docpages_hoaexpress_3/`
  - Search calls: 60; raw results: 223; unique URLs: 99; raw leads: 23.
  - Exact GCS source-URL prefilter removed 4 already-banked URLs and found 0 signed URLs, leaving 19 new public document endpoints.
  - Deterministic PDF/text cleaning accepted 2; compact OpenRouter repair kept both with `deepseek/deepseek-v4-flash`.
  - Banked 2 PDFs with 0 skips. Count after pass: 151 manifests, 170 PDFs.

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
- Secondary-metro city searches still produce useful finds when deduped and cleaned first. Latest additions included Vineyard Grove, Chandler Point, Tellico Village, Lakeview Estates, Middlebrook, Steeplechase, Shiloh Springs, Jackson Square, Ashberry Farms, and Canyons.
- Public static-host expansion now has lower marginal yield but still added Clifftops, Sedman Hill Townhomes, Thrasher Landing, Townes at Horse Creek Farms, Grand Valley Lakes, McKay's Mill, Old Capitol Town, and Grandview.
- Regional legal-phrase expansion appears low yield after prior passes. Latest additions were Shagbark and Jackson Creek.
- Builder/realtor host expansion is mostly exhausted after prior passes. Latest addition was Calla Crossing; Lee Godfrey and Smithbilt searches now mostly duplicate or reject.
- HOA Express-style `/file/document` and `/file/document-page` expansion remains worth targeted use. Latest additions were River Sound, Abbottsford, and Montgomery Cove.
- HOA Express governing-term extension added River Plantation Section 1 and Villas at Lyons Crossing, but marginal yield dropped to 2 banked PDFs.

## False Positives / Reject Patterns

- State/government reports and packets: `tn.gov`, legislative studies, county subdivision regulations, public utility dockets.
- CAI legislative reports and court filings.
- Real-estate/listing hosts such as LandHub, Showcase, Chicago Title, auction/property packet hosts.
- Generic welcome packages, forms, applications, minutes, budgets, newsletters, and pool/lease documents.
- Out-of-state hits triggered by city names like Franklin or Brentwood.
- Signed or credentialed URLs from otherwise public-looking search results, especially AWS query strings containing `AWSAccessKeyId`, `Signature`, or `X-Amz-Signature`. Exclude these before model repair and banking.
- Tellico Village `tgYYYYMMDD.pdf` Tell-E-Gram PDFs are newsletters and should be rejected even if they contain covenant/legal snippets.

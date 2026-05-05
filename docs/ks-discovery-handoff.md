# Kansas Discovery Handoff

Updated: 2026-05-05

User instruction: continue autonomously for KS. Do not stop at checkpoints. Commit or hand off as needed, then immediately keep scraping. Only final-answer if blocked, out of budget, or asked for status.

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/KS/`
- Current count: 703 manifests, 1,577 PDFs
- OpenRouter credits: about `$10.73 / $20` used, about `$9.27` remaining
- Active KS work: continuing deterministic source-family scraping; no Gemini.
- An unrelated NC benchmark process may be running; leave it alone.
- Do not commit `benchmark/results/`, `benchmark/run_benchmark.sh`, or `benchmark/task.txt`.
- `hoaware/discovery/__main__.py` was already dirty and should not be touched unless specifically needed.

## Best Strategy So Far

1. Use deterministic Serper search to collect candidate URLs.
2. Dedupe candidates against prior validated URLs.
3. Use OpenRouter only for compact validation batches.
4. Probe validated leads one at a time with a subprocess timeout when hosts are fragile.
5. Prefer host/source-family expansion over generic county sweeps once a productive pattern appears.
6. For direct public PDF hits, manually clean/group the raw inferred names before probing. Use same-host crawl for document-library sites, but retry slow hosts as direct-only `pre_discovered_pdf_urls` with `website=null`.
7. Do not use Gemini for this workflow. It was too expensive for the yield and is blocked by `HOA_DISCOVERY_MODEL_BLOCKLIST`.

Highest-yield source families:

- eNeighbors public-document URLs and `/p/{community}` pages.
- Independent HOA/community domains discovered with `-eneighbors -ha-kc` queries.
- Municipal document center URLs, especially `DocumentCenter/View`.
- County-strict independent-domain passes for Sedgwick, Douglas, Riley/Pottawatomie, Leavenworth, Butler.
- Direct `filetype:pdf` city searches for large Kansas HOA cities, followed by manual cleanup and grouped probes. The first cleaned batch banked Willowbrooke Villas, Amber Meadows, Meadows at Shawnee, Equestrian Estates, Foxfire Addition, Avenbury Lakes, Montclair, Andover Forest, Deer Valley, Battle Creek, Sycamore Village, Reflection Ridge, Shadow Rock, and Prairie Creek 6th.
- Host-pattern searches with `inurl:/file/document/`, `inurl:hmsft-doc`, `inurl:/wp-content/uploads/`, `site:gogladly.com/connect/document`, and `site:pmtechsol.sfo2.cdn.digitaloceanspaces.com/hmsft-documents`. This banked Maple Crest, Crescent Lakes, Primrose, Hallbrook East Village, Falcon Ridge, Woodland Park, Southwood, Lake Kahola, Wyndham Heights, Wildcat Woods, Cedar Ridge, Sterling East, Quivira Falls, Brooks Farm, Holly Ridge, Willow Ridge, Ryan's Run, and Milburn Fields.
- Legal-phrase PDF searches with `Kansas not-for-profit corporation`, `Kansas non-profit corporation`, `Register of Deeds`, and county names. This banked high-quality Johnson/Sedgwick/secondary-city documents including Seven Hills, Arlington Estates, St. Andrews Place, Walnut Creek Estates, Oak Hill, Wycliff, Normandy Place, Homestead Woods, Park Glen Estates, The Cedars, Kensington Valley, Tanglewood Lake, Copper Creek, Falcon Valley Villas, Foxfield Village, and Red Oak Hills.
- Secondary-city direct PDF search can still work when constrained by host/document phrases. Lansing Ridge II and Bel-Aire Estates were high-yield crawls; broad secondary-city search without those constraints remains noisy.
- Broader legal/amendment phrase searches continued to add coverage after the first legal pass. Useful phrases included `Homes Association Declaration`, `Articles of Incorporation`, `Amendment to Declaration`, `Restated Bylaws`, `Supplemental Declaration`, and county-specific `Register of Deeds` wording. These added Summerfield Farm, Rock Creek Estates, Homestead Creek, Avignon Villa, Foxborough, Timber Creek III, Hawthorne Valley, Comotara, Gleason Glen, Pheasant Run, Grand Mere, Woodland Ridge, Rockwood Estates, Tamarind, Hampton Place, High Point, Wilderness, Wilshire, The Oaks, West Ridge, Boulder Hills, Melrose Reserve, Falcon Lakes, and Running Horse.
- A small OpenRouter/Gemini county-query pass for Wyandotte was noisy but found useful Kansas-side leads after manual cleanup: WestLake, Erika's Place, and Riverview Bluffs banked PDFs; Prairie Oaks and Country Side created manifests but did not produce usable PDFs from the probed pages.
- A direct hmsft/HOAMsoft source expansion was the best late-stage deterministic branch. It added or improved Kensington at St. Andrews, Prairie Point, Gramercy Place, Prairie Brook, Nottingham Downs Duplex, Crestwood Village, Indian Creek Park Estates, Milhaven, Chateau, Montrachet, Foxborough, and Deer Valley. The useful query family is `site:*.com/hmsft-doc`, `site:*.org/hmsft-doc`, `site:*.hoamsoft.com/hmsft-doc`, and `site:pmtechsol.sfo2.cdn.digitaloceanspaces.com/hmsft-documents` with `Kansas` plus `Declaration`, `Bylaws`, `Covenants`, or `Restrictions`.
- County-by-county searching is helpful for focus, but lower-density generic county sweeps are noisy unless paired with source/legal phrases. Recent strict county passes found useful hand-selected documents for Mill Creek Meadows, Westwood Hills Townhomes, Bella Sera at the Preserve, West Glen, and Falcon Lakes; Riley, Butler, and broad Miami/Leavenworth results were mostly duplicates, legal noise, real estate pages, minutes, or public planning documents.
- GoDaddy download URLs and HOA Express-style `/file/document-page/` URLs remain useful in small doses. Recent source-family searches added or enriched Battle Creek, Canyon Creek Villas, Edgewood, Arlington Estates, and Holly Ridge. Search results are low-volume but high-signal when constrained by `Kansas`, county names, and formal document phrases.
- Deep legal-phrase searches remain productive when manually selected. The latest pass used 4 pages per query over `ks_statewide_legal_phrase_2_queries.txt` and added or enriched Berkshire Villas, Greystone Estates South, Dover Estates, Pepper Tree Park, Villas of Asbury, Timber Creek Estates, Willo-Esque, Clearwater Creek, Symphony Hills, Eagles Landing, Cottages at Woodridge, Lee Mill Village, Auburn Hills 13th, Canyon Lakes, Villas of St. Andrews, Mesa Verde, Harwycke, and Meadows Place.
- A second manual pass over the same deep legal results added or enriched Highlands Creek, Village at Deer Creek, Tuscany Reserve, Fairway Hills, Greens of Chapel Creek, Boulder Creek Villas, Sycamore Village, Chapel Hill, and Blue Valley Riding. Many were existing communities, so this increased PDF count more than manifest count.
- Source-family expansion on BuilderCloud/S3, WebsiteFiles CDN, real-estate CDN PDFs, McCurdy files, and Rackcdn added another Johnson/Sedgwick-heavy wave: Parkside Reserve, Arbor Woods, Stonebridge Park, Cottages at Dayton Creek, East Village Villas, Southern Meadows, Regents Park Villas, Dayton Creek, Wolf Creek, Century Farms, Ashbriar Estates, Springdale Lakes, Amarado Estates, and Cedar Brook. Oakleaf Ridge and Farm at Garnet Hill created manifests but their PDFs were skipped by state verification.
- A formal legal-phrase pass over underrepresented counties was mostly dry: only Lee Mill Village and Springdale Lakes appeared, both already banked. Smaller counties likely need source-specific discovery or local HOA/domain discovery, not just county names with `Kansas non-profit corporation`.
- HOA Express page-searches for `/p/Legal-Documents`, `/p/Governing-Documents`, and `/p/Documents` were mostly dry. Some public pages are indexable but do not expose PDF URLs in static HTML, so they are lower priority than direct `/file/document-page/` URL searches.
- WordPress/static upload searches are productive but noisy. The latest pass added Windmill Creek, Tyler's Landing, The Moorings 10th, and Oak Cliff. It also surfaced many duplicates and out-of-state hits; inspect PDF text for state before banking.
- GoGladly source search is low-volume but still useful. It added Southcrest Addition and a cleaner Douglas County Meadows Place declaration. Skip Longwood reminder/history PDFs and estate-sale forms even when they mention Kansas.
- Broad Kansas-owned/WordPress domain searching added Willow Springs and Prairie Lake Estates, but most results were duplicates or non-governing PDFs. Use it only after source-family searches and inspect page text for out-of-state or newsletter/minutes hits.
- A deeper hmsft/pmtech pass is still worthwhile, but it is now mostly enriching existing communities rather than adding many new manifests. The latest selected pass added or enriched Gramercy Park, Villas at Southpointe, Prairie Point, Preserve at Clear Creek, Foxwood Ranch, Deer Valley, Seven Hills, Copper Creek, Willowbrooke Villas, Melrose Reserve, Southwood, Foxborough, Montrachet, Nottington Creek, Nottingham Downs Duplex, London Park, and Persimmon Pointe. Some `hmsft-doc` URLs on community domains can show as PDF results in search but return 403 from the fetcher; do not force them unless a direct storage URL is available.
- A Sedgwick owned-site pass added or enriched Cambridge-Lexington, Evergreen, Casa Bella, Reflection Ridge, Wyndham Creek, and Forest Oaks. Important correction: do not run broad `probe(lead)` on document-rich HOA websites unless you are ready to clean the result, because the current harvester banks every linked PDF, including newsletters, minutes, forms, budgets, and pool/rental documents. Prefer preflighting links and passing only whitelisted direct PDF URLs for bylaws, declarations, covenants, articles, restrictions, rules, regulations, amendments, and architectural guidelines. This pass was cleaned in GCS after overharvesting.
- A safer whitelisted owned-site pass over county lead leftovers added or enriched Westwood Hills, Woods on 19th, West Glen, Wyndham Hill, and Timber Creek III. The count increase was modest because several were already present, but the method is good: preflight candidate sites, whitelist direct governing PDFs, then run direct-only probes. Falcon Lakes exposed many high-value direct PDFs, but its `editor_upload` path is robots-disallowed, so leave it skipped.
- A follow-up whitelisted pass added or enriched Tatarrax Hills, Parkway Village, and Ginger Creek. This reinforces that county-by-county owned-site mining is now better than broad search: many useful PDFs are on ordinary HOA websites, but the page must be link-preflighted so only governing PDFs are sent to the bank.
- Public ZIP archives can be high-yield when clearly labeled. Pheasant Run Condominiums exposed `DeclarationBylaws.zip`; after verifying robots and inspecting the archive in `/tmp`, bank only the amendment, articles, bylaws, and declaration PDFs, skipping management agreements and summaries. This added 21 PDFs in one clean pass.
- Owned-domain search with governing-document phrases added or enriched Nottingham Downs Homes, Ridge at Pinewood, Irvine Acres, Moorings First, and Pepper Tree Park. Shannon Valley, Lancaster, and Kennett Place exposed good-looking PDF links but their `editor_upload` paths are robots-disallowed, so leave them skipped unless another allowed source appears.
- Cobalt-managed HOA pages are a strong source family. Direct Cobalt PDFs added or enriched Westbank Townhomes, Nelsons Ridge, Vanesta, Williamsburg Townhome, and Grand Mere; Kimball links are now live and banked. Continue with `site:cobaltreks.com/hoa/` and `site:cobaltreks.com/wp-content/uploads filetype:pdf` searches for Riley/Pottawatomie-style communities.
- The Cobalt HOA index at `https://cobaltreks.com/hoa-management/` is better than search alone. The latest direct index pass added or enriched Brittnay Ridge, Brianna Court, Grand Mere, Vanesta, Irvine Acres, Kimball, Nelsons Ridge, Solheim, Westbank, and Williamsburg. Rosewalk and Westwood Village created manifests but did not produce bankable PDFs in this pass. Continue by enumerating manager indexes/sitemaps wherever a source family proves productive.
- The no-Gemini hmsft/PMTech pass needs manual name repair for CDN PDFs. A raw hmsft run was stopped after noisy names; bad manifests were removed from GCS, then valid PDFs were rebanked under clean names. Clean hmsft additions/enrichments from this pass include Woodbridge, Willowbrooke Villas, Copper Creek, Melrose Reserve, Seven Hills, Montrachet, Foxborough, Southwood, Deer Valley, Gramercy Park, Villas at Southpointe, Preserve at Clear Creek, Indian Creek Park Estates, Villas of St. Andrews, Crestwood Village, Nottingham Downs Duplex, Prairie Point, Prairie Brook, and Milhaven.
- The latest no-Gemini owned-domain PDF pass raised KS to 607 manifests and 1,431 PDFs. Manually selected governing-document PDFs added or enriched Amber Meadows, Primrose Addition, Prairie Creek Sixth, Timber Creek Estates, Willow Springs, Foxfire Addition, Clearwater Creek, Falcon Ridge Estates, Ryan's Run, Hallbrook East Village, Greystone Estates South, Oak Valley, Falcon Ridge, Prairie Lake Estates, Cobalt Reks, Reflection Ridge, Cedar Ridge, The Moorings 10th Addition, Mesa Verde, Nelsons Ridge, Milburn Fields, Crescent Lakes, and Woodland Park. Skipped minutes, planning/court packets, estate forms, out-of-state PDFs, and non-governing annual/board materials.
- County/city-owned direct-PDF search is productive when it uses city names plus formal document terms. The latest pass used queries like `filetype:pdf "Overland Park" "Homes Association" "Declaration"` and raised KS to 633 manifests and 1,460 PDFs after manual selection. New/enriched communities included Willow Brooke, Seven Hills Lake, Arlington Estates, St. Andrews Place, Wycliff, Villas of Tamarind, Dover Estates, Chapel Hill, Symphony Hills, Mill Creek Meadows, Red Oak Hills, Oakmont, Villas of Falcon Valley, Normandy Place, Brookhill, Berkshire Villas, Tomahawk Creek, Evergreen, Amarado Estates, West View, Southwest Vistas, Whitetail, Grand Reserve of Mission Ranch, Heather Glen, Woodland Trails, Verona Gardens, Greenbrier of Leawood, Willow Farm, Gardner Ridge, Willow Creek, Auburn Hills 13, Shadow Rock, Sterling East, Grand Mere, and The Willows. This is a better late-stage pattern than broad generic county sweeps.
- Secondary county/city direct-PDF queries are lower-yield but still useful when manually selected. The latest lower-density pass raised KS to 638 manifests and 1,468 PDFs after cleanup. It added or enriched Pepper Tree Park, Ant Hills, Northbrook, Morningview, Grand Mere Vanesta, Tomahawk Valley, Falcon Lakes, and Southwood. Pinehurst Villas and Lake Kahola were validation skips and their empty manifests were removed. eNeighbors public-document page URLs did not harvest PDFs when probed as pages, so prefer direct downloadable URLs or known eNeighbors source logic.
- Re-mining existing result sets is worthwhile and costs no Serper/OpenRouter calls. A leftover pass over `ks_serper_docpages_articles_amendments_2p_1` raised KS to 643 manifests and 1,478 PDFs by adding or enriching Villages of Carmel, Hawthorne Valley, Villas of Falcon Valley, Amber Meadows, Equestrian Estates, Villas of Tamarind, Southwood, Villas of Asbury, Gleason Glen, West Ridge Estates, West Glen, Red Oak Hills, Maple Crest, and Holly Ridge.
- Re-mining the CDN/source-family result set raised KS to 655 manifests and 1,494 PDFs. Clear-name leftovers added or enriched Canyon Lakes, Dayton Creek Cottages, Woodland Ridge, Southern Meadows, Regents Park Villas, Dayton Creek, Boulder Hills, Wolf Creek, East Village Villas, Boulder Creek Villas, Century Farms, Park Glen Estates, Amarado Estates, and Cedar Brook. Ambiguous scanned S3 PDFs were skipped when first-page text did not reveal the HOA name; CenterPoint Preserve failed validation and its empty manifest was removed.
- Re-mining `ks_serper_docpages_direct_pdf_highsignal_1` raised KS to 666 manifests and 1,506 PDFs. Clean direct PDFs added or enriched Harwycke, Willo-Esque, Evergreen, Tyler's Landing, Villages of Southport, Price Hills, Copper Creek, Creekside, Wyndham Heights, Comotara, Eagles Landing South, Estates of Highland Ridge, Parkwood Hills, Holly Ridge, and Foxfield Village. Oak Park still has some validation skips, but the manifest already contains documents, so it was not removed.
- Re-mining `ks_serper_docpages_statewide_legal_phrase_deep_3` raised KS to 671 manifests and 1,516 PDFs. Selected leftovers added or enriched Seven Hills Lake, Amber Meadows, Oak Hill, Walnut Creek Estates, Symphony Hills, Foxborough, Deer Valley, Highland Pointe, Blue Valley Riding, Kensington Valley, Greens of Chapel Creek, Shadow Rock, and Nelsons Ridge.
- A focused HA-KC direct-file pass raised KS to 674 manifests and 1,519 PDFs. It added or enriched Brooke Meadows, Prairie Village Homes Association, and Verona Hills. HA-KC remains low-volume: unlabeled scanned restriction files should be skipped unless the first page or filename reveals a clean HOA name.
- A small cleanup pass over `ks_serper_docpages_statewide_legal_phrase_2_2p_1` raised KS to 678 manifests and 1,524 PDFs. Selected leftovers added or enriched Deer Valley, Grand Ridge at Grand Mere, Preserve at Clear Creek, Rockwood, Timber Creek III, and Nelsons Ridge.
- A focused `site:*/file/document` search raised KS to 683 manifests and 1,538 PDFs. This pattern is better than `file/document-page` alone because many HOA Express-style sites expose direct PDF routes. Selected documents added or enriched St. Andrews Place, Pheasant Run, Wolf Creek, Brookhill, Willow Ridge, Willo-Esque, Maple Dunes, Southern Village, Highcroft, Quivira Falls, Heather Glen, Edgewood, Arlington Estates, and Gramercy Park. Continue to skip annual meetings, newsletters, review/social docs, and city packets.
- A CommunitySite-specific direct-file pass raised KS to 684 manifests and 1,539 PDFs by adding Brooks Farm. The source is low-volume; most remaining CommunitySite hits were dues notices, meeting notices, ARC request forms, or law/reference PDFs.
- A GoGladly leftovers pass raised KS to 685 manifests and 1,541 PDFs by adding or enriching Meadows Place and Southcrest Addition. Continue to skip Longwood reminders/history and Hawthorne Place estate-sale forms.
- A cheap DeepSeek/OpenRouter Wyandotte county-query call used 434 total tokens and produced a focused query file. The resulting deterministic Serper/manual pass raised KS to 686 manifests and 1,543 PDFs by adding Westlake and Persimmon Hill. The planner was cheap and auditable, but the output was generic; the value came from deterministic preflight and direct-PDF selection, not model browsing.
- A Wix/UGD pass raised KS to 687 manifests and 1,545 PDFs by adding Autumn Glen Owners Association in Newton. Wix pages often hide direct `filesusr.com/ugd/...pdf` links behind HTML subpages; preflight the subpage and extract direct PDFs. Skip scanned candidates if the state/community cannot be verified from text or surrounding page context.
- A Squarespace upload pass raised KS to 688 manifests and 1,546 PDFs by adding Northridge Estates. Broad `site:*.squarespace.com/s/` searches are low-yield and pick up newspaper/out-of-state noise; use only after higher-yield source families.
- A direct `filesusr.com/ugd` search found no candidates, but a Webydo/files8 source pass raised KS to 689 manifests and 1,549 PDFs by adding/enriching Falcon Lakes and Boulders at Falcon Lakes. Files8 is low-volume but high-signal when a known community/source family appears.
- A DeepSeek source-family planning call used 4,275 tokens and mostly recommended already-known families, so it was less valuable than deterministic evidence. A follow-up management/CDN direct-PDF pass raised KS to 691 manifests and 1,552 PDFs by adding or enriching Tomahawk Creek, Sylvan Lake, North Lakes, Nottingham Downs Duplex, Willow Ridge, and Highland Ridge. The useful part of this branch was direct URL inspection, not generic model strategy.
- PayHOA and ManageCasa source-specific searches only rediscovered Sylvan Lake and Comotara. HOAEdge added one Edgewood PDF, raising KS to 691 manifests and 1,553 PDFs. These source-specific searches are useful for cleanup but are probably exhausted after one focused pass.
- Expanding terminology from `homes/homeowners association` to `owners association`, `property owners association`, `villa owners`, `townhome owners`, and `condominium owners` was high-yield. This pass raised KS to 697 manifests and 1,562 PDFs by adding or enriching Cedar Creek, Woodland Park, Mill Creek Meadows, Montrachet, Allens Lake, Westbank Townhomes, Tanglewood Lake, Monroe Manor, Solheim, Spring Creek Meadows, Westwind Estates, and Villas of Asbury. Carry this terminology expansion to other states.
- `Architectural Guidelines`, `Design Guidelines`, and `Rules and Regulations` terminology is useful for enrichment but noisier. The latest pass raised KS to 698 manifests and 1,570 PDFs by adding or enriching Villas of Falcon Valley, Brookhill, Oak Hill, Grand Mere, Sugar Valley Lakes, St. Andrews Place, Tanglewood Lake, and Parkwood Hills. Skip newsletters, pool forms, clubhouse rental forms, generic law PDFs, city packets, and out-of-state rules manuals.
- `Community Association` / `Master Association` terminology was lower-yield but added one net PDF, raising KS to 698 manifests and 1,571 PDFs via Villas at Southpointe and Quivira Falls enrichment. Most results were eNeighbors pages, generic law/reference PDFs, city packets, or already-banked Cobalt/WebsiteFiles docs.
- `CC&R` / `CCRs` terminology is noisy but worth one pass. It raised KS to 703 manifests and 1,577 PDFs by adding or enriching Tyler's Landing, Lake Chaparral, Stone Canyon, Four Colonies, Lake Elbo, and Station 1. Skip case law, newsletters, sales disclosures, generic manuals, and out-of-state CC&R packets.

Lower-yield or avoid:

- HA-KC as currently probed: many manifests, almost no PDFs.
- Saline/Reno broad searches: mostly legal, agenda, archive, or SEO noise.
- Lower-density generic county sweeps without a host/source/legal phrase: mostly noise. Use county names to focus, but combine them with `filetype:pdf`, `Register of Deeds`, `Homes Association Declaration`, `hmsft-doc`, `eneighbors`, `hoa-express`, or known HOA-owned domains.
- Directory hosts like `homeownersassociationdirectory`, `communitypay`, `hoa-community`, `zoominfo`.
- Broad statewide validation unless raw hosts look clearly HOA-owned.

## Useful Commands

Counts:

```bash
gsutil ls 'gs://hoaproxy-bank/v1/KS/**/manifest.json' 2>/dev/null | wc -l
gsutil ls 'gs://hoaproxy-bank/v1/KS/*/*/doc-*/original.pdf' 2>/dev/null | wc -l
```

Credit check:

```bash
set -a; source settings.env; set +a
curl -s https://openrouter.ai/api/v1/credits \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | python3 -m json.tool
```

Process check:

```bash
ps -fA | rg 'hoaware.discovery|run_ks_openrouter_discovery|scrape_ks_serper|openrouter_ks_planner'
```

## Next Good Branches

- More source-specific searches for hmsft/HOAMsoft, HOA Express `/file/document-page/`, GoDaddy `img1.wsimg.com/blobby/go/.../downloads`, eNeighbors public documents, and county recorder/legal phrases.
- More independent-domain searches for remaining Kansas metros/counties, but inspect host distribution before validation.
- Smaller city-specific passes for places with actual HOA-owned domains:
  - Johnson County suburbs not fully exhausted.
  - Sedgwick/Wichita variations.
  - Douglas/Lawrence produced very high PDF yield from Westwood Hills.
  - Riley/Manhattan produced Nelson's Ridge and Parkway Village.
- Use manual deterministic selection when raw host list has only a few obvious HOA-owned domains; skip OpenRouter in that case.
- Next good branch: deterministic source-specific searches are still safer than more OpenRouter. OpenRouter usage is about `$9.61 / $10`, possibly including unrelated benchmark activity. Do not spend model budget casually. Good next pivots are manually selected leftovers from `benchmark/results/ks_serper_docpages_statewide_legal_phrase_deep_3/leads.jsonl`, then more county-constrained legal phrases and source-specific hmsft/HOAMsoft, GoDaddy-download, and HOA Express searches. Avoid newsletters, meeting minutes, forms, out-of-state hits, and generic `homesassociation.org` records unless the specific HOA identity is clear.

## Autonomy Reminder

The turn boundary is not a blocker. If no real blocker exists, keep launching the next concrete scrape/probe/validation step and use commentary updates. Do not send a final answer just to summarize progress.

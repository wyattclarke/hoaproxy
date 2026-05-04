# Kansas Discovery Handoff

Updated: 2026-05-04

User instruction: continue autonomously for KS. Do not stop at checkpoints. Commit or hand off as needed, then immediately keep scraping. Only final-answer if blocked, out of budget, or asked for status.

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/KS/`
- Current count: 478 manifests, 1,183 PDFs
- OpenRouter credits: `$7.14 / $10` used, about `$2.86` remaining
- Active KS work: none at last process check.
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

Lower-yield or avoid:

- HA-KC as currently probed: many manifests, almost no PDFs.
- Saline/Reno broad searches: mostly legal, agenda, archive, or SEO noise.
- Directory hosts like `homeownersassociationdirectory`, `communitypay`, `hoa-community`, `zoominfo`.
- Broad statewide validation unless raw hosts look clearly HOA-owned.

## Useful Commands

Counts:

```bash
gsutil ls 'gs://hoaproxy-bank/v1/KS/**/manifest.json' 2>/dev/null | wc -l
gsutil ls -r 'gs://hoaproxy-bank/v1/KS/' 2>/dev/null | grep '/original.pdf$' | wc -l
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

- More independent-domain searches for remaining Kansas metros/counties, but inspect host distribution before validation.
- Smaller city-specific passes for places with actual HOA-owned domains:
  - Johnson County suburbs not fully exhausted.
  - Sedgwick/Wichita variations.
  - Douglas/Lawrence produced very high PDF yield from Westwood Hills.
  - Riley/Manhattan produced Nelson's Ridge and Parkway Village.
- Use manual deterministic selection when raw host list has only a few obvious HOA-owned domains; skip OpenRouter in that case.
- Next good branch: deterministic source-specific searches are still safer than more OpenRouter. OpenRouter usage was about `$8.33 / $10` at the last check, possibly including unrelated benchmark activity. Good next pivots are underrepresented county phrase searches and targeted source hosts found from successful probes. Avoid newsletters, forms, out-of-state hits, and generic `homesassociation.org` records unless the specific HOA identity is clear.

## Autonomy Reminder

The turn boundary is not a blocker. If no real blocker exists, keep launching the next concrete scrape/probe/validation step and use commentary updates. Do not send a final answer just to summarize progress.

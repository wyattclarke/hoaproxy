# Kansas Scraper Utilities

Kansas-specific scrape repair and enrichment scripts live here. Generated
outputs belong in `state_scrapers/ks/results/` and are ignored by git.

## OCR Location Enrichment

`scripts/enrich_live_locations_from_ocr.py` improves live KS map coverage from
already-ingested OCR text:

1. Reads live KS HOA summaries, map points, and document OCR pages from
   `https://hoaproxy.org`.
2. Skips HOAs that are already mapped unless `--include-mapped` is passed.
3. Extracts HOA aliases, city/county mentions, Kansas ZIPs, and state evidence
   from searchable document text.
4. Optionally queries OSM/Nominatim for Kansas subdivision or neighborhood
   polygons, using a local cache and conservative Kansas bounding-box checks.
5. Falls back to Census ZCTA centroids when repeated OCR ZIP evidence exists.
6. Defaults to dry-run. Pass `--apply` to post accepted records to
   `/admin/backfill-locations`.

Useful runs:

```bash
# Low-cost pass: no public geocoder calls, only OCR ZIP evidence.
.venv/bin/python state_scrapers/ks/scripts/enrich_live_locations_from_ocr.py \
  --skip-nominatim \
  --output state_scrapers/ks/results/live_location_ocr_zip_enrichment.json

# Polygon pass: cached/slow Nominatim lookups from OCR city/county/alias clues.
.venv/bin/python state_scrapers/ks/scripts/enrich_live_locations_from_ocr.py \
  --nominatim-delay-s 2.0 \
  --output state_scrapers/ks/results/live_location_ocr_polygon_enrichment.json
```

For live writes, provide one of:

- `HOAPROXY_ADMIN_BEARER`
- Render credentials (`RENDER_API_KEY` and `RENDER_SERVICE_ID`) so the script can
  read the live `JWT_SECRET`
- `JWT_SECRET` for a matching local/admin environment

## Serper Places Location Enrichment

`scripts/enrich_live_locations_from_serper_places.py` is the higher-yield
cleanup pass for HOAs whose OCR does not expose repeated ZIP evidence. It:

1. Reads currently unmapped live KS HOAs.
2. Queries Serper Places with the compact HOA/subdivision name and city hints.
3. Accepts only candidates with Kansas address evidence, Kansas-bounded
   coordinates, and strong name overlap.
4. Emits `address` quality when a street-like address is present and
   `place_centroid` quality for subdivision/neighborhood/place centroids.

Useful run:

```bash
.venv/bin/python state_scrapers/ks/scripts/enrich_live_locations_from_serper_places.py \
  --output state_scrapers/ks/results/live_location_serper_places.json
```

Apply only after the live app supports `place_centroid` in
`/admin/backfill-locations` and `/hoas/map-points`:

```bash
.venv/bin/python state_scrapers/ks/scripts/enrich_live_locations_from_serper_places.py \
  --output state_scrapers/ks/results/live_location_serper_places.json \
  --apply
```

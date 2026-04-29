# SEO Roadmap

Living document for traffic-growth work on hoaproxy.org. Created in response to a GA4 chatbot suggestion list; this version reframes those suggestions against actual site state and adds the bigger issues the chatbot missed. Update items as they ship; keep this in sync with reality so future-you doesn't redo the audit.

## Why this exists

GA4 reported very thin organic traffic — individual HOA pages getting 1–2 organic visits/month, ~73% of all traffic labeled "direct," homepage bounce rate 58%. The catalog is much bigger than that traffic implies (8,325 HOAs in TX alone, ~1,850 city index pages in the sitemap as of 2026-04-27). The traffic floor is what you'd expect when most URLs render as duplicate templates with thin static content and there's no PageRank flow from `/` into the directory.

Two of GA4's four suggestions were already done (long-tail title structure, regional landing pages exist as `/hoa/{state}/` and `/hoa/{state}/{city}/`). The remaining levers — and the ones the chatbot missed — are below, ranked by ROI.

## Current state (as of 2026-04-27)

- **Catalog size:** 8,325 HOAs in TX; 7 active states in the sitemap (AZ, CA, CO, NC, SC, TX, VA); ~1,850 URLs in `/sitemap.xml`. Note: with 8K+ HOAs in TX alone, the sitemap appears to under-emit individual HOA URLs — likely a `slugify_*` filter dropping rows. Audit needed (item 5).
- **HOA profile pages:** Title is `{HOA Name} | {City}, {State} | HOAproxy` (good). H1 currently `<h2 id="hoaTitle">` (needs promotion; the only H1 is the brand mark in the header). Body is JS-hydrated SPA — Googlebot sees no documents, no address, no city/state in body prose. To a crawler, 8K+ HOA pages look like the same template with a different name. Textbook "Crawled, currently not indexed" risk.
- **Homepage `/`:** Hero with H1 "Find your HOA. Read the rules. Vote without showing up." plus search bar. State filter pills at `index.html:833` are JS-loaded buttons, not anchor links — invisible to Googlebot, no PageRank flows from `/` into `/hoa/{state}/`. Single horizontal row layout that won't scale past ~10 states.
- **State/city index pages:** Title + "X HOAs across Y cities" + bare bullet list. No intro copy, no metro grouping, no featured-HOA picks. Nothing for Google to rank on except the title.
- **Google Search Console:** **Not verified.** Until it is, every diagnosis above is guessing — we have no visibility into actual indexing, queries, or thin-content flags.
- **Open Graph tags:** None anywhere. Social shares preview as plain text.

## Roadmap

### 1. Verify Google Search Console (1 hour, unblocks everything)

Cheapest, highest-leverage. Until GSC is verified, items 2–5 are flying blind.

**Recommended approach:** **DNS TXT record** — open Search Console → Add Property → "Domain" → copy the TXT value → add at the registrar/DNS provider for `hoaproxy.org`. Zero code. Survives template refactors. Covers all subdomains.

**Alternative if you can't or don't want DNS:** meta-tag method. Add `<meta name="google-site-verification" content="{token}">` to:
- `api/static/index.html:6`
- `api/static/hoa.html:6`
- The inline templates in `hoa_state_index()` (`api/main.py:2358`) and `hoa_city_index()` (`api/main.py:2309`)

A `_render_seo_head()` helper would dedupe these.

**After verification:**
- Submit `https://hoaproxy.org/sitemap.xml` from inside GSC.
- Wait 2–3 days, then check Coverage report. Expect to see how many URLs are indexed vs. discovered/not-indexed/excluded — that's the real diagnostic for items 2 and 5.

---

### 2. Make HOA profile pages indexable (the biggest unlock)

**Problem:** body text is JS-hydrated. Across 8K+ near-identical templates, Google has no signal that any individual page is worth indexing.

**Fix — server-render a unique content block above the JS app.** In `_render_hoa_page()` (`api/main.py:2022`), inject a static `<section class="hoa-overview">` containing:

- Promote the page heading to `<h1>` (currently `<h2 id="hoaTitle">` at `api/static/hoa.html:395`). The brand mark is currently the page's H1 (`api/static/hoa.html:390`); demote it to a `<div>` or `<a>` since the HOA name is the actual page topic.
- One sentence: `"{HOA Name} is a homeowners association in {City}, {State}{, {County} county if known}."`
- If `street`/`postal_code` are present in `hoa_locations`: the formatted mailing address.
- A document inventory line: `"HOAproxy has {N} document(s) on file: {breakdown by category}"`. Categories live in `documents.category` and `VALID_CATEGORIES` is defined in `hoaware/doc_classifier.py:47` (`ccr`, `bylaws`, `articles`, `rules`, `amendment`, `resolution`, `minutes`, `financial`, `insurance`).
- "Last updated" line from `MAX(documents.last_ingested)` for that HOA.
- 2–3 short FAQ items (rendered as static HTML *and* `FAQPage` JSON-LD): "What governing documents does {HOA} have on file?", "How do I file a proxy vote for {HOA}?", "Where is {HOA} located?"

This adds ~150–250 unique words per page from existing DB fields, no new data collection. Crosses most "thin content" thresholds.

**Also add `BreadcrumbList` JSON-LD sitewide** (HOAproxy → State → City → HOA). Cheap; commonly produces breadcrumb display in SERPs which boosts CTR.

**Files:**
- `api/main.py` — `_render_hoa_page()` and `_load_hoa_template()` around lines 2017–2086.
- `api/static/hoa.html` — heading restructure (lines 388–399).
- `hoaware/db.py` — new helper `get_hoa_overview(conn, hoa_id) -> {street, city, state, postal_code, doc_categories: dict[str,int], last_updated}`.

---

### 3. Reimagine the homepage state-pill row

**What's there now:** `api/static/index.html:833` has `<div id="stateFilter" class="state-filter-row"></div>`, populated by JS (`api/static/index.html:1307`) from `/hoas/states`. Each pill is a `<button data-state="...">` that filters the directory below.

**Three problems with the current setup:**

1. **Invisible to Googlebot.** Pills are JS-rendered, so the homepage has zero static `<a>` links to `/hoa/{state}/`. Crawl gravity flows through the sitemap only.
2. **Pills are filter buttons, not links.** Even with JS, clicking filters in place — doesn't navigate to the much richer `/hoa/{state}/` index page where the rankable content lives.
3. **Layout doesn't scale.** Single horizontal `state-filter-row`. Fine at 7 states, ugly at 20, broken at 50.

**Fix — three changes in one place:**

- **SSR the pills.** Convert `index()` from a static `FileResponse` (`api/main.py:1981`) into a small SSR like `hoa_state_index()` already is. Inject the state list into `<div id="stateFilter">` server-side, with a 5-min in-memory cache keyed off the state list (cheap, infrequent invalidation).
- **Make pills real links.** Each pill becomes `<a href="/hoa/{state}/" class="state-pill" data-state="{state}">{State} <span>{count}</span></a>`. Existing JS click handler stays but adds `event.preventDefault()` so users still get the in-place filter UX. Bots and middle-click users get the link.
- **Replace flat row with a regional grid.** Group by Census region (Northeast / South / Midwest / West) in a CSS grid — caps visual width, accommodates 50 states without becoming a wall.

Side benefit: addresses the homepage bounce-rate concern by giving users a real next-click.

**Files:**
- `api/main.py:1981` — `index()` becomes SSR.
- `api/static/index.html:833` — wrapper changes from flat row to regional grid.
- `api/static/index.html:1307–1325` — rendering changes; click handler adds `preventDefault`.
- `hoaware/db.py` — possibly reuse the query backing `/hoas/states`.

---

### 4. Make state and city index pages worth ranking

**Problem:** `/hoa/tx/` is `<h1>HOAs in Texas</h1>` + `8325 homeowners associations across 595 cities` + bullet list of 595 cities. That's all. Nothing for Google to rank on except the title; nothing to keep a user on the page.

**Per state page:**
- 150–250-word intro paragraph: which state HOA statutes apply (e.g., TPC §209 for TX), what governing documents are typically public, why someone might be looking. Reuse data from `hoaware/law.py` and `legal_corpus/`.
- Group cities by metro (DFW, Houston Metro, San Antonio Metro, Austin Metro for TX) instead of one flat alphabetical list. Creates internal-link clusters and matches metro-area queries. Low-priority states can keep flat list.
- "Top 10 HOAs in {State} by document coverage" — picks profiles with the richest content; gives crawl gravity to the strongest pages.
- `CollectionPage` JSON-LD wrapping the list.

**Per city page:**
- Short intro: `"There are {N} homeowners associations in {City}, {State}. Browse governing documents, file proxy votes, or search across community rules."`
- Optional county field if available.
- "Top HOAs in {City}" section ahead of the alphabetical list.

**Files:**
- `api/main.py:2341` — `hoa_state_index()` template.
- `api/main.py:2286` — `hoa_city_index()` template.
- New static state→metro→cities mapping (data file or constant) for the top 4 states.
- `hoaware/law.py` — read-only reuse for state intros.

---

### 5. Sitemap hygiene

`/sitemap.xml` reports ~1,850 URLs total but TX alone has 8,325 HOAs. Either individual HOA URLs are being filtered out or the file is truncating. `db.list_hoas_for_sitemap()` (`hoaware/db.py:955`) is the place to look.

**Fixes:**
- Audit `list_hoas_for_sitemap()` — verify all HOAs with non-empty `state` + `city` emit URLs. Likely culprit: `slugify_name`/`slugify_city` returning empty for rows with unusual characters.
- Add `<lastmod>` per URL, sourced from `MAX(documents.last_ingested)` for HOA URLs and the latest descendant update for index pages. Real lastmod measurably improves crawl prioritization.
- If post-fix total exceeds ~10K URLs, split into per-state sitemaps with a sitemap index at `/sitemap.xml`. Per-state shards are easier to debug in GSC's Coverage report.

**Files:** `api/main.py:1892–1979`, `hoaware/db.py:955`.

---

### 6. Open Graph + Twitter Card tags (small, cheap)

No `og:*` or `twitter:*` tags anywhere currently. When someone shares an HOA URL in a Reddit thread, Facebook group, or text message, the preview is bare. Doesn't move SEO directly but improves social-referral CTR — and HOA-dispute posts on Reddit/Facebook are a real referral channel for this kind of site.

**Implementation:** Same `_render_seo_head()` helper introduced in item 1 emits `og:title`, `og:description`, `og:url`, `og:type=website`, `og:image` (generic branded card for now), plus `twitter:card`/`twitter:title`/`twitter:description`/`twitter:image`.

---

## Explicit non-goals

- **Per-HOA generated OG images.** Cost/complexity outweighs benefit until items 1–4 are landed.
- **"Fixing" homepage bounce rate as a primary metric.** For a directory, "bounce rate" is misleading; engaged-session rate on profile pages is the right metric. Item 3 fixes the underlying issue (no entry points) and bounce rate follows.
- **Pseudo-content like "[HOA Name] reviews" or "[HOA Name] management contact"** as the GA4 chatbot suggested. The site can't actually answer those queries — targeting them creates user-disappointment signals that hurt rankings.
- **Full SSR migration.** The JS app is fine for engaged users; we just need an SSR'd content shell so crawlers see something distinct.

## Verification checklist

After each item ships, verify before moving on. **Wait 2–4 weeks after items 1–4 are deployed** before judging impact in GSC — Google's re-indexing cycle is slow.

1. **GSC verified** → property shows green check in Search Console; sitemap submitted; Coverage report appears within 2–3 days.
2. **HOA profile SSR overview** → `curl https://hoaproxy.org/hoa/tx/san-marcos/blanco-vista-residential-owners-association-inc | grep -i "san marcos"` returns matches in body, not just title. Validate JSON-LD at https://search.google.com/test/rich-results.
3. **Homepage state pills** → `curl https://hoaproxy.org/ | grep -c 'class="state-pill" href="/hoa/'` returns the expected state count. Click a pill in a real browser; in-place filter still works (preventDefault path); middle-click navigates.
4. **State/city index expansions** → `curl https://hoaproxy.org/hoa/tx/` and confirm intro paragraph + metro grouping + featured-HOAs section in raw HTML.
5. **Sitemap** → `curl https://hoaproxy.org/sitemap.xml | grep -c '<url>'` shows roughly the total HOA count + index pages. Each `<url>` has a `<lastmod>`.
6. **OG tags** → paste an HOA URL into https://www.opengraph.xyz/ and confirm preview renders correctly.

**Expected GSC signal 2–4 weeks post-launch:** indexed-page count climbing toward total HOA count; impressions for `[HOAs in {city}]` queries appearing alongside the existing `[HOA Name]` queries; CTR improvement on URLs that gained `BreadcrumbList` rich results.

## Status log

Update as items ship. Format: `YYYY-MM-DD — item N — note`.

- 2026-04-27 — roadmap drafted from GA4 audit + codebase exploration.
- 2026-04-28 — items 2, 3, 4, 5, 6 implemented. Item 1 (GSC verification) is the only remaining piece and is a user action: add the DNS TXT record from Search Console at the registrar for hoaproxy.org, then submit `/sitemap.xml` from inside GSC. Notes:
  - Item 2: HOA profile pages now SSR a unique overview block (location sentence, mailing address when known, document inventory by category, last-updated date) plus 3 FAQ items rendered visibly *and* as `FAQPage` JSON-LD. `BreadcrumbList` JSON-LD added sitewide on profile pages. H1 promoted from `<h2 id="hoaTitle">` to `<h1>`; brand mark demoted from H1 to a `<div>` so the page topic owns the H1.
  - Item 3: Homepage `index()` converted from `FileResponse` to SSR. State pills are now crawlable `<a href="/hoa/{state}/">` anchors grouped by Census region (Northeast / South / Midwest / West) instead of JS-loaded buttons. Click handlers preserve the in-place filter UX via `preventDefault()`. 5-min in-memory cache on the state-counts query keeps `/` fast.
  - Item 4: State and city index pages now render an intro paragraph (state-specific for AZ/CA/CO/NC/SC/TX/VA — generic template otherwise), a "Top HOAs by document coverage" featured section, and `BreadcrumbList` + `CollectionPage` JSON-LD. Texas and North Carolina state pages group cities by metro (Houston / DFW / Austin / San Antonio for TX; Charlotte / Triangle / Triad / Coastal for NC). Other states keep the flat alphabetical city list.
  - Item 5: Sitemap audit found ~12,760 URLs already (the earlier ~1,850 estimate was a WebFetch summarization error, not a real gap). `<lastmod>` now emitted on every URL — sourced from `MAX(documents.last_ingested)` per HOA, max-of-descendants for state/city aggregate pages, and today's date for static pages. No per-state sharding yet (12K is well under Google's 50K soft cap).
  - Item 6: OG + Twitter Card tags (`og:type`, `og:url`, `og:title`, `og:description`, `og:image`, `og:site_name`, `twitter:card`, `twitter:title`, `twitter:description`, `twitter:image`) now emitted on home, profile, state, and city pages. Generic branded image for now; per-HOA cards remain explicitly out of scope.
  - Tests: `tests/test_seo_ssr.py` (8 passing) covers SSR overview, JSON-LD blocks, sitemap lastmod, OG tags, state-index intro/metros/featured, and homepage state-pill anchors.

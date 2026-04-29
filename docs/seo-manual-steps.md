# SEO Manual Steps

Companion to `docs/seo-roadmap.md`. The roadmap covers what was implemented in code; this doc covers the manual work — DNS records, GSC clicks, validations — that no commit can do for you. Walk through these after the SEO overhaul deploys.

## 1. Sanity-check the deploy

Once Render shows the latest deploy as `live`, run these one-liners. They confirm the SSR work is actually serving:

```bash
# HOA profile page renders the location sentence in static HTML
curl -s https://hoaproxy.org/hoa/tx/san-marcos/blanco-vista-residential-owners-association-inc \
  | grep -i 'is a homeowners association in San Marcos'

# Homepage SSRs state-pill anchor links — count occurrences, not matching
# lines (the regional grid is on one HTML line, so `grep -c` undercounts).
curl -s https://hoaproxy.org/ \
  | grep -oE 'class="state-pill" href="/hoa/[a-z]+/"' | wc -l
# Should equal the number of active states (7 as of 2026-04-28).

# Sitemap emits <lastmod> on every URL
curl -s https://hoaproxy.org/sitemap.xml | grep -c '<lastmod>'

# State index has metro grouping for TX
curl -s https://hoaproxy.org/hoa/tx/ | grep -i 'Houston Metro'
```

Any of these returning empty means the deploy didn't ship the expected code — check Render logs.

## 2. Verify Google Search Console (item 1 of the roadmap)

The single highest-leverage manual step. Until GSC is verified, there's no visibility into how Google is actually indexing the 12K+ URLs in the sitemap.

**Recommended: DNS TXT record verification.** One-time, covers all subdomains, survives template changes.

1. Open https://search.google.com/search-console
2. Click **Add property** → choose **Domain** (the left-side option, not "URL prefix")
3. Enter `hoaproxy.org`
4. Google displays a TXT value like `google-site-verification=abc123…`
5. Add it at your DNS provider for `hoaproxy.org`:
   - **Type:** TXT
   - **Host / Name:** `@` (root domain — provider may also accept blank)
   - **Value:** the full `google-site-verification=…` string Google gave
   - **TTL:** default (3600 is fine)
6. Wait 5–60 minutes for DNS to propagate. Check with:
   ```bash
   dig TXT hoaproxy.org +short
   ```
   You should see your verification string in the output.
7. Back in GSC, click **Verify**.

If you can't or don't want DNS verification, the alternative is a `<meta name="google-site-verification" content="…">` tag in the page `<head>` — ask Claude to wire that in once you have the token. DNS is the better long-term answer.

## 3. Submit the sitemap

Once the property is verified:

1. GSC sidebar → **Sitemaps**
2. Enter `sitemap.xml`
3. Click **Submit**

Within 2–3 days the **Coverage** report populates. That's where indexing problems will show up — `Discovered, currently not indexed`, `Crawled, currently not indexed`, `Duplicate, Google chose different canonical`, etc.

## 4. Validate structured data (one-time)

Paste an HOA URL into Google's Rich Results Test:

- https://search.google.com/test/rich-results

For an HOA profile page (e.g. `https://hoaproxy.org/hoa/tx/san-marcos/blanco-vista-residential-owners-association-inc`), expect:
- `BreadcrumbList` detected ✓
- `FAQPage` detected ✓
- `Organization` detected ✓
- No errors

For a state index page (e.g. `https://hoaproxy.org/hoa/tx/`), expect:
- `BreadcrumbList` detected ✓
- `CollectionPage` detected ✓

If the tool flags errors, fix before they accumulate in GSC's "Enhancements" reports.

## 5. Validate Open Graph (one-time)

Paste any HOAproxy URL into:

- https://www.opengraph.xyz/

Confirm the preview card shows title + description + a (small, generic) image. The current image is the favicon — fine for now. If/when you want richer previews (per-HOA generated cards), revisit item 6 of the roadmap.

## 6. What to watch over 2–4 weeks

Google's re-indexing cycle is slow. Don't read the tea leaves day-by-day; check in once a week.

In GSC's **Coverage** and **Performance** reports, look for:

- **Indexed-page count climbing** toward the total in the sitemap (~12,760 URLs as of 2026-04-28). Healthy outcome: most URLs move from `Discovered, currently not indexed` to `Indexed`. If they stay in `Discovered` purgatory after 4 weeks, that's a signal item 2's SSR content still isn't unique enough — revisit and extend.
- **New query shapes** in the Performance report — specifically `[HOAs in {city}]` and `[HOAs in {state}]` queries appearing alongside the existing `[specific HOA name]` queries. That's evidence the state/city index pages are starting to rank.
- **CTR improvement** on URLs that gained breadcrumb display in SERPs (from `BreadcrumbList` JSON-LD).
- **The `direct` traffic share dropping** in GA4 as Google starts attributing actual organic traffic. The previous "73% direct" number was largely mis-attribution — once GSC is linked, you'll see how much was really organic all along.

## 7. Keep the roadmap log current

`docs/seo-roadmap.md` has a status log section at the bottom. Add a line whenever something meaningful happens:

- Date GSC verified
- Date sitemap submitted
- First time you see `[HOAs in {city}]` queries in Performance
- Indexed-count milestones (1K, 5K, 10K)
- Any unexpected GSC errors or warnings
- Decisions to extend or rework items

The log is the only durable record of what was actually true at each point in time. Memory and git history don't capture "we tried X and the indexing rate didn't move" — the status log does.

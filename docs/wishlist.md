# HOAware Wish List

Long-term ideas that aren't actively being worked on.

---

## Mobile App

Make HOAware available as a native mobile app on Android and iOS.

**Options (in order of effort):**
1. **PWA** — Add `manifest.json` + service worker to existing site. ~1–2 days. No app store, but installable to home screen on both platforms.
2. **Capacitor wrapper** — Wrap existing HTML/JS in a native shell for App Store + Play Store distribution. ~1–3 weeks.
3. **React Native rewrite** — Full native UI; FastAPI backend unchanged. ~2–4 months.

Key value-adds over the web app: push notifications (proxy status, meeting reminders), offline access, app store discoverability.

---

## Blend Legal Corpus into HOA Doc Q&A

When a user asks a question about their HOA docs, the answer should also draw on their state's legal corpus — not just the HOA's own bylaws. For example, "can I vote by proxy electronically?" is only fully answered by combining the HOA's rules with the state statute.

**What it requires:**
1. Resolve the user's state from their HOA's location record
2. Query `legal_rules` for that state and relevant topic (proxy voting, records access, etc.)
3. Inject those rules as additional context alongside the HOA doc chunks before the LLM call
4. Surface citations from both sources in the response

The HOA doc context comes from Qdrant (semantic search); the legal context comes from SQLite (structured lookup). They'd need to be merged in the prompt. The main risk is adding noise for purely HOA-specific questions (fence colors, parking rules) — worth filtering by topic or letting the LLM sort it out.

Highest value for proxy/voting/records questions; lower value for property/aesthetics questions.

---

## Web Scraping HOA Document Corpus

Many HOAs publish their governing documents (CC&Rs, bylaws, rules) on publicly accessible websites — HOA management portals, county recorder sites, neighborhood association pages, and community forums. Scraping these at scale would seed the document index without requiring residents to upload anything.

**Potential sources:**
- HOA management company portals (FirstService, Associa, CINC, AppFolio, etc.) — many expose document libraries without auth
- County recorder / register of deeds sites — CC&Rs are recorded public documents in most states
- State HOA registries — FL, AZ, NV, CO, and a few others maintain public HOA databases with contact info
- Nextdoor / community Facebook groups — often link to official doc pages
- Google: `site:*.com filetype:pdf "CC&R" OR "bylaws" OR "declaration of covenants"` style queries via Search API

**Approach:**
1. Build a crawler that discovers HOA home pages (start from state registry exports + Google Custom Search)
2. For each HOA site, look for PDF links matching document patterns (bylaws, CC&R, rules & regulations, meeting minutes)
3. Download, deduplicate by hash, and run through the existing ingest pipeline
4. Store provenance (source URL, crawl date) alongside each document

**Considerations:**
- `robots.txt` compliance and rate limiting are essential
- PDFs from county recorders are unambiguously public record; HOA portal docs may have terms-of-service restrictions — legal review needed before large-scale scraping
- Quality signal: prefer documents where the HOA name matches a known registered HOA

---

## Manual Legal Corpus: Missing States

Four states could not be scraped automatically because their legislature sites use JavaScript rendering or CAPTCHAs. Their proxy voting rules need to be added manually.

| State | Blocker | Suggested approach |
|-------|---------|-------------------|
| **OK** | `oscn.net` — Cloudflare Turnstile CAPTCHA | Copy statute text manually from [oscn.net](https://www.oscn.net) or use the Oklahoma Legislature site at [osclegislature.gov](https://www.osclegislature.gov) |
| **PA** | `legis.state.pa.us` — JS-rendered | Copy from [legis.pa.gov](https://www.legis.pa.gov) (Consolidated Statutes, Title 68 for HOA) |
| **SD** | `sdlegislature.gov` — React SPA | Copy from [sdlegislature.gov](https://sdlegislature.gov) (Title 43A, Chapter 44 for HOA) |
| **WY** | Static HTML inaccessible | Copy from [wyoleg.gov](https://www.wyoleg.gov) (Title 34 for HOA) |

**To add a state manually:**
1. Copy the relevant statute text into `legal_corpus/raw/{STATE}/hoa/proxy_voting/manual.txt`
2. Run `python3 scripts/legal/normalize_law_texts.py --state {STATE}`
3. Run `python3 scripts/legal/extract_rules.py --state {STATE} --include-aggregators`
4. Run `python3 scripts/legal/assemble_profiles.py --state {STATE}`
5. Re-export seed files: `python3 scripts/legal/export_seeds.py` (see `scripts/legal/README.md`)

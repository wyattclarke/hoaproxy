# HOAproxy Wish List

Long-term ideas that aren't actively being worked on.

---

## Mobile App

Make HOAproxy available as a native mobile app on Android and iOS.

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

## Meeting & Agenda Model → Directed Proxies

Currently, proxies are undirected — a homeowner grants general voting authority to a delegate without specifying how to vote on individual questions. This is the right default for now, and `proxy_type` is already in the schema to support expansion later.

The full path to directed proxies requires:

**Step 1 — Meeting/agenda model.** Add a lightweight `meetings` table (date, description, HOA ID) and an `agenda_items` table (text description, meeting ID, order). Even free-text agenda items are enough to make directed proxies meaningful — a homeowner can say "on the special assessment item, vote NO."

**Step 2 — Scope proxies to meetings.** Associate each proxy with a specific upcoming meeting rather than floating in the abstract. This also enables automatic expiry (proxy lapses if the meeting passes) and improves the audit trail.

**Step 3 — Re-enable directed proxies.** With agenda items as named entities, a directed proxy can reference a specific item and carry a vote instruction (`yes` / `no` / `abstain`). This also satisfies state statutes (CA, FL, TX, and others) that explicitly distinguish general vs. limited/directed proxies and may require one form or the other depending on the vote type.

**Community Pulse** (see separate wish-list item) is the natural engagement layer: a Pulse item that gains enough support could be promoted to a formal agenda item, closing the loop from member sentiment → official vote → proxy delegation.

---

## Postal Address Verification

To confirm that a registered user actually lives at the HOA address they claim, send a physical postcard to that address containing a short verification code. The user enters the code in the app to complete verification.

**Why it matters:** Proxy voting and resident proposals carry real governance weight. A resident who can't prove they live at an address shouldn't be able to vote on behalf of that unit or submit proposals on its behalf. Postal verification is the gold standard for address proof — it's the same mechanism banks and the IRS use.

**Flow:**
1. After registration (or on-demand from profile), resident submits their unit address
2. System generates a short random code (6–8 alphanumeric), stores it with an expiry (e.g., 21 days), and queues a postcard via a mail API
3. Postcard arrives addressed to "HOAproxy Resident" at the unit address, containing the code and a URL/QR code to verify
4. Resident enters the code; `address_verified_at` timestamp is recorded on their account
5. Unverified residents can browse but are blocked from proxy grant/accept and proposal submission

**Implementation options:**
- **Lob.com** — postcard API, ~$1/card, handles printing + USPS delivery; straightforward REST integration
- **PostGrid** — similar, slightly cheaper at volume
- **DIY** — generate a PDF, hand off to a local print + mail service; works but not automated

**Edge cases to plan for:**
- Resend flow (postcard lost or expired)
- Multi-unit buildings where the address alone is ambiguous — include unit number on the card
- Renters vs. owners — verification confirms address, not ownership; ownership verification is a separate problem (title records)
- HOA admin override — board members should be able to manually verify a resident (e.g., they know the person)

**Good trouble protection:** address verification should not be a gatekeeping tool against legitimate residents. The resend flow must be frictionless, and there should be a clear appeals path if a resident has mail delivery issues (e.g., USPS holdback, new construction).

---

## User Profile Page

Residents should be able to view and edit their own profile instead of only seeing pieces of their account scattered across registration, dashboard, and proxy flows.

**What it should include:**
1. Full name and contact email
2. Claimed HOA memberships and unit numbers
3. Delegate status by HOA
4. Email verification state
5. Future address verification state if postcard verification ships

**Editing actions:**
1. Update full legal/display name
2. Update contact email or start an email-change verification flow
3. Edit unit number on a membership claim
4. Manage delegate bio and contact info from the same place

**Why it matters:** proxy documents, delegate listings, and proposal attribution all depend on accurate user identity data. Right now there is no obvious self-service place to confirm or correct that information.

---

## Google / Apple OAuth Sign-In

Allow residents to sign in with their Google or Apple account instead of (or in addition to) an email/password.

**What it requires:**
1. Make `password_hash` nullable in the `users` table (OAuth users won't have one) — additive migration via `_ensure_table_column`
2. New `user_oauth_providers` table: `(user_id, provider, provider_user_id, created_at)`
3. Add `authlib` to handle the OAuth 2.0 / OIDC dance
4. New routes: `GET /auth/oauth/{provider}` (redirect) and `GET /auth/oauth/{provider}/callback` (exchange code → issue JWT)
5. "Sign in with Google / Apple" buttons on login and register pages
6. Handle email collision: user registered with email+password then tries OAuth with same email → link accounts

**Effort:**
- Google: ~2–3 hours (straightforward OAuth 2.0 setup in Google Cloud Console)
- Apple: ~4–6 hours (Apple's client secret is a short-lived JWT signed with a private key; developer account required)

**Recommendation:** ship Google first, add Apple only if there's demand.

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

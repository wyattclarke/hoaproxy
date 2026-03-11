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

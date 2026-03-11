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

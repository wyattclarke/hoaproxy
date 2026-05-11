# Scrape-protection runbook

*Companion to `docs/scaling-proposal.md` §Risks #6. Ships alongside the
Phase 2 ingest-worker cutover.*

Goals:

1. **Don't pay** for surprise PDF-download traffic. GCS egress is
   $0.12/GB; a determined scraper grabbing 50 GB/day is $180/day on the
   bank bucket alone.
2. **Don't OOM Render** by proxying large PDFs through the web service.
3. **Hard-stop** if both rate limits and CDN fail.

The three layers, defense-in-depth:

| Layer | Tech | What it does |
|---|---|---|
| L1 — App | `_check_rate_limit` on `/hoas/{hoa}/documents/file` | 60 req/IP/hour |
| L2 — Edge | Cloudflare Worker on `documents.hoaproxy.org` | Caches PDFs, blocks aggressive UAs |
| L3 — Bank | GCP Budget + `scripts/gcp_egress_cap` Cloud Function | Auto-unlinks billing at $200/day egress |

L1 ships in this PR (`api/main.py`). L2 + L3 need operator-side wiring
once and never need to change.

---

## L1 — Per-IP rate limit (already deployed)

`/hoas/{hoa_name}/documents/file` is rate-limited to 60 requests / IP /
hour via `_check_rate_limit(request, limit=60)`. The endpoint also
returns `Cache-Control: public, max-age=86400, immutable` so any CDN in
front (L2) caches each PDF for a day.

Test:

```bash
# Should get 429 after 60 calls from the same IP in an hour.
for i in $(seq 1 65); do
    curl -s -o /dev/null -w "%{http_code}\n" \
        "https://hoaproxy.org/hoas/Test%20HOA/documents/file?path=test.pdf"
done | sort | uniq -c
```

Tune the limit by editing `api/main.py`. If the cron'd backup or an
internal smoke test trips it, exempt the IP via `_RATE_LIMIT_EXEMPT_IPS`
(not currently configured — wire it if needed).

---

## L2 — Cloudflare Worker in front of GCS PDFs

**One-time operator setup** (~30 min). Replaces direct Render → GCS
egress with cached-at-edge delivery.

### Worker script

Save as `documents-cache.js` in a Cloudflare Worker bound to
`documents.hoaproxy.org/*`:

```javascript
// Cloudflare Worker — caches HOAproxy PDF downloads at the edge.
//
// Request flow:
//   browser → documents.hoaproxy.org/{sha}.pdf
//     → Cloudflare cache (HIT → 0 origin reads)
//     → MISS → hoaproxy.org/hoas/.../documents/file?path=... → 302 to
//       signed GCS URL → Cloudflare fetches GCS, caches the body for
//       1 day, returns to browser.
//
// The Render origin is hit at most once per PDF per Cloudflare data
// center (≈300 worldwide). A scraper hitting one PoP repeatedly will
// see edge cache hits, never reaching Render or GCS.

const ORIGIN = "https://hoaproxy.org";

addEventListener("fetch", (event) => {
  event.respondWith(handle(event.request));
});

async function handle(request) {
  // Strict UA filter — block obvious scrapers. Adjust the allow-list
  // based on observed traffic (curl/wget are blocked because real
  // users hit this via browsers; agents that want bulk access should
  // use the public /search API).
  const ua = (request.headers.get("user-agent") || "").toLowerCase();
  if (
    !ua ||
    ua.startsWith("curl/") ||
    ua.startsWith("wget/") ||
    ua.startsWith("python-requests/") ||
    ua.startsWith("go-http-client/") ||
    ua.includes("scrapy")
  ) {
    return new Response("Forbidden — public scraping disabled", { status: 403 });
  }

  const url = new URL(request.url);
  // Only GET on .pdf paths goes through caching; everything else 404s.
  if (request.method !== "GET" || !url.pathname.endsWith(".pdf")) {
    return new Response("Not Found", { status: 404 });
  }

  // Cloudflare cache lookup. The cache key intentionally drops query
  // params so two URLs that resolve to the same content hit one entry.
  const cache = caches.default;
  let response = await cache.match(request);
  if (response) {
    return response;
  }

  // Forward to Render origin. The Render endpoint either returns a
  // FileResponse directly (current Phase 1) or 302s to a signed GCS
  // URL (after the Phase 1 "PDFs off disk" migration). Either way,
  // Cloudflare follows redirects and caches the final body.
  const originUrl = `${ORIGIN}${url.pathname}${url.search}`;
  response = await fetch(originUrl, {
    cf: { cacheTtl: 86400, cacheEverything: true },
    headers: {
      // Pass through caller's UA so app-layer rate limit sees the real client.
      "user-agent": ua,
      "x-forwarded-for": request.headers.get("cf-connecting-ip") || "",
    },
  });

  if (response.status === 200) {
    // Clone before stashing in the cache (response body can only be
    // read once).
    const clone = response.clone();
    // Override Cache-Control to be sure Cloudflare honors our TTL.
    const headers = new Headers(clone.headers);
    headers.set("Cache-Control", "public, max-age=86400, immutable");
    const cacheable = new Response(clone.body, {
      status: clone.status,
      headers,
    });
    event.waitUntil(cache.put(request, cacheable.clone()));
    return cacheable;
  }
  return response;
}
```

### DNS + Worker route

1. Add `documents.hoaproxy.org` to Cloudflare with proxied (orange-cloud)
   DNS pointing at the same Render web service.
2. Workers → Create Application → "documents-cache" → paste the script.
3. Triggers tab: bind to `documents.hoaproxy.org/*`.
4. Cache rules: ensure the default cache TTL is at least 1 day for
   anything Worker-cached.

### App-side integration

Once the Worker is live, change the SSR templates that link to PDFs to
use `https://documents.hoaproxy.org/{path}` instead of the direct Render
URL. Cloudflare caches by URL, so the simpler the URL pattern, the
higher the hit rate.

Until that change ships, the Worker is dormant — it costs nothing if no
request hits it.

---

## L3 — GCP egress budget auto-shutoff

Same pattern as the `stop-billing` Cloud Function at $600/mo total
spend, but scoped to GCS egress.

### Deploy the function

```bash
cd scripts/gcp_egress_cap
gcloud functions deploy stop-gcs-egress \
    --runtime python311 \
    --trigger-topic gcs-egress-budget-alerts \
    --entry-point stop_gcs_egress \
    --region us-central1 \
    --source .
```

### Wire the budget

In GCP Console:

1. Cloud Pub/Sub → topics → create `gcs-egress-budget-alerts`.
2. Cloud Billing → Budgets & alerts → Create budget:
   - Scope: Service = "Cloud Storage" (or specific buckets:
     `hoaproxy-bank`, `hoaproxy-ingest-ready`).
   - Amount: $200 (hard cap).
   - Threshold rules:
     - 25% ($50): action = email the operator notification channel.
     - 100% ($200): action = the `gcs-egress-budget-alerts` Pub/Sub
       topic. This fires the Cloud Function, which unlinks billing.
3. Notification channel: configure email/SMS for `wyatt.clarke@gmail.com`.

### Re-enable billing after a hard-stop

```bash
gcloud billing projects link hoaware --billing-account=01FBA6-3384FF-C9BD1A
```

After re-linking, audit the access logs (`gs://hoaproxy-bank/_logs/`)
to find the IP / UA pattern that drove the egress, and tighten the
Cloudflare Worker UA filter.

---

## What this combination does NOT protect against

- **Legitimate-looking traffic** (real browsers from many IPs)
  spread thin enough to evade the per-IP cap. We'd need a JS challenge
  or signed-URL auth for that. Out of scope for Phase 2; revisit if
  abuse is observed.
- **DocAI runaway** — that's bounded separately by the $20/day daily
  cap in `_check_daily_docai_budget` and the $600/mo `stop-billing`
  Cloud Function.
- **Render egress** — Render bills the web service's outbound bandwidth
  separately from GCS. Cloudflare in front of Render is the same trick
  but on the Render side; it isn't deployed yet (Phase 3 task in the
  scaling proposal). Until Phase 3 ships, L1 + L3 cover the
  document-download path; L2 reduces the Render bill once the static
  pages get the Cloudflare treatment too.

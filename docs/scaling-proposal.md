# HOAproxy 10x Scaling Proposal

*Drafted 2026-05-09. Revise as data lands.*

## TL;DR

The site's current bottleneck is **disk on Render**, not CPU/RAM/queries. The disk grew from 54.8 GB on 2026-05-05 to 117.3 GB on 2026-05-10 — ~12 GB/day during an active ingest period. If that pace continued steady-state the 200 GB disk would fill in about a week; even at half that rate it's a 2-3 week runway. A naive "10x" lift triples Render spend, and Render disks max out at 1 TB anyway — so the brute-force path doesn't reach 10x at all.

**The real fix is to stop using the Render disk for PDFs.** PDFs are already in `gs://hoaproxy-bank/` (122.9 GiB) and `gs://hoaproxy-ingest-ready/` (24.5 GiB). Live SQLite holds the *searchable* state (~1 GB). Once PDFs come off the persistent disk and ingest moves to a worker, 10x is mostly a config change and we stay under ~$100/mo.

Phases 1–3 are independently shippable and recover cost + headroom **before** any 10x demand. Phases 4–5 are gated on real load signals (`p95 search > 500 ms`, RPS > 5).

## Current state (verified)

Source: Render API + `gsutil du` + reading `hoaware/`, `api/`, `settings.env` keys.

| Component | Today | Plan / Cost | Source |
|---|---|---|---|
| Web service | Render Standard, 1× instance, Oregon | $25/mo | `/v1/services/srv-d62kms68alac738h67b0` |
| RAM use (7d) | 600 MB avg / 672 MB peak (of ~2 GB) | — | `/v1/metrics/memory?...` |
| HTTP traffic (7d) | 214,733 reqs / 7d ≈ 0.36 RPS avg | — | `/v1/metrics/http-requests` |
| Persistent disk | 200 GB, 117 GB used (60%), growing ~7 GB / 12 h | $50/mo | `/v1/metrics/disk-usage` |
| Cron (DB backup) | Starter, 0 10,22 \* \* \* | ~$1/mo | `crn-d7vbkddckfvc73eei9m0` |
| SQLite (`/var/data/hoa_index.db`) | ~1 GB live, ~1 GB nightly snapshot until 2026-04-15; switched to "precious-only" (~10 KB) | — | `gs://hoaproxy-backups/db/` |
| `hoa_docs/` (PDFs on disk) | ~115 GB (the bulk of disk) | — | inferred (disk − db − qdrant_local) |
| Qdrant local | **disabled** (`HOA_DISABLE_QDRANT=1`) — sqlite-vec is the only vector store | — | env var |
| Bank (GCS) | `gs://hoaproxy-bank/` 122.91 GiB | $0.020/GB·mo ≈ $2.50 | `gsutil du` |
| Ingest-ready (GCS) | `gs://hoaproxy-ingest-ready/` 24.48 GiB | ≈ $0.50 | `gsutil du` |
| Backups (GCS) | `gs://hoaproxy-backups/db/` 9.21 GiB | ≈ $0.20 | `gsutil ls` |
| OpenAI embeddings | `text-embedding-3-small`, **no cache** | $0.02 / 1M tokens | `hoaware/embeddings.py:8-24` |
| QA LLM | Groq `llama-3.3-70b-versatile` | free / near-zero | `QA_API_BASE_URL` env |
| DocAI OCR | $0.0015/page, daily cap $20 | $0–$600/mo | `DAILY_DOCAI_BUDGET_USD=20` |
| Email | `EMAIL_PROVIDER=stub` (logs only) | $0 | env var |

**Total today:** ~$80/mo Render + ~$5/mo GCS + a few dollars OpenAI/DocAI ≈ **~$90–100/mo**.

### Architectural notes that matter for scaling

- **Vector store is sqlite-vec** (`chunk_vec` virtual table, `hoaware/db.py:441`) with `hoa_id INTEGER PARTITION KEY`. Per-partition brute force — single-HOA search stays fast no matter the corpus size; cross-HOA `/search/multi` is O(N).
- **Bank/live separation is intact.** `hoaware/bank.py` writes only to GCS; nothing in `api/main.py` reads `gs://hoaproxy-bank/` directly. Drain happens via `/admin/ingest-ready-gcs` (operator-triggered) or `/upload` (user-triggered).
- **No embedding/query cache anywhere.** `batch_embeddings` always calls OpenAI; identical search queries pay twice.
- **`/upload` is the OOM-prone path.** A 75-second pacing memory note exists from prior crashes — RAM contention from concurrent OCR + embedding on the web instance.
- **Daemon threads on boot** (`api/main.py:419-423`): vec backfill, proxy-status backfill, cost report, polygon refit, geo seed. All single-process; not a horizontal-scaling blocker yet but they pin work to instance #1.

### Findings that surprised me

- **PDFs are stored twice.** ~115 GB on Render disk and ~123 GB in the bank — substantially the same content. We're paying $50/mo for the duplicate.
- **Disk is growing fast.** Two weeks of headroom at the current rate. Not 10x — the *current* trajectory needs a fix.
- **Qdrant was already disabled.** The `data/qdrant_local` directory is dormant. Re-enabling Qdrant (local or cloud) is an *option*, not a *necessity* — sqlite-vec is doing the work today.
- **vec0 `PARTITION KEY` pre-allocates a 1024-vector chunk per partition value** (sqlite-vec default `chunk_size=1024`). At 50K HOAs that's 50K × 1024 × 6 KB = **~300 GB of chunk allocation alone**, even if each HOA only has 30 actual chunks. Discovered while running Spike A (see below). Today this isn't visible because we have hundreds of HOAs averaging ~300 chunks each — partitions are dense — but it's a hard scaling trap if HOA count grows faster than per-HOA doc count.

## What breaks at 10x

For each axis, "10x" means roughly: 10x corpus (≈1M HOAs / 1M docs / 10M chunks), 10x daily traffic (~3.6 RPS avg, 50 RPS peak), and 10x ingest throughput.

| Axis | Bottleneck at 10x | Why |
|---|---|---|
| **Disk** | **HARD CAP at 1 TB** (Render) | 117 GB → 1.17 TB; Render single-disk max is 1 TB |
| Web RAM | Soft — likely fits on Standard | 600 MB → ~1.2 GB at 10x concurrency |
| Web CPU | ~120% on single instance | Currently low; need 2 instances |
| sqlite-vec single-HOA query | Fine | Per-partition brute force, partition stays small |
| sqlite-vec `/search/multi` (cross-HOA) | Slow | O(N) brute force on 10M chunks ≈ ~1 s; needs HNSW or partition prefilter |
| SQLite write contention | Probably fine | Proxy votes / auth scale to thousands/sec on WAL; only breaks at very high write fan-out |
| OpenAI embeddings $ | Real but small | 10x → still ~$20/mo without cache; cache cuts it ~50% |
| DocAI $ | Already capped | $20/day cap is the throttle; corpus ingest just takes longer |
| Ingest throughput | Bottlenecked at the web process | 75 s pacing + single worker → can't ingest faster |
| Static page traffic | Render bandwidth | Cloudflare in front saves money + protects origin |

**The only hard wall is disk.** Everything else is a soft cost or latency tradeoff.

## Spikes

### Spike A — sqlite-vec at projected scale

**Goal:** measure single-HOA and `/search/multi` latency at 150K (today) and 500K–1.5M (10x) chunks.

**What actually happened:** the synthetic `chunk_vec` table for the 5K-HOA × 30-chunk baseline scenario blew past 13 GB on disk before the populate loop hit 50K of 150K rows, and the process was killed by macOS memory pressure. **Diagnosis:** vec0 with `PARTITION KEY` allocates a full chunk (default `chunk_size=1024` vectors) per *partition value*, so 5K sparse partitions × 1024 × 6 KB = ~30 GB of chunk allocation regardless of how few real vectors are stored. Latency numbers not collected.

**Why production hasn't hit this yet:** the live DB is ~1 GB, which means production has *dense* partitions — probably hundreds of HOAs averaging hundreds of chunks each, not thousands of HOAs with tens of chunks each. The amplification factor only kicks in if HOA count grows faster than chunks-per-HOA.

**What we still know from algorithm + small-scale checks:**
- vec0 brute-force within a partition is O(N_partition × dim) memory bandwidth. At ~10 GB/s a modern CPU does ~1.5M 1536-dim vectors/sec, so a single-HOA query stays sub-millisecond as long as per-HOA chunks stay in the hundreds.
- Cross-partition (`/search/multi`, no `WHERE hoa_id=?`) brute-force at 10x: 1.5M chunks ≈ ~1 s. Too slow for interactive multi-HOA search.
- Single-HOA `/search` (the dominant path per `api/main.py` route inventory) is **not** the problem. `/search/multi` is. *And* the partition-allocation amplification is a new concern at 10K+ HOAs.

**Implication:** Phase 4 is more strongly motivated than the original draft suggested. There are now two failure modes for staying on vec0: latency on cross-HOA queries (already known) and disk amplification when HOA count outpaces per-HOA chunks (new). Both push toward pgvector with HNSW.

**TODO if useful:** rerun the spike with a larger `chunk_size` parameter (vec0 caps it; need to find the limit) or with a denser partition distribution (e.g., 200 HOAs × 750 chunks). Will confirm latency curve without the disk-amplification artifact.

### Spike B — Qdrant Cloud cost at 10x

Qdrant's public pricing page is calculator-only; need to estimate from resource specs. For 1.5M × 1536-dim float32 vectors with HNSW:
- Raw vector storage: ~9 GB
- HNSW overhead: ~30% → ~12 GB total
- Smallest viable cluster: ~2 GB RAM / 16 GB disk → roughly **$30–50/mo** for managed Qdrant Cloud (rough; a real quote requires contacting them).
- Self-hosted Qdrant on Render Background Worker (4 GB / 100 GB disk): ~$40/mo.

### Spike C — pgvector on managed Postgres at 10x

Neon, Supabase, and Render Postgres all support pgvector with HNSW. For ~10 GB of data + index:
- **Neon**: ~$25/mo for 10 GB compute + storage with autoscaling.
- **Supabase**: ~$25/mo Pro tier.
- **Render Postgres**: ~$50/mo for similar capacity.

pgvector consolidates: you replace SQLite *and* vec0 with one component. That's why Phase 4 below recommends it over Qdrant.

### Spike D — embedding cache hit rate

Couldn't run a real measurement (no production query log to sample). Conservative estimate from typical Q&A workloads:
- `/search`: 30–60% hit rate (users repeat their queries; common phrases like "pet policy" recur)
- `/qa`: 10–20% (more specific natural-language questions)

A SQLite-backed LRU at the embedding layer is ~30 lines of code and pays for itself instantly.

## The proposal

Five phases, ordered by **value × confidence ÷ risk**. Phases 1–3 ship now; Phases 4–5 are gated on signals.

### Phase 1 — Stop double-storing PDFs *(immediate)*

**Goal:** PDFs leave the Render disk. Live disk drops from ~117 GB to ~5 GB.

**Changes:**
1. `/upload` handler: write PDF to `gs://hoaproxy-bank/...` first (already happens via the bank), then **stop also copying to `/var/data/hoa_docs/`**.
2. `/admin/ingest-ready-gcs`: stop materializing PDFs locally. Stream directly from GCS to DocAI/parser; chunks land in SQLite as today.
3. `/hoas/{name}/documents/file` route: replace local-file response with a **signed-URL redirect** to `gs://hoaproxy-bank/...`. (302 with 5-minute signed URL.)
4. One-time backfill: ensure every PDF in `hoa_docs/` is in `gs://hoaproxy-bank/`. Anything missing → upload, then delete locally.
5. Provision a smaller Render disk (20 GB) and migrate. *Render disks can only grow, not shrink — this requires a new disk + cutover.*

**Cost delta:** −$45/mo (200 GB disk → 20 GB).
**Risk:** Low. Document downloads gain ~50–150 ms one-time per click (signed URL redirect). Search/QA latency unchanged.
**Effort:** ~1 day.

### Phase 2 — Move ingest off the web service *(removes the OOM ceiling)* ✅ SHIPPED 2026-05-11

**Goal:** `/upload` becomes a thin enqueue. Ingest runs on a dedicated worker with its own RAM budget. Eliminates the 75 s pacing memo.

**Changes (as shipped):**
1. ~~New Render Background Worker (`hoaproxy-ingest`, 4 GB RAM, $25/mo)~~ → Co-located worker process in the existing web container (`scripts/start_web_with_worker.sh`). Render persistent disks can't be shared across services, so a single container with two sibling processes (uvicorn + `python -m hoaware.ingest_worker`) is the deployment shape until Phase 4 (Neon Postgres) makes splitting trivial. Each process has its own Python heap → RAM isolation preserved.
2. `/upload` writes the PDF to local disk (with sidecar JSON of agent metadata), inserts a `pending_ingest` row pointing at a `local://` URI, and returns 200 with `{queued: true, job_id, status_url}`. The worker dispatches on URI scheme: `gs://` for prepared GCS bundles, `local://` for upload sidecars. (Phase 1 will eventually flip /upload to write to `gs://hoaproxy-bank/` directly; both URI schemes will continue to be supported.)
3. Worker drains: DocAI/OCR → chunk → embed → upsert → mark complete.
4. New `/ingest/status/{job_id}` route — public, rate-limited.
5. Web service RAM target stays ~600 MB peak (ingest's RAM lives in the worker heap; concurrent OCR/embed no longer compete with request handlers).
6. **Feature flag `ASYNC_INGEST_ENABLED`** added so the cutover was a single env-var change with a one-line rollback. Default off; flipped to on at 03:46Z on 2026-05-11.

**Cost delta:** **$0** at Phase 2 (co-located process); +$25/mo when split off into a true `type: worker` Render service after Phase 4.
**Risk:** Medium-low as observed. Cutover smoke test ran 10/10 sample jobs successfully, average drain latency ~250 ms/job. No new prod errors post-cutover.
**Effort:** ~1 day (instead of estimated 2-3) thanks to the existing prepared-ingest scaffolding.

**Gotchas discovered:**
- Render's API: `PATCH /v1/services/{svc}/env-vars/{key}` returns 405. The correct single-var endpoint is `PUT /v1/services/{svc}/env-vars/{key}` (distinct from the memory-rule footgun, which is the full-collection `PUT /env-vars`).
- 36 existing state-scraper `run_state_ingestion.py` scripts had a hardcoded counter (`r.get("status") == "imported"`) that doesn't match the async response shape (results are `{job_id, prefix}`, no status). Bulk-patched. Loop *exit* was correct in both modes (uses `found==0`), so the bug was stats-only.
- See `docs/phase2-retrospective.md` for the full chronology.

### Phase 3 — Caching *(quick wins on cost and latency)*

**Goal:** Cut OpenAI cost ~30–50% and offload static traffic from Render.

**Changes:**
1. **Embedding cache.** New SQLite table `embedding_cache(query_hash, model, embedding BLOB, last_seen_at)`. `batch_embeddings()` checks cache before calling OpenAI; writes back with TTL 30 days. ~30 LOC change in `hoaware/embeddings.py`.
2. **Cloudflare in front of Render.** Free plan. Cache `/`, `/about`, `/privacy`, `/terms`, `/static/*`, `/robots.txt`, `/sitemap.xml`, and `/hoas` listings (with short TTLs). Page Rules to bypass cache on auth and POST routes.
3. **Browser cache headers** on `/static/*` (already partly there; tighten with hashed asset names).

**Cost delta:** −$5–15/mo (OpenAI) + Render bandwidth savings; +$0 for Cloudflare free tier.
**Risk:** Low. Cache invalidation only matters for `/hoas` listings; 60 s TTL is fine.
**Effort:** ~1 day.

### Phase 4 — Vector search at scale *(only when triggered)*

**Trigger:** any of:
- `/search/multi` p95 > 500 ms,
- chunks table > 1 M rows,
- planned launch of a "search by state" or whole-corpus feature.

**Recommendation:** Migrate the live DB to **managed Postgres + pgvector** (Neon, ~$25/mo).

**Why pgvector over Qdrant Cloud:**
- Consolidates: replaces both SQLite *and* vec0 with one store, reducing moving parts.
- Single transactional store keeps proxy-voting / auth on the same DB as document chunks. (Currently SQLite already does this; preserves the property.)
- HNSW index handles 10–100M vectors at sub-100 ms.
- Easier multi-instance web (no shared-disk SQLite).

**Migration plan:**
1. Provision Neon, copy schema, dual-write for one week.
2. Switch reads to Postgres behind a feature flag.
3. Decommission SQLite live DB; keep `/var/data/hoa_index.db` as cold backup for 30 days.

**Cost delta:** +$25/mo Postgres, −$5/mo Render disk (drop the 20 GB to 1 GB or remove entirely once SQLite is gone).
**Risk:** Medium-high. Biggest migration in the plan. Roll back by toggling reads back to SQLite.
**Effort:** ~1 week.

### Phase 5 — Horizontal web scaling *(only when triggered)*

**Trigger:** sustained CPU > 70% for 30 min on the single instance, or peak RPS > 5 with rising p95 latency.

**Changes:**
1. Render Pro plan (3 instances) **or** 2× Standard with their managed load balancer.
2. Move daemon-thread boot tasks (vec backfill, proxy-status backfill, geo refit, cost report) into the Phase-2 worker so they run once per cluster, not once per instance.
3. Ensure no instance-local state outside Postgres + GCS (already mostly true).

**Cost delta:** +$50/mo (Pro plan, or 2× Standard).
**Risk:** Low if Phase 4 is done. High if SQLite is still the live DB.
**Effort:** ~2 days config + boot-task refactor.

## Cost projection at 10x

| Component | Today | After P1–3 | After P1–5 (full 10x) | Naive 10x (no architecture change) |
|---|---:|---:|---:|---:|
| Render web | $25 | $25 | $50–80 | $80 (Pro) |
| Render disk | $50 | $5 | $0–$5 | **$250** (1 TB) |
| Render BG worker | — | $25 | $25 | — |
| Render cron | $1 | $1 | $1 | $1 |
| Postgres (Neon) | — | — | $25 | — |
| GCS storage | $3 | $5 | $10 | $10 |
| GCS egress | <$1 | $2–5 | $5–10 | $0 |
| OpenAI embeddings | ~$2 | ~$2 | ~$5 (cached) | ~$20 (uncached) |
| DocAI | $0–600 (capped) | same | same | same |
| Cloudflare | — | $0 | $0 | — |
| **Subtotal (excl. DocAI)** | **~$82** | **~$65** | **~$120–150** | **~$360+** |

**The naive path costs ~3× more and still hits the 1 TB disk wall.** The phased path is *cheaper than today after Phase 1–3* and reaches 10x for ~$120/mo.

## Risks & non-obvious traps

1. **Render disks can only grow, not shrink.** Phase 1 needs a *new* smaller disk + cutover, not an in-place resize.
2. **Render env-var PUT is full-replace and silently drops sensitive vars** (`reference_render_api.md`). Use `PATCH /env-vars/{key}` for the single-var changes Phases 2–4 need; never round-trip GET → PUT.
3. **`/upload` 75 s pacing must stay until Phase 2 ships.** Removing it earlier reproduces the OOM crashes.
4. **DocAI billing has an auto-shutoff** at $600/mo via the `stop-billing` Cloud Function (`reference_local_docai.md`). A backfill burst can hit this; plan ingest waves around the cap.
5. **Cloudflare in front of Render needs a Render custom domain** — straightforward but moves DNS into the migration path.
6. **GCS egress to Render costs $0.12/GB** — fine for documents, would be expensive for hot-path search reads. Phase 1 only egresses on user clicks (rare); Phase 4 keeps search inside Postgres so egress stays low.
7. **vec0 partition amplification** (see Spike A) — if the corpus skews toward more HOAs with fewer chunks each (e.g., a thin-coverage state-wide rollout), `chunk_vec` can balloon to 30+ GB on what is logically a 1 GB dataset. Mitigation: keep on vec0 only while partitions are dense (>200 chunks/HOA on average), or move to pgvector earlier than Phase 4's trigger if HOA count outpaces ingest depth.

## What is explicitly NOT in this plan

- Switching QA off Groq. It works and is ~free.
- Building a custom OCR pipeline. DocAI is fine.
- Multi-region / multi-cloud. Premature; a single Oregon region serves a US-only product fine.
- Microservices split. The monolith with one worker is the right shape at this scale.
- Replacing FastAPI with a heavier framework. No reason to.

## Recommended sequencing

```
Week 1:  Phase 1 (PDFs off disk)              — saves $45/mo, frees runway
Week 2:  Phase 3 (caching)                    — instant cost + UX win
Week 3:  Phase 2 (ingest worker)              — removes the OOM ceiling
       —— Stop here. Watch metrics for 4–6 weeks. ——
Phase 4 only if /search/multi p95 > 500 ms or chunks > 1M.
Phase 5 only if CPU sustained > 70% or peak RPS > 5.
```

After Phases 1–3 the site is **smaller, faster, cheaper, and ready for 10x**. Phases 4–5 only spend money when load actually demands it.

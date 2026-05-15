# Upload Acceleration Plan — Move More to GCS

**Goal:** Make live-site HOA upload 10–100× faster by moving the remaining
heavy work (chunking + OpenAI embeddings + DocAI fallback) out of the
Hetzner ingest worker and into a GCS-staged "prepared bundle v2" that the
server ingests with no external API round-trips.

**Status today (2026-05-15):**
- v1 prepared bundles already exist (`hoaware/prepared_ingest.py`).
- v1 already moves **OCR** off the host: bundles include a `text_gcs_path`
  sidecar; ingest passes `pre_extracted_pages` to `_ingest_pdf`, which
  skips `extract_pages`/DocAI when text is supplied
  ([`hoaware/ingest.py:146`](../hoaware/ingest.py#L146)).
- **What still runs on Hetzner per document:**
  - `chunk_pages(...)` (cheap, ~10 ms)
  - **`batch_embeddings([...])` against OpenAI** ([`hoaware/ingest.py:212`](../hoaware/ingest.py#L212))
    — this is the long pole. Today the worker hits ~1.5 s/doc on a
    pegged single core, almost entirely embedding RT + SQLite contention.
  - `db.replace_chunks` + vec0 partition writes.

The remaining win is in lifting chunking + embedding to the prepare side
and shipping the chunk vectors **inside** the bundle. The server's job
collapses to validate-and-INSERT.

---

## Target architecture

```
┌─────────────────────────┐      ┌──────────────────────────┐
│ Prepare worker (GCS-    │      │ Hetzner ingest worker    │
│ resident, horizontally  │      │ (single SQLite writer)   │
│ scalable: Cloud Run /   │      │                          │
│ laptop / VM)            │      │  POST /admin/ingest-     │
│                         │ ───▶ │       ready-gcs          │
│  PDF → DocAI → chunks   │      │                          │
│       → OpenAI embed    │      │  bundle v2 → validate    │
│       → write bundle v2 │      │     → db.replace_chunks  │
│                         │      │     → vec0 inserts       │
│  gs://hoaproxy-ingest-  │      │                          │
│     ready/v2/.../       │      │  no DocAI, no OpenAI     │
│                         │      │                          │
└─────────────────────────┘      └──────────────────────────┘
```

Hetzner's per-doc work shrinks from ~1500 ms (embedding-dominated) to
~50 ms (parse + INSERT-dominated). Prepare-side is naturally
parallelizable: N workers running concurrently in GCS-land can saturate
the Hetzner ingestor.

---

## Bundle v2 schema

`schema_version: 2` reuses everything in v1 and adds **one optional
sidecar per document**: `chunks_gcs_path`. The sidecar is a JSON object
with the document's chunk array + embeddings + the model that produced
them.

### Per-doc addition to `bundle.json::documents[i]`

```jsonc
{
  // ...all v1 fields stay...
  "chunks_gcs_path":
    "gs://hoaproxy-ingest-ready/v2/FL/palm-beach/foo/{sha[:12]}/chunks.json",
  // optional but recommended; bundles without it fall back to the
  // v1 path (server-side chunking + embedding).
}
```

### `chunks.json` sidecar

```jsonc
{
  "schema_version": 2,
  "doc_sha256": "ae3f...",
  "chunker": {
    "max_chars": 1800,
    "overlap_chars": 200,
    "library_version": "hoaware.chunker@2026.05"
  },
  "embedder": {
    "provider": "openai",
    "model": "text-embedding-3-small",
    "dimensions": 1536,
    "produced_at": "2026-05-15T03:12:09Z"
  },
  "chunks": [
    {
      "idx": 0,
      "text": "...",
      "page_start": 1,
      "page_end": 1,
      "char_offset_in_doc": 0,
      "embedding": [0.0123, -0.4456, ...]   // 1536 floats
    },
    ...
  ]
}
```

**Why a separate sidecar (not inline in `bundle.json`):** keeps
`bundle.json` small so manifest scans/queue ops stay cheap. A 32-page CCR
with ~50 chunks produces a ~310 KB sidecar (50 × 1536 × 4 B = 307 KB +
text). Bundles can carry 5–10 docs without inflating manifests.

**Why versioned + model-stamped:** server **must refuse** ingest if its
configured `OPENAI_EMBEDDING_MODEL` / `dimensions` don't match. Stale
bundles would silently poison vector search.

---

## Phases

### Phase A — Prepare-side embedder (no Hetzner changes)

Build the producer that bakes v2 bundles. Already-banked PDFs become the
input.

**Files (new):**
- `hoaware/prepare/__init__.py` — module marker
- `hoaware/prepare/embed.py` — `bake_chunks_sidecar(pdf_path, pages, *, model, chunk_char_limit, chunk_overlap) → dict`
- `scripts/prepare/bake_bundle.py` — single-shot wrapper: reads a v1
  bundle, calls `_ingest_pdf`'s extract→chunk→embed pipeline against
  the upstream PDF/text, writes the v2 sidecar to GCS, rewrites
  `bundle.json` to v2 with `chunks_gcs_path` set.
- `scripts/prepare/bake_state.py` — bulk wrapper: enumerates prepared
  bundles for a state, runs `bake_bundle.py` over them in parallel
  (concurrency configurable, default 8).

**Files (extended):**
- `hoaware/embeddings.py` — `batch_embeddings` already takes a model
  name; no change.
- `hoaware/chunker.py` — `chunk_pages` already returns `Chunk` objects;
  add `to_dict()` that includes `page_start`, `page_end`,
  `char_offset_in_doc`.

**Acceptance:**
- Run `bake_bundle.py` against one HOA's v1 bundle in the existing
  GCS-prepared bucket. The new v2 bundle parses cleanly via the (yet
  unbuilt) validator and `chunks.json` deserializes into `Chunk` objects
  whose embeddings match (within `1e-6`) a fresh `batch_embeddings` call
  on the same texts.
- Cost: ~$0.00002 / 1k tokens × ~3M tokens for a state of 5k HOAs =
  ~$0.06 per state pre-bake. Negligible.

### Phase B — Hetzner: validate + ingest v2 bundles

The server learns to read the new sidecar and skip
chunking/embedding when present.

**Files (extended):**
- `hoaware/prepared_ingest.py`:
  - Bump `SCHEMA_VERSION = 2` (`validate_bundle` accepts both 1 and 2)
  - Add `chunks_sidecar_blob_name(...)` and validator
    `validate_chunks_sidecar(payload) → list[ChunkWithEmbedding]`
  - Server-side **model check**: refuse if
    `payload["embedder"]["model"]` ≠ `settings.embedding_model` or
    `dimensions` ≠ `1536`. Return a stamped reason for the
    `pending_ingest.error` column.
- `hoaware/ingest.py::_ingest_pdf`:
  - New optional param `pre_chunked: list[ChunkWithEmbedding] | None = None`.
  - When supplied, bypass `chunk_pages` and `batch_embeddings`; go
    straight to `db.replace_chunks` with the precomputed embeddings.
  - Telemetry: log `bake_source=prepared_v2` so we can grep speedup.
- `hoaware/ingest_worker.py`:
  - When claiming a `pending_ingest` row whose bundle is v2 with a
    `chunks_gcs_path`, fetch the sidecar (single `download_as_bytes`)
    and pass through to `_ingest_pdf(pre_chunked=...)`.

**Acceptance:**
- For one v2 bundle, the worker log shows
  `bake_source=prepared_v2, embeddings_skipped=true, server_elapsed_s≈0.05`
  (vs ~1.5 s baseline).
- `/admin/ingest/queue-stats` shows the bundle as `done`.
- Vector search results for that HOA are identical to a v1 baseline
  (same model, same chunks, same vectors).

### Phase C — Bulk-bake the existing GCS-prepared backlog

Run Phase A's `bake_state.py` against every v1 bundle currently in
`gs://hoaproxy-ingest-ready/v1/`. Bundles that still need DocAI (no
`text_gcs_path`) get the OCR step here too.

**Sequence:**
1. Snapshot the bank inventory:
   `gsutil ls -r gs://hoaproxy-ingest-ready/v1/ | grep bundle.json` →
   `data/v1_bundle_inventory.txt` (~10–50k entries).
2. Bulk-bake at concurrency=16 with a per-state cap so OpenAI rate
   limits don't trip. Estimated wall time: 5k HOAs × ~0.3 s/doc
   (parallel) ≈ 15 min/state.
3. After bake completes, queue v2 bundles via
   `POST /admin/ingest-ready-gcs` and watch
   `/admin/ingest/queue-stats` drain at ~50 ms/doc.

**Where to run the bake:** anywhere with `OPENAI_API_KEY` +
`GOOGLE_APPLICATION_CREDENTIALS`. Recommended: a Cloud Run job with
`max_instances=8`, billed in the existing `hoaware` GCP project.
Acceptable: laptop in a tmux. Don't run it on the Hetzner box (defeats
the point).

### Phase D — Ratchet: make v2 the default; quarantine v1

Once Phase C's backlog is drained:

**Files (extended):**
- `hoaware/discovery/probe.py` (or wherever `bank_hoa` is called) — emit
  v2 directly, not v1.
- `hoaware/prepared_ingest.py`:
  - Refuse v1 bundles whose `text_gcs_path` is unset (forces
    OCR-upstream policy already in CLAUDE.md).
  - Log a warning when accepting a v1 bundle that has
    `text_gcs_path` but no `chunks_gcs_path` — "expected v2".
- `hoaware/ingest_worker.py`:
  - Default `pre_chunked=None` only when bundle truly lacks it;
    otherwise require v2.

**Decommission (optional, after a quiet week):**
- Move the chunking/embedding code path in `_ingest_pdf` behind a
  `LEGACY_INGEST_ALLOWED=1` env flag (default off in prod). Hetzner
  reads only.

### Phase E — Parallelize the Hetzner write side (optional, for true 100×)

Bundle v2 alone gets us ~15–30× per-doc speedup. Reaching 100× requires
multiple concurrent SQLite writers. Options ranked by surgery:

1. **Bigger batches inside one writer.** Pre-collect 1000 chunks into a
   single `INSERT INTO chunks` + `INSERT INTO chunks_vec` transaction.
   Cheap. Probably enough.
2. **Two writer processes** with separate SQLite handles — feasible in
   WAL mode but only one can hold the write lock at a time, so the gain
   is in pipelining (one fetches GCS while the other writes). Modest.
3. **Append-only WAL2** (SQLite 3.45+) for higher write concurrency.
   Touches the deployment; defer until v2 throughput is proven.

Recommend (1). Skip (2)/(3) until measurement shows the SQLite writer
is the new bottleneck.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Stale embeddings if the OpenAI model changes | Bundle stamps `embedder.model` + `dimensions`; server refuses mismatched bundles with a stamped reason. Cleanup is "re-bake the state." |
| Schema drift between bake and ingest | `schema_version: 2` + `bake_chunks_sidecar` lives in `hoaware/` so both sides use one source of truth. Tag the chunker library version too. |
| Bake-side OCR/embed failures get hidden | Bake worker writes a `prepare_errors.jsonl` per state run; failing docs land in `hidden_reason=ocr_failed:*` on the bundle so the server marks them visibly. |
| OpenAI rate limits during bulk bake | Concurrency cap (`max_workers=8`) + retry-with-backoff in `batch_embeddings`. Already present. |
| Double-charging for embeddings (re-bake of unchanged docs) | Idempotent: skip bake if `chunks.json` already exists and `doc_sha256` matches. |
| GCS egress cost for huge sidecars | A 50-chunk doc is ~310 KB. 100k docs ≈ 30 GB egress to Hetzner. At GCS standard egress ($0.12/GB intra-continent), ~$3.60 total. Trivial. |
| Schema-version-1 bundles still in flight | Phase B keeps v1 working; Phase D ratchets the default after the backlog drains. |

---

## Expected impact

**Per-document server-side wall time:**

| Stage | v1 (today) | v2 (post-Phase B) |
|---|---:|---:|
| Download/PDF read | ~50 ms | ~50 ms |
| DocAI OCR | already upstream | already upstream |
| Chunking | ~10 ms | 0 (upstream) |
| OpenAI embeddings | ~500–1500 ms | 0 (upstream) |
| Sidecar fetch from GCS | 0 | ~30 ms (one `download_as_bytes`) |
| SQLite + vec0 inserts | ~100 ms | ~100 ms |
| **Total per doc** | **~1.5 s** | **~50–200 ms** |

That's a **7–30× speedup per doc** on the Hetzner side, before any
parallelism. With prepare-side concurrency at 8–16 workers and Phase E
(1)'s batched inserts, **100×** is reachable for bulk drains.

**Hetzner CPU drop:** the 113% sustained uvicorn pegging we observed on
2026-05-13 was almost entirely the embedding round-trips. Removing those
should drop steady-state uvicorn CPU to 10–20% even under heavy drain,
and the WAL fattening problem largely disappears (writer is no longer
hot looping).

---

## Out of scope (intentionally)

- Switching embedding model. v2 is a transport change; the model stays
  `text-embedding-3-small` (1536 dims) unless and until a separate
  re-embed pass is planned.
- Replacing SQLite. Single-writer is fine at the target throughput.
- Qdrant. The optional Qdrant side path stays as-is in v2.
- Changing `/upload`'s contract. Agents that POST PDFs continue to do
  so; the speedup applies to the GCS-staged path which is what the
  state scrapers already use.

---

## Order of operations (if you say go)

1. Implement Phase A (~1 day): schema, `hoaware/prepare/embed.py`,
   `bake_bundle.py` single-shot, integration test against one bundle.
2. Implement Phase B (~½ day): `_ingest_pdf` `pre_chunked` arg,
   worker plumbing, model-mismatch refusal. Pytest coverage.
3. Phase C (~1 day wall clock, mostly waiting on bake): bulk-bake the
   backlog, queue, watch drain.
4. Phase D (~½ day): ratchet defaults, add deprecation log.
5. Skip Phase E unless measurement shows writer is the bottleneck.

Total: ~3 working days of code + a 1-day bulk-bake window.

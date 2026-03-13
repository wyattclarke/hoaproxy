# Resident Proposals — Implementation Plan

A lightweight "show of hands" feature. Any HOA member can post an idea; others in the same HOA can endorse it once. No formal voting, no proxies. Boards see community sentiment; members see what their neighbors care about.

**Self-moderation mechanisms** (no admin role required):
- **Private by default** — proposals start invisible; the submitter shares a 4-char code with neighbors out-of-band
- **Co-signer threshold** — 2 co-signers (3 total supporters) required before a proposal appears on the public feed
- **One active proposal per unit** — prevents spam; user must withdraw or wait for archive before creating another
- **Upvotes only** — no downvotes, no comment threads; deliberation happens at board meetings
- **60-day quiet archive** — stale proposals archive automatically, no public notification
- **Anonymous to public** — submitter identity stored internally but never shown on the public feed

---

## Milestone 1 — DB Schema & CRUD (`hoaware/db.py`)

**New tables:**

```sql
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    creator_user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Other',
    status TEXT NOT NULL DEFAULT 'private',
    share_code TEXT NOT NULL UNIQUE,
    cosigner_count INTEGER NOT NULL DEFAULT 0,
    upvote_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_proposals_hoa ON proposals(hoa_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_share_code ON proposals(share_code);

CREATE TABLE IF NOT EXISTS proposal_cosigners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(proposal_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_cosigners_proposal ON proposal_cosigners(proposal_id);

CREATE TABLE IF NOT EXISTS proposal_upvotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(proposal_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_upvotes_proposal ON proposal_upvotes(proposal_id);
```

**Design notes:**
- `category` validated at API layer: Maintenance / Amenities / Rules / Safety / Other
- `status` is `private`, `public`, or `archived`
  - `private` — newly created, not yet visible on the feed; awaiting 2 co-signers
  - `public` — co-signer threshold met; visible on the HOA feed, open for upvotes
  - `archived` — auto-archived after 60 days public, or manually withdrawn by creator
- `share_code` — 4-char uppercase alphanumeric, generated server-side at creation. Alphabet excludes ambiguous chars (`0`, `O`, `I`, `1`) → 32-char alphabet (`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`), 32^4 ≈ 1M codes. Used by co-signers to look up private proposals.
- `cosigner_count` — denormalized count of `proposal_cosigners` rows; when it reaches 2, status flips to `public` and `published_at` is set
- `upvote_count` — denormalized count of `proposal_upvotes` rows; only incremented while `status = 'public'`
- `published_at` — set when `cosigner_count` reaches 2; used by the 60-day archive sweep
- `UNIQUE(proposal_id, user_id)` on both cosigners and upvotes enforces one-per-user at DB level (raises `IntegrityError` on duplicate → API returns 409)
- `ON DELETE CASCADE` on cosigners and upvotes
- **No comment table** — deliberation happens at board meetings, not in-app threads

**CRUD functions to add:**

| Function | Notes |
|----------|-------|
| `create_proposal(conn, *, hoa_id, creator_user_id, title, description, category)` | Generates unique `share_code`; returns new proposal id |
| `get_proposal(conn, proposal_id)` | Subqueries for `cosigner_count` and `upvote_count`; does NOT join `creator_display_name` (anonymous to public) |
| `get_proposal_by_share_code(conn, share_code)` | Returns proposal dict or None |
| `list_proposals_for_hoa(conn, hoa_id, *, include_archived=False)` | Only returns `public` (and optionally `archived`) proposals; ordered by upvote_count DESC, then created_at DESC |
| `get_active_proposal_for_user(conn, user_id)` | Returns the user's current `private` or `public` proposal, or None; used to enforce one-active limit |
| `create_cosigner(conn, *, proposal_id, user_id)` | Caller catches IntegrityError → 409; increments `cosigner_count`; if count reaches 2, sets `status = 'public'` and `published_at = now` |
| `delete_cosigner(conn, *, proposal_id, user_id)` | Returns True if deleted, False if not found; decrements `cosigner_count`; if count drops below 2 and status is `public`, reverts to `private` and clears `published_at` |
| `get_cosigner(conn, proposal_id, user_id)` | Returns dict or None; used to set `user_cosigned` flag |
| `list_cosigners(conn, proposal_id)` | Returns `[{user_id, cosigned_at}]` |
| `create_upvote(conn, *, proposal_id, user_id)` | Caller catches IntegrityError → 409; increments `upvote_count` |
| `delete_upvote(conn, *, proposal_id, user_id)` | Returns True if deleted, False if not found; decrements `upvote_count` |
| `get_upvote(conn, proposal_id, user_id)` | Returns dict or None; used to set `user_upvoted` flag |
| `archive_stale_proposals(conn, days=60)` | UPDATE status → `archived` WHERE `status = 'public'` AND `published_at < now - days`; returns count of archived proposals |

Also update the `/healthz` required tables set to include `proposals`, `proposal_cosigners`, and `proposal_upvotes`.

---

## Milestone 2 — API Routes (`api/main.py`)

**Pydantic models:**

```python
PROPOSAL_CATEGORIES = {"Maintenance", "Amenities", "Rules", "Safety", "Other"}

class CreateProposalRequest(BaseModel):
    hoa_id: int
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=5000)
    category: str = "Other"

class ProposalResponse(BaseModel):
    id: int
    hoa_id: int
    hoa_name: str | None = None
    creator_user_id: int
    # creator_display_name intentionally omitted — anonymous to public
    title: str
    description: str
    category: str
    status: str
    cosigner_count: int = 0
    upvote_count: int = 0
    share_code: str | None = None      # only populated for the creator (GET /proposals/mine)
    user_cosigned: bool = False
    user_upvoted: bool = False
    created_at: str | None = None
    published_at: str | None = None
```

**Routes** (add in a `# Proposals` section after the proxy routes):

| Method | Path | Auth | Rate limit | Notes |
|--------|------|------|------------|-------|
| `POST` | `/proposals` | ✓ member | 10/60s | Validate category; strip whitespace; generate `share_code`; check `get_active_proposal_for_user` → 409 if active proposal exists; return response with `share_code` |
| `GET` | `/hoas/{hoa_id}/proposals` | ✓ member | — | Returns only `public` proposals (+ `?include_archived=true`); set `user_upvoted` per proposal; omit `share_code`; omit author identity |
| `GET` | `/proposals/mine` | ✓ user | — | Returns current user's proposals (all statuses); includes `share_code` for `private` proposals; includes creator identity for self |
| `GET` | `/proposals/{proposal_id}` | ✓ member | — | Membership check against proposal's hoa_id; if `private`, only visible to creator and co-signers; omits author identity in response |
| `POST` | `/proposals/cosign/{share_code}` | ✓ member | 30/60s | Look up private proposal by share code; 404 if not found or not in same HOA; 403 if own proposal (submitter cannot co-sign); 409 if already co-signed; 400 if not `private`; on success, if `cosigner_count` reaches 2, set `status = 'public'` and `published_at = now` |
| `DELETE` | `/proposals/{proposal_id}/cosign` | ✓ user | — | 404 if no co-signature to withdraw |
| `POST` | `/proposals/{proposal_id}/upvote` | ✓ member | 30/60s | 400 if proposal status is not `public`; 409 on duplicate |
| `DELETE` | `/proposals/{proposal_id}/upvote` | ✓ user | — | 404 if no upvote to withdraw |
| `DELETE` | `/proposals/{proposal_id}` | ✓ creator | 20/60s | Withdraw — sets status to `archived`; 403 for non-creator; 400 if already archived. Frees the user's active-proposal slot. |

Also add `GET /proposals` page route (include_in_schema=False) serving `proposals.html`.

**Archive sweep:** In the `lifespan` handler (alongside existing proxy expiry sweep), call `archive_stale_proposals(conn, days=60)` on startup. Also run nightly via a lightweight background task or cron-style check — proposals with `status = 'public'` and `published_at < now - 60 days` → `archived`. No public notification.

**Route ordering:** Define `/proposals/mine` and `/proposals/cosign/{share_code}` before `/proposals/{proposal_id}` (same pattern as proxy routes).

---

## Milestone 3 — Frontend: `api/static/proposals.html` (new file)

- HOA selector dropdown (auto-selects if user has exactly one HOA)
- **"New Proposal" as a separate page or modal** (not inline on the feed) — after creation, display the generated 4-char share code prominently with copy button and instructions: "Share this code with 2 neighbors to publish your proposal"
- **"Co-sign a Proposal" flow** — text input for 4-char share code → fetches proposal preview via `POST /proposals/cosign/{share_code}` → shows proposal title/description/category → cosign confirmation button
- Proposal cards on the public feed (sorted by upvote count desc):
  - Title, category pill badge, description excerpt (~200 chars with expand)
  - Upvote count prominently displayed with upvote button (thumbs up / arrow up icon)
  - **No author name** — show "Posted by a verified resident" or omit author entirely
  - No "Close Proposal" button — replaced by "Withdraw" (only on user's own proposals via "My Proposals" view, only while `private` or `public`)
  - Archived proposals: show "Archived" pill, hide upvote button, slight grey
- **"My Proposals" section or link** — shows the user's own proposals with:
  - Creator's own name visible (to self only)
  - Share code displayed for `private` proposals
  - Cosigner count and status
  - "Withdraw" button
- "Show archived proposals" checkbox — re-fetches with `?include_archived=true`
- **No comment threads anywhere** — deliberation happens at board meetings

**Key JS functions:** `loadProposals(hoaId)`, `createProposal(hoaId)`, `cosignByCode(shareCode)`, `toggleUpvote(proposalId, currentlyUpvoted)`, `withdrawProposal(proposalId)`, `loadMyProposals()`

**Style:** Match dashboard.html exactly — same CSS variables, Manrope/Space Grotesk, `.card`, `.pill`, `.btn`, `.meta` classes.

---

## Milestone 4 — Dashboard Integration

**`api/static/dashboard.html`:** Add "Resident Proposals" section (between Delegate Status and Ask Your HOA Docs). Call `loadTopProposals(me)` to fetch top 3 public proposals for the user's primary HOA and render compact cards (title, category, upvote count — no author names) + "View all proposals" link.

**`api/static/js/auth.js`:** Add "Proposals" to `renderNav()` between "My Proxies" and "Legal":
```javascript
'<a class="btn" href="/proposals">Proposals</a>'
```

---

## Milestone 5 — Tests (`tests/test_proposals.py`)

**Test isolation:** Module-level temp DB + `os.environ["HOA_DB_PATH"]`. FK delete order: `proposal_upvotes → proposal_cosigners → proposals → membership_claims → sessions → users`.

**Helper:** `_setup_users_and_hoa()` creates 3 users, one HOA, membership claims; returns headers + hoa_id.

**Test cases (~25):**

| Area | Cases |
|------|-------|
| Creation | happy path (status=private, share_code returned), non-member → 403, bad category → 422, title too short → 422 |
| Share code | 4-char alphanumeric, unique across proposals, no ambiguous chars (0, O, I, 1) |
| One-active limit | second creation while first is `private` → 409, second creation while first is `public` → 409, withdraw frees slot → creation succeeds |
| Co-sign | happy path via share code, wrong HOA → 404, own proposal → 403, duplicate → 409, already public → 400 |
| Publication trigger | 2 co-signers → status becomes `public` + `published_at` set; withdraw cosigner → reverts to `private` |
| Listing | public feed excludes `private` proposals, sorted by upvote_count, archived excluded by default, `include_archived` param, non-member → 403 |
| Get single | private proposal visible to creator and co-signers only, author name NOT in public response |
| My Proposals | returns creator's proposals with share_code and author info |
| Upvote | happy path on public proposal, upvote on private → 400, duplicate → 409, non-member → 403 |
| Withdraw upvote | happy path (count decrements), not found → 404 |
| Withdraw proposal | happy path, already archived → 400, non-creator → 403, frees active-proposal slot |
| Archive sweep | proposals public for 60+ days → archived; recently published → untouched |
| Isolation | user in HOA B cannot see HOA A's proposals; share code from HOA A returns 404 for HOA B member |

---

## Deferred

- Admin/board controls (pin, force-close) — requires a role system
- Moderation / admin force-archive — requires admin role; out of scope for self-moderation MVP
- Notifications (email on new proposal or upvote) — requires real email provider
- Proposal editing — deliberately excluded to keep co-signing semantics clean
- Pagination — not needed at MVP scale
- Linking to proxy system / agenda items — see `docs/wishlist.md` (Meeting & Agenda Model)
- Comment threads — excluded by design; deliberation happens at board meetings

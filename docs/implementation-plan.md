# HOAware Proxy Voting: Implementation Plan

**Reference:** [Proxy Voting Design Memo](proxy-voting-design-memo.md)
**Build order:** Auth → Proxy Assignment MVP → Participation Dashboard
**Target:** Solo developer, ~10–14 weeks to proxy MVP

---

## Current State of the Codebase

**What exists:**
- FastAPI backend (`api/main.py`) with routes for HOA management, document search, Q&A, and legal corpus queries
- SQLite database (`data/hoa_index.db`) with tables: `hoas`, `documents`, `chunks`, `hoa_locations`, `legal_sources`, `legal_sections`, `legal_rules`, `jurisdiction_profiles`
- Qdrant vector search with OpenAI embeddings for document retrieval
- Legal rules already extracted with proxy-specific fields: `proxy_allowed`, `proxy_form_requirement`, `proxy_directed_option`, `proxy_validity_duration`, `proxy_electronic_assignment_allowed`, etc.
- Working endpoints: `GET /law/{jurisdiction}/proxy-electronic`, `GET /law/proxy-electronic/summary`
- Vanilla HTML/CSS/JS frontend in `api/static/` — no build step, no framework
- Docker + Render deployment with persistent disk

**What does NOT exist:**
- No user accounts, authentication, or sessions
- No HOA membership or delegate system
- No proxy assignment workflow
- No e-signature integration
- No email delivery
- No participation/voting data tracking

---

## Milestone 1: Authentication & User Identity (Weeks 1–3)

### Goal
Users can register, log in, and claim membership in an HOA.

### Database Schema

Add to `hoaware/db.py` SCHEMA:

```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    verified_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_jti TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS membership_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    unit_number TEXT,
    status TEXT DEFAULT 'self_declared',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, hoa_id)
);
```

### New Files

| File | Purpose |
|------|---------|
| `hoaware/auth.py` | Password hashing (bcrypt), JWT creation/validation (HS256), token middleware, helper to extract current user from request |
| `api/static/login.html` | Login form (email + password) |
| `api/static/register.html` | Registration form (email + password + display name) |
| `api/static/dashboard.html` | Logged-in home: list my HOAs, quick links to proxy/delegate features |
| `api/static/js/auth.js` | Shared JS: store JWT in localStorage, inject Bearer header in fetch, redirect if unauthenticated |

### Modified Files

| File | Change |
|------|--------|
| `api/main.py` | Add auth endpoints, startup migration hook, Bearer token middleware |
| `hoaware/db.py` | Add new tables to SCHEMA, add CRUD functions for users/sessions/membership |
| `hoaware/config.py` | Add `JWT_SECRET`, `JWT_ALGORITHM`, `JWT_EXPIRY_DAYS` settings |
| `requirements.txt` | Add `passlib[bcrypt]>=1.7`, `python-jose[cryptography]>=3.3` |
| `api/static/index.html` | Add Login/Register nav links |
| `api/static/hoa.html` | Add "Claim membership" button (visible when logged in) |

### New API Endpoints

```
POST /auth/register        — { email, password, display_name } → { user_id, token }
POST /auth/login            — { email, password } → { token }
POST /auth/logout           — (requires JWT) → revoke token
GET  /auth/me               — (requires JWT) → { user_id, email, display_name, hoas[] }

POST /user/hoas/{hoa_id}/claim  — (requires JWT) { unit_number } → membership claim
GET  /user/hoas                  — (requires JWT) → list of HOAs user has claimed
```

### MVP Simplifications
- **Auto-verify membership claims.** No board approval workflow. Status is `self_declared` immediately. Verification tiers (property document, county records) come later.
- **Auto-verify email.** No email verification flow. Mark `verified_at` on registration. Real email verification comes with the email delivery system in Milestone 3.
- **No OAuth/SSO.** Email + password only.

### Definition of Done
A user can: register → log in → claim membership in an existing HOA → see their HOA on the dashboard → log out. JWT is stored client-side and sent on subsequent requests. Existing unauthenticated features continue to work.

---

## Milestone 2: Delegate Registration (Week 4)

### Goal
A member can register as a delegate for their HOA, with a public profile page. Other members can discover delegates.

### Database Schema

```sql
CREATE TABLE IF NOT EXISTS delegates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    bio TEXT,
    contact_email TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, hoa_id)
);
```

Note: A user can be a delegate for multiple HOAs (one row per HOA).

### New Files

| File | Purpose |
|------|---------|
| `api/static/become-delegate.html` | Form: select HOA (from memberships), write bio |
| `api/static/delegate-profile.html` | Public profile page for a delegate |

### New API Endpoints

```
POST /delegates/register           — (requires JWT + membership) { hoa_id, bio } → delegate profile
GET  /delegates/{delegate_id}       — public profile (display_name, bio, hoa_name)
GET  /hoas/{hoa_id}/delegates       — list delegates for an HOA (public)
PATCH /delegates/{delegate_id}      — (requires JWT, own profile) update bio/contact
```

### Modified Files

| File | Change |
|------|--------|
| `api/main.py` | Add delegate endpoints |
| `hoaware/db.py` | Add delegates table + CRUD |
| `api/static/hoa.html` | Show "Delegates" section listing registered delegates for the HOA |
| `api/static/dashboard.html` | Show delegate status, link to "Become a delegate" |

### Definition of Done
A logged-in member can register as a delegate for an HOA they've claimed. Their profile is visible on the HOA page. Other members can browse available delegates before assigning proxies.

---

## Milestone 3: Proxy Form Template Engine (Weeks 5–6)

### Goal
Given a jurisdiction + community type, generate a legally-compliant proxy authorization form as HTML, ready for e-signing.

### New Files

| File | Purpose |
|------|---------|
| `hoaware/proxy_templates.py` | Template engine: query legal_rules for jurisdiction requirements → render HTML form |
| `hoaware/templates/proxy_base.html` | Base Jinja2 template for proxy forms |
| `hoaware/templates/proxy_directed.html` | Directed proxy variant (includes voting instruction fields) |

### How It Works

1. **Query `legal_rules`** for the jurisdiction + community_type:
   - `proxy_allowed` — is proxy voting permitted?
   - `proxy_form_requirement` — specific form language required?
   - `proxy_directed_option` — directed/undirected rules
   - `proxy_validity_duration` — max duration (11 months, 1 year, etc.)
   - `proxy_electronic_assignment_allowed` — e-signature OK?
   - `proxy_holder_restrictions` — must be member? caps?
   - Any required disclosures or witness requirements (DC)

2. **Render an HTML form** with:
   - State-specific header and statutory citation
   - Grantor name, address, unit number
   - Delegate name, relationship to HOA
   - Meeting date (if specified) or general authorization with duration
   - Voting instructions section (for directed proxies)
   - Required disclosures per state
   - Signature block with date
   - Revocation instructions

3. **Return the rendered HTML** — stored in `proxy_assignments.form_html` when a proxy is created.

### Template Strategy (MVP)

Start with **one base template** that conditionally includes state-specific sections. Don't build 51 separate templates. The legal_rules table already encodes the variations — the template engine reads them and adapts.

Priority states for testing (based on existing legal corpus coverage + HOA density):
1. **California** — member-only proxy holders, explicit rules
2. **Florida** — detailed proxy law, condo vs HOA distinction
3. **Texas** — common HOA state, moderate proxy rules
4. **Colorado** — well-defined proxy rules, HOA oversight body
5. **Virginia** — detailed POAA proxy provisions

### New API Endpoints

```
GET /proxy-templates/preview?jurisdiction=CA&community_type=hoa  — preview form template (no user data)
```

### Definition of Done
`render_proxy_form(jurisdiction, community_type, grantor, delegate, meeting_date)` returns valid HTML for at least 5 states. The form includes all legally required elements per the state's rules. A preview endpoint shows what the form looks like before a user commits.

---

## Milestone 4: Proxy Assignment & E-Signature MVP (Weeks 7–9)

### Goal
A resident can create a proxy assignment, sign it electronically, and have it recorded with a full audit trail. This is the core transaction.

### Database Schema

```sql
CREATE TABLE IF NOT EXISTS proxy_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grantor_user_id INTEGER NOT NULL REFERENCES users(id),
    delegate_user_id INTEGER NOT NULL REFERENCES users(id),
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    jurisdiction TEXT NOT NULL,
    community_type TEXT NOT NULL,
    direction TEXT DEFAULT 'directed',
    voting_instructions TEXT,          -- JSON: agenda items + votes (for directed)
    for_meeting_date DATE,
    expires_at DATE,                   -- computed from state duration rules
    status TEXT DEFAULT 'draft',       -- draft, signed, delivered, acknowledged, revoked, expired
    form_html TEXT,                    -- rendered template at time of creation
    signed_pdf_path TEXT,              -- path to signed PDF (future: Documenso)
    signed_at TIMESTAMP,
    delivered_at TIMESTAMP,
    acknowledged_at TIMESTAMP,
    revoked_at TIMESTAMP,
    revoke_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS proxy_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_id INTEGER NOT NULL REFERENCES proxy_assignments(id),
    action TEXT NOT NULL,
    actor_user_id INTEGER REFERENCES users(id),
    details TEXT,                      -- JSON: IP, user agent, error messages
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### New Files

| File | Purpose |
|------|---------|
| `hoaware/proxy.py` | Core proxy logic: create, sign, deliver, revoke, expire, audit |
| `hoaware/esign.py` | E-signature abstraction layer. MVP: local click-to-sign. Future: Documenso API |
| `hoaware/email_service.py` | Email delivery abstraction. MVP: log to console + store as "delivered." Future: SMTP/SendGrid |
| `api/static/assign-proxy.html` | Multi-step form: select HOA → select delegate → set direction → review → sign |
| `api/static/proxy-sign.html` | E-signature page: display form read-only, "I agree and sign" button |
| `api/static/my-proxies.html` | List user's outgoing proxies with status badges and revoke action |
| `api/static/delegate-dashboard.html` | For delegates: incoming proxies, counts, statuses |

### New API Endpoints

```
POST /proxies                        — (requires JWT + membership) create proxy assignment
GET  /proxies/{proxy_id}             — (requires JWT, grantor or delegate) view proxy details
POST /proxies/{proxy_id}/sign        — (requires JWT, grantor only) e-sign the proxy
POST /proxies/{proxy_id}/deliver     — (requires JWT, grantor only) trigger email delivery to board
POST /proxies/{proxy_id}/revoke      — (requires JWT, grantor only) { reason } → revoke
GET  /proxies/mine                   — (requires JWT) list outgoing proxies (as grantor)
GET  /proxies/delegated              — (requires JWT, delegate) list incoming proxies
GET  /hoas/{hoa_id}/proxy-stats      — public: aggregate proxy count for HOA (no PII)
```

### Proxy Lifecycle

```
draft → signed → delivered → acknowledged
                           ↘ revoked
       ↘ expired (cron or on-read check)
```

1. **Create (draft):** Grantor selects delegate + direction. Form is rendered via template engine. Status = `draft`.
2. **Sign:** Grantor reviews form on the sign page. Clicks "I agree and sign." MVP: records timestamp + IP as signature evidence. Status = `signed`. Audit log entry.
3. **Deliver:** Signed form is "sent" to the board. MVP: logs the email, marks `delivered_at`. Future: actually sends email with PDF attachment. Status = `delivered`. Audit log entry.
4. **Acknowledge (optional):** Board confirms receipt. MVP: manual status update by delegate. Future: board portal.
5. **Revoke:** Grantor can revoke at any time before the meeting. Status = `revoked`. Audit log entry. Notification queued to delegate.
6. **Expire:** Background check or on-read: if `expires_at < now`, status = `expired`.

### E-Signature MVP (Local Click-to-Sign)

Instead of integrating Documenso immediately, the MVP implements a simple but legally defensible e-sign flow:

1. Display the full proxy form as read-only HTML
2. Show consent language: "By clicking 'Sign,' I affirm that I am [Grantor Name], I intend this to constitute my electronic signature under the ESIGN Act (15 U.S.C. § 7001) and my state's UETA, and I authorize [Delegate Name] to vote my proxy as described above."
3. Record: timestamp, user_id, IP address, user agent
4. Generate a "signature receipt" stored in the audit log

This satisfies ESIGN Act requirements (intent to sign + consent to electronic process). The Documenso integration (Milestone 6) adds a more polished UX and PDF generation, but the legal validity is the same.

### Email Delivery MVP (Stub)

```python
# hoaware/email_service.py
def deliver_proxy_to_board(proxy_id: int) -> bool:
    """
    MVP: Log the delivery event. Do not actually send email.
    The delegate prints/forwards the form manually.
    """
    proxy = get_proxy(proxy_id)
    log.info(f"PROXY DELIVERY: proxy_id={proxy_id}, "
             f"grantor={proxy['grantor_email']}, "
             f"delegate={proxy['delegate_email']}, "
             f"hoa={proxy['hoa_name']}")
    update_proxy_status(proxy_id, 'delivered')
    return True
```

The stub records the event in the audit log and marks the proxy as delivered. The delegate is responsible for actually delivering it (print + bring to meeting, or email the board directly). Real email delivery comes in Milestone 6.

### Definition of Done
A logged-in resident can: select a delegate → choose directed/undirected → review the state-specific form → click to sign → see status update to "signed" → trigger "delivery" → see status update to "delivered." The delegate can view incoming proxies on their dashboard. The grantor can revoke. Audit trail captures every state change.

---

## Milestone 5: Frontend Polish & End-to-End Testing (Weeks 10–11)

### Goal
The full proxy flow works smoothly in a browser. All pages are styled consistently with existing HOAware pages. Error handling is solid.

### Work Items

**Frontend consistency:**
- Match existing color scheme (blue accent #1662f3, background #eef5ff)
- Use existing fonts (Manrope body, Space Grotesk headings)
- Responsive layout (works on mobile — delegates may use phones at meetings)
- Navigation: add auth-aware nav bar to all pages (Login/Register when logged out, Dashboard/My Proxies when logged in)

**Error handling:**
- Form validation (client-side + server-side)
- Graceful error messages for: duplicate membership claims, proxy for wrong HOA, expired proxy, already-revoked proxy
- Rate limiting on auth endpoints (prevent brute force)

**Testing:**
- `tests/test_auth.py` — register, login, JWT validation, logout
- `tests/test_membership.py` — claim, duplicate, list
- `tests/test_delegates.py` — register, profile, list per HOA
- `tests/test_proxy.py` — full lifecycle: create → sign → deliver → revoke
- `tests/test_proxy_templates.py` — form generation for 5 priority states
- Manual end-to-end test: walk through the full flow in a browser

**Deployment:**
- Update `Dockerfile` for new dependencies
- Update `render.yaml` with new environment variables (`JWT_SECRET`)
- Database migration script that adds new tables without disrupting existing data
- Health check covers new tables

### Definition of Done
`pytest tests/ -v` passes. A manual walkthrough of register → claim HOA → become delegate → assign proxy → sign → deliver → revoke works without errors. Deployment to Render succeeds.

---

## Milestone 6: Real E-Signatures & Email Delivery (Weeks 12–14)

### Goal
Replace the MVP stubs with production-grade e-signature and email delivery.

### E-Signature: Self-Hosted Documenso

**Setup:**
- Add Documenso as a Docker service in `docker-compose.yml`
- Configure Documenso API URL and API key in settings
- On Render: either run Documenso as a separate service or use Documenso Cloud free tier during early adoption

**Integration (`hoaware/esign.py`):**
- `create_document(form_html, grantor_email)` → Documenso document ID + signing URL
- `get_signing_status(document_id)` → signed/pending/expired
- `get_signed_pdf(document_id)` → download signed PDF
- Webhook handler: Documenso calls back when document is signed → update proxy status

**Flow change:**
- Instead of local click-to-sign, grantor is redirected to Documenso signing page
- Documenso handles signature capture, audit trail, and PDF generation
- Signed PDF is stored and attached to the delivery email

### Email Delivery

**Setup:**
- Free tier: Postmark (100 emails/month free) or Resend (100 emails/day free)
- Self-hosted fallback: Postfix on the Docker host
- Configure SMTP credentials in settings

**Integration (`hoaware/email_service.py`):**
- `deliver_proxy_to_board(proxy_id)` → sends email with signed PDF to the board's contact email
- `notify_grantor(proxy_id, event)` → sends status notification to grantor (signed, delivered, revoked)
- `notify_delegate(proxy_id, event)` → sends notification to delegate (new proxy, revocation)

**Board contact email:** Stored per HOA. Initially entered by the delegate or uploader. Future: scraped from uploaded CC&Rs or management company websites.

### Definition of Done
A proxy assignment generates a real Documenso signing link. The grantor signs via Documenso. The signed PDF is emailed to the board. Status updates flow through webhooks.

---

## Milestone 7: Participation Dashboard (Weeks 15–17)

### Goal
Phase 2 from the design memo: show historical voting participation per HOA, calculate the "magic number."

### Database Schema

```sql
CREATE TABLE IF NOT EXISTS participation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hoa_id INTEGER NOT NULL REFERENCES hoas(id),
    meeting_date DATE NOT NULL,
    meeting_type TEXT,                 -- annual, special, board
    total_units INTEGER,              -- total eligible voters
    votes_cast INTEGER,               -- ballots + proxies counted
    quorum_required INTEGER,          -- from CC&Rs
    quorum_met BOOLEAN,
    source_document_id INTEGER REFERENCES documents(id),  -- link to uploaded minutes
    entered_by_user_id INTEGER REFERENCES users(id),
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(hoa_id, meeting_date, meeting_type)
);
```

### New Files

| File | Purpose |
|------|---------|
| `hoaware/participation.py` | CRUD for participation records, magic number calculation |
| `api/static/participation.html` | Dashboard: table of historical meetings, chart of participation over time, magic number callout |
| `api/static/add-participation.html` | Form to manually enter meeting participation data |

### New API Endpoints

```
POST /hoas/{hoa_id}/participation    — (requires JWT + membership) add participation record
GET  /hoas/{hoa_id}/participation    — public: list participation records for HOA
GET  /hoas/{hoa_id}/magic-number     — public: calculated magic number based on historical participation
```

### Magic Number Calculation

```python
def calculate_magic_number(hoa_id: int) -> dict:
    """
    Based on historical participation:
    - Average votes cast across recent meetings
    - Total units in the HOA
    - Quorum threshold (from CC&Rs or state default)

    Returns:
    - average_participation_rate: e.g., 0.15 (15%)
    - average_votes_cast: e.g., 30
    - total_units: e.g., 200
    - proxies_to_swing: ceil(average_votes_cast / 2) + 1 - estimated_incumbent_support
    - confidence: low/medium/high based on data points
    """
```

### MVP Approach
- **Manual data entry first.** A member enters: meeting date, total units, votes cast, quorum met. This is fast to build and covers the 80% case.
- **Future enhancement:** Upload meeting minutes → AI extracts participation data automatically (the existing Q&A pipeline + a specialized prompt can do this).

### Definition of Done
An HOA page shows a "Participation" tab with historical meeting data, a participation rate chart, and the magic number. A member can add new records. The magic number is displayed prominently as motivation for proxy organizing.

---

## Milestone 8: Hardening & Launch Prep (Weeks 18–20)

### Security
- Rate limiting on all auth and proxy endpoints
- CSRF protection on form submissions
- Input sanitization on all user-provided text fields
- PII scrubbing: ensure public endpoints never leak grantor addresses or unit numbers
- Data retention policy: auto-expire proxy records after meeting date + 90 days (configurable)
- DMCA takedown email + documented procedure (per design memo §11.5)

### Terms of Service & Legal
- ToS page (`api/static/terms.html`) with disclaimers per design memo §11.5
- Privacy policy page
- Checkbox on registration: "I agree to the Terms of Service"
- "Not legal advice" disclaimer on every proxy form

### Monitoring
- Request logging (structured JSON logs)
- Error alerting (Sentry free tier or similar)
- Uptime monitoring on Render

### Documentation
- Update README with new features, API docs, deployment instructions
- Contributor guide for open-source participation

### Definition of Done
The platform is ready for real users. Legal disclaimers are in place. Monitoring is active. A new contributor can clone the repo and run the full stack locally with `docker-compose up`.

---

## File Map: New vs Modified

### New Python Modules (in `hoaware/`)
1. `auth.py` — authentication, JWT, password hashing
2. `proxy.py` — proxy assignment lifecycle
3. `proxy_templates.py` — state-specific form generation
4. `esign.py` — e-signature abstraction (stub → Documenso)
5. `email_service.py` — email delivery abstraction (stub → SMTP)
6. `participation.py` — participation records and magic number
7. `migrations.py` — schema migration runner

### New Frontend Pages (in `api/static/`)
1. `login.html`
2. `register.html`
3. `dashboard.html`
4. `become-delegate.html`
5. `delegate-profile.html`
6. `assign-proxy.html`
7. `proxy-sign.html`
8. `my-proxies.html`
9. `delegate-dashboard.html`
10. `participation.html`
11. `add-participation.html`
12. `terms.html`
13. `js/auth.js` — shared auth utilities

### Modified Files
1. `api/main.py` — all new endpoints, middleware, startup hook
2. `hoaware/db.py` — new tables, CRUD functions
3. `hoaware/config.py` — new settings (JWT, email, Documenso)
4. `requirements.txt` — new dependencies
5. `Dockerfile` — updated for new deps
6. `docker-compose.yml` — add Documenso service (Milestone 6)
7. `render.yaml` — new env vars
8. `api/static/index.html` — auth-aware nav
9. `api/static/hoa.html` — delegates section, participation tab, proxy stats

### New Test Files (in `tests/`)
1. `test_auth.py`
2. `test_membership.py`
3. `test_delegates.py`
4. `test_proxy.py`
5. `test_proxy_templates.py`
6. `test_participation.py`

---

## Dependency Graph

```
Milestone 1 (Auth)
    ↓
Milestone 2 (Delegates)
    ↓
Milestone 3 (Templates)  ← reads from existing legal_rules table
    ↓
Milestone 4 (Proxy Assignment MVP)  ← depends on 1, 2, 3
    ↓
Milestone 5 (Polish & Testing)
    ↓
Milestone 6 (Real E-Sign & Email)  ← replaces stubs from M4
    ↓
Milestone 7 (Participation Dashboard)  ← independent of M6, can start after M5
    ↓
Milestone 8 (Hardening & Launch)
```

Milestones 6 and 7 can be worked in parallel — they have no dependencies on each other.

---

## Post-Code Manual Steps (Launch Checklist)

All 8 milestones are code-complete and deployed. These items require manual action outside the codebase.

### Render Environment Secrets (set via dashboard or Render API upsert script)

| Key | Where to get it | Priority |
|-----|----------------|----------|
| `RESEND_API_KEY` | resend.com → API Keys | Required for real email delivery |
| `DOCUMENSO_API_KEY` | app.documenso.com or self-hosted | Required for Documenso e-sign |
| `DOCUMENSO_WEBHOOK_SECRET` | Documenso → Webhook settings | Required for Documenso e-sign |
| `OPENAI_API_KEY` | platform.openai.com | Already set ✓ |
| `JWT_SECRET` | Already set (Mar 2026) ✓ | Already set ✓ |

When RESEND_API_KEY is set, also change `EMAIL_PROVIDER` from `stub` to `resend` on Render.

### Email Delivery Activation

1. Sign up at [resend.com](https://resend.com) (100 emails/day free tier)
2. Add and verify the `hoaware.app` domain in Resend (add DNS TXT + MX records)
3. Set `RESEND_API_KEY` on Render
4. Set `EMAIL_PROVIDER=resend` on Render
5. Trigger a test proxy delivery to confirm email arrives

### Documenso E-Signature Activation (optional — click-to-sign works without it)

**Option A — Documenso Cloud:**
1. Sign up at app.documenso.com
2. Get API key from settings
3. Set `DOCUMENSO_API_KEY` on Render
4. Set up webhook: URL = `https://<your-render-url>/webhooks/documenso`, copy the secret
5. Set `DOCUMENSO_WEBHOOK_SECRET` on Render

**Option B — Self-hosted Documenso:**
1. Run `docker-compose --profile esign up` locally to test
2. Deploy Documenso as a separate Render service or VPS
3. Set `DOCUMENSO_API_URL` to point at your instance

### DNS / Domain

- Point a custom domain at the Render service (e.g. hoaware.app)
- Add domain in Render dashboard → Settings → Custom Domains
- Render provisions TLS automatically

### GCP Service Account (for Document AI)

- The key file `hoaware-598872615131.json` must be uploaded to Render as a Secret File at path `/etc/secrets/gcp-service-account.json`
- Render dashboard → Environment → Secret Files

### First-Time Database

On first deploy to a fresh Render disk, the `lifespan` startup hook runs `db.SCHEMA` automatically — no manual migration needed. The health check at `/healthz` will return 503 until the DB is initialized (which happens on first request).

### Monitoring (optional but recommended)

- **Sentry**: sign up at sentry.io (free tier), add `SENTRY_DSN` env var, add `sentry-sdk[fastapi]` to requirements.txt and initialize in `api/main.py`
- **Render uptime alerts**: enable in Render dashboard → Notifications
- **Log drain**: Render can forward logs to Datadog, Papertrail, etc. via dashboard → Log Streams

### Legal / Operational

- Register `dmca@hoaware.com` and `privacy@hoaware.com` email addresses (or forwards)
- Review Terms of Service and Privacy Policy with a lawyer before public launch
- Set up a way to receive and respond to DMCA takedown requests

---

## What's NOT in This Plan (Future Work)

These are deferred to post-launch based on user feedback and adoption:

- **Physical mail integration** (Lob API for postcards/letters) — Phase 3b
- **Voting instruction builder** (agenda item UI, candidate matching) — Phase 3b
- **Board verification of membership claims** — when/if boards engage with the platform
- **County records API for identity verification** — Phase 3b
- **Agent-ready REST API and webhooks** — Phase 3c
- **AI extraction of participation data from meeting minutes** — enhancement to M7
- **Escalation toolkit** (demand letter templates, complaint filing guides) — Phase 3b
- **Nonprofit incorporation and fiscal sponsorship** — operational, not code

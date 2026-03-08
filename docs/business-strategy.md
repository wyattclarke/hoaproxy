# HOAware — Business Strategy

*Written March 2026*

---

## The Insight

Most HOA residents who want to challenge their board are outgunned and alone. They don't understand their own governing documents, don't know what state law requires, don't know how to run a proxy campaign, and don't know how to write a proposal that their neighbors will take seriously. The board has a management company and sometimes an attorney. The resident has nothing.

HOAware exists to close that gap.

---

## The Platform: Open Source, Non-Profit

The HOAware website — document Q&A, proxy voting coordination, state law corpus, HOA lookup — is fully open source under a permissive license. It is maintained as a non-profit public good.

This is the right structure because:
- The technology is not the defensible asset; the service built on top of it is
- Open source builds trust with residents who are uploading sensitive governing documents
- It invites community contribution — legal corpus updates, new state coverage, template improvements
- It positions HOAware as infrastructure for the broader HOA accountability ecosystem rather than a product competing for the same residents' wallets

The open source project has no expectation of direct revenue. It can pursue foundation grants and civic tech funding on its merits.

---

## The Business: Resident Advocacy Services

A separate, for-profit (or fee-for-service non-profit) business operates on top of the platform, providing professional services to individual residents who want to win something specific.

The customer is a pissed-off resident with a goal and a deadline. They are not a software user — they are a client. Their experience is entirely human: they contact the service, explain their situation, and a campaign happens on their behalf.

The software is invisible back-office infrastructure. It is what makes the service economically viable at low price points.

---

## The Service Offering

### 1. Proposal Drafting

The resident has an idea — cap the management fee, change the pet policy, require board meeting recordings. The idea may be reasonable but the drafting is likely unusable: wrong procedure, conflicts with existing bylaws, or simply not credible to neighbors because it looks amateur.

The service:
- Reviews the resident's governing documents against their state law profile
- Drafts the proposal in language that is procedurally valid, internally consistent, and legally sound
- Returns a document with a "prepared by" professional stamp

The stamp matters as much as the content. Neighbors who might ignore one angry resident's proposal will read something that looks like it was professionally prepared. It changes the social dynamic of the vote.

Pricing: flat fee, mostly templated work with attorney review sign-off. High margin.

### 2. Proxy Campaign Management

The resident needs signatures before a meeting deadline. Collecting proxies from HOA neighbors is a logistics and persuasion problem — most residents who would support a proposal simply won't get around to signing without being asked directly and repeatedly.

The service:
- Pulls the applicable state law proxy requirements (form, delivery, timing, revocation)
- Reviews the specific HOA's bylaws for additional requirements
- Generates a legally compliant proxy form for the situation
- Assists the client in obtaining the HOA membership roster (which residents are legally entitled to in most states — the legal corpus identifies the exact mechanism per state)
- Designs and executes a physical mail campaign: cover letter, proxy form, return envelope
- Tracks responses, runs follow-up waves, reports progress to the client
- Delivers completed proxies before the deadline

Pricing: flat intake fee plus per-unit postage cost. A 150-unit HOA campaign might cost the client $500-700 total. If they're fighting a $10,000 special assessment, this is an obvious purchase.

### 3. Combined Campaign

Proposal drafting plus proxy campaign as a bundled service. Full-service resident advocacy from idea to vote.

---

## The Legal Question

The primary risk is unauthorized practice of law. The line:

**Safe:** "Here is what the statute requires. Here is a form that meets those requirements."

**Unsafe:** "Your board broke the law. You will win."

The service operates as a legal document preparation service, not as legal counsel. This is a recognized category in most states. The practical protection:

- Engage one HOA attorney on retainer to review and sign off on all form templates and the service methodology
- That attorney is available for escalation when clients need actual legal advice
- The service never opines on outcomes, only on procedures and documents

A single conversation with an attorney familiar with the state's unauthorized practice of law rules is required before taking the first paying client. This is a one-time $300 investment.

A formal law firm partnership is probably not the right structure — law firms want litigation feeders, and this service's value is in resolving things cheaply without litigation. A retained solo HOA attorney is a cleaner arrangement.

---

## Technical Hooks Needed in the Open Source Platform

The open source platform needs the following additions to support the business operating on top of it:

**Operator role:** A way for a service operator to manage a proxy campaign on behalf of a client — acting within a client's HOA context without requiring the client to navigate the UI themselves.

**PDF/print output:** Proxy forms and proposals need to be exportable as print-ready PDFs suitable for physical mail campaigns.

**Mailing list export:** Address data formatted for upload to a physical mail service (Lob, PostGrid, or similar).

**Audit log export:** A client-readable record of what was sent to whom and when — delivery confirmation, timestamps, the legal record of the campaign. Exportable without requiring the client to use the app.

**Intake CTA:** A visible "get professional help" path in the UI for residents who want the service rather than the self-serve tool.

These hooks are workflow and output features, not business logic. The business judgment — drafting, attorney relationship, campaign strategy — stays entirely outside the open source codebase.

---

## What This Is Not

- Not a law firm or legal advice service
- Not HOA management software (that market serves boards, not residents)
- Not a subscription SaaS (HOA activity is episodic — annual meetings, special assessments, recall votes)
- Not dependent on software adoption by the end client

---

## The Market

The customers are self-selecting: only residents with a specific grievance, a realistic goal, and enough motivation to pay will become clients. That is a small fraction of all HOA residents — but there are approximately 360,000 HOAs in the US. Even rare events at that scale produce a serviceable market.

The service is particularly well-suited to remote delivery. There is no geographic constraint on which HOAs can be served, because everything — document review, form generation, physical mail — can be executed remotely. A mail service like Lob handles physical delivery from anywhere.

---

## Near-Term Steps

1. One attorney conversation — clarify UPL boundaries in the target state before any client work
2. Build the operator role and PDF export hooks into the open source platform
3. Set up a physical mail service account (Lob or PostGrid)
4. Take the first client at low or no cost to validate the end-to-end workflow
5. Open source the platform once the service business has validated the model and the hooks are in place

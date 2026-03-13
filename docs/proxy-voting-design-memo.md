# HOAproxy Proxy & Absentee Voting: Design Memo

**Author:** Claude (for Wyatt Clarke)
**Date:** 2026-03-01
**Status:** Draft for discussion

---

## 1. The Core Insight

HOA elections are typically decided before the annual meeting happens. Boards mail postcards that double as proxy forms, defaulting the proxy holder to the board president. Residents who don't attend (the vast majority) unknowingly hand their votes to incumbents. The proxy instrument — designed to ensure quorum — has become the mechanism of incumbent entrenchment.

HOAproxy can invert this dynamic. The same proxy laws that let boards collect default proxies also let *any resident* collect directed proxies from their neighbors. Nothing in the law privileges the board's proxy solicitation over anyone else's. A platform that makes it trivially easy for residents to assign directed proxies to a reform-minded neighbor — months before the meeting — can shift the balance of power without changing a single law.

This is not hypothetical. The legal infrastructure already exists in ~45 states. What's missing is the coordination mechanism.

## 2. What the Law Actually Requires

### 2.1 The ESIGN/UETA Baseline

The federal ESIGN Act (2000) and the Uniform Electronic Transactions Act (adopted by 49 states; NY has its own equivalent) establish that electronic signatures carry the same legal weight as wet-ink signatures for virtually all transactions. A proxy is a written authorization — squarely within ESIGN/UETA scope.

This means: an e-signed proxy document, delivered by email, is a legally valid proxy in every state that permits proxies — *unless* that state's HOA statute imposes additional requirements that ESIGN/UETA cannot satisfy.

### 2.2 State-by-State Landscape

Based on the legal corpus (all 51 jurisdictions researched), states fall into tiers:

**Tier 1 — Green light (~30 states).** Proxy voting permitted. No restrictions on electronic form/delivery. "Duly executed" means signed, and ESIGN/UETA covers e-signatures. The platform can generate, e-sign, and email proxies with high confidence. Includes: AL, AK, AR, CO, CT, DE, GA, IA, KS, KY, LA, ME, MI, MN, MO, MS, MT, NE, NJ, NM, NY, NC, ND, OH, OK, OR, PA, RI, SC, SD, TN, TX, VT, WV, WI, WY.

**Tier 2 — Green light with guardrails (~8 states).** Proxies permitted but with specific procedural requirements the platform must encode:

- **California:** Proxyholder must be an association member. Directed proxy voting instructions must be on a *separate page* detached from the proxy form itself.
- **Florida (HOA):** Proxies valid only for the specific meeting + 90 days. General proxies OK for non-election business; limited proxies for condo elections.
- **Indiana:** Explicitly blesses e-signatures and electronic delivery (mail, fax, email, other electronic means). The cleanest statute for HOAproxy's model.
- **Hawaii:** Proxy valid only for the specific meeting. Any person may be proxyholder (not limited to members).
- **Idaho:** 50% cap on votes any one person can hold. Max 3-year duration.
- **New Hampshire:** 10% cap on proxies one person can hold in 20+ unit condos. Terminates at adjournment.
- **Maryland:** Undirected proxies count only for quorum and general business. Director elections require directed proxies or in-person votes.
- **DC:** Proxy signatures must be *witnessed* — a person who signs their full name and address. This is the hardest requirement to satisfy electronically.

**Tier 3 — Absentee ballot substitution (1 state).**

- **Arizona:** Proxies are **banned** after the developer control period ends. Associations must provide absentee ballots instead. AZ does allow email/fax/electronic delivery of absentee ballots, so the platform can still function — but the instrument is an absentee ballot, not a proxy.

**Tier 4 — Proxies restricted for board elections (~3 states).**

- **Nevada:** Board election proxies prohibited entirely. Strict limits on who can hold proxies for other matters (immediate family, tenant, unit owner, authorized delegate).
- **Illinois (CICAA):** No proxies for board elections. Permitted for other matters.
- **Florida (condo, residential):** No proxies for board elections. General proxies OK for other business.

In Tier 4 states, HOAproxy's proxy feature is limited to non-election votes (budget approvals, rule changes, special assessments). For board elections specifically, the platform may need to pivot to get-out-the-vote messaging instead.

### 2.3 Duration and Renewal

Most states cap proxy validity. The platform must track expiration and prompt renewal:

| Duration | States |
|----------|--------|
| 11 months | AL, CO, ID, IL, MS, NE, WA |
| 1 year | AK, DE, RI |
| 180 days | MD, WI |
| 90 days / specific meeting | FL (HOA), IA, NJ |
| At adjournment | NH |
| Rolling annual until revoked | OK |

**Design implication:** The platform should default to the shortest legally permissible duration for the user's state and prompt renewal well before expiration. For "specific meeting" states, the proxy must name the meeting.

### 2.4 Revocation

Nearly all states require **actual notice of revocation to the presiding officer** of the meeting. Simply telling the proxy holder you've changed your mind is insufficient. The platform should:

1. Generate a formal revocation document.
2. Deliver it to the same address/person who received the original proxy.
3. Confirm delivery and log it.

## 3. The Product: How It Works

### 3.1 User Flow

**For the proxy grantor (the homeowner assigning their vote):**

1. **Find your community.** Resident navigates to their HOA on HOAproxy (already exists in the "Understand" phase).
2. **Choose your delegate.** The resident selects from neighbors who have volunteered as community delegates. The platform displays who is collecting proxies, how many they hold, and optionally their stated platform/positions. Only residents of the community can serve as delegates.
3. **Set your instructions.** For directed proxies (recommended, and required for board elections in MD and de facto essential everywhere): the resident specifies how they want their vote cast on known agenda items. For items not yet known, they can choose "delegate's discretion" or "abstain."
4. **E-sign the proxy.** The platform generates a state-compliant proxy form. The resident signs electronically via an embedded e-signature provider. The form includes all legally required elements for their jurisdiction: date, meeting identification (if required), proxy holder name, signature, and witness line (if DC).
5. **Delivery to the board.** HOAproxy emails the signed proxy to the HOA board/management company from a verified address. For communities where the board is known to reject email delivery, the platform triggers a physical mailing via a postcard/letter service.
6. **Confirmation and tracking.** The resident receives confirmation. The proxy appears on HOAproxy's public dashboard (PII removed) as a counted assignment. If the board acknowledges receipt, that status is updated.

**For the proxy holder (the neighbor attending the meeting):**

1. **Register as a delegate.** A resident volunteers to attend the meeting and collect directed proxies from neighbors.
2. **View your proxy book.** Before the meeting, the delegate sees all directed proxies assigned to them with specific voting instructions per agenda item.
3. **Attend and vote.** The delegate brings printed copies of all proxy forms to the meeting and presents them to the inspector of elections / presiding officer.
4. **Report results.** After the meeting, the delegate reports how votes were cast, which HOAproxy publishes.

### 3.2 What Gets Published

HOAproxy's public dashboard for each community shows:

- Total proxies assigned through the platform (count, not names)
- Distribution: how many proxies each delegate holds (by name — delegates are public figures by choice)
- Aggregate voting intentions on known ballot items (X votes for Candidate A, Y for Candidate B)
- Timeline: when proxies were filed vs. the board's proxy solicitation timeline

This transparency is the strategic product. Even if HOAproxy-organized proxies don't outnumber board proxies, the *visibility* of an organized alternative changes the dynamics of what was previously a one-party election.

### 3.3 The Community Delegate Model

HOAproxy's proxy holder is always a **community delegate** — a *resident* of the community who volunteers to attend the meeting and carry directed proxies from their neighbors. HOAproxy facilitates the logistics, but the delegate is a neighbor, not a platform employee or outside representative. Sending a non-member to vote on behalf of residents is not part of HOAproxy's model.

This is the right design for several reasons:

- **Legal compliance everywhere.** Multiple states restrict who can hold a proxy — CA requires the holder to be an association member, NV limits holders to immediate family, tenants, and fellow unit owners, NH caps one holder at 10% of votes in larger condos. A resident delegate satisfies every state's restrictions by definition.
- **Political legitimacy.** A neighbor organizing their community is a fundamentally different story than an outside platform inserting itself into a local election. The delegate is accountable to the community, not to HOAproxy. This framing is harder for boards to attack.
- **Sustainability.** HOAproxy doesn't need to recruit, train, or send staff to meetings in thousands of communities. The platform scales through *enabling* local leaders, not *being* the local leader.

The platform can support multiple delegates per community. A resident might assign their proxy to Delegate A for the board election and Delegate B for budget matters. The math of who holds what gets complex — the platform handles it.

## 4. Service Architecture

### 4.1 E-Signature

**Recommendation: Self-hosted Documenso** — an open-source e-signature platform (AGPL-licensed, Next.js/Prisma stack) that can run alongside HOAproxy on the same infrastructure. Variable cost per signature: **$0.**

HOAproxy is an open-source nonprofit project. Per-signature fees from commercial providers (DocuSign, HelloSign, SignNow) would create unsustainable variable costs with no revenue model to absorb them. Self-hosting eliminates this entirely.

**Why self-hosted e-signatures are legally sufficient:**

Under ESIGN/UETA, what makes an electronic signature valid is not the vendor — it's four elements: (1) intent to sign, (2) consent to do business electronically, (3) attribution connecting the signature to the signer and document, and (4) reliable record retention. A self-hosted platform satisfies all four. The audit trail (timestamp, IP address, document hash, signer identity) is generated and stored by HOAproxy's own infrastructure. There is no legal requirement to use a commercial e-signature provider.

The practical risk of a board challenging the *method* of signature (as opposed to the *identity* of the signer or the *content* of the proxy) is very low. Boards that reject valid proxies are already breaking the law; the signature technology is unlikely to be the actual dispute. And if it ever is, the audit trail from Documenso is no less defensible than DocuSign's — it records the same metadata.

**Open-source options evaluated:**

- **Documenso** (recommended): Fully open-source, self-hostable with full API access. Built on Next.js/Prisma — close to HOAproxy's existing stack. Supports templates, embedded signing, and audit certificates. ~9k GitHub stars. No feature gates on self-hosted deployments.
- **DocuSeal:** AGPL-licensed, polished UI. But API/embedding access requires a $20/month Pro seat even for self-hosted — a recurring cost that undermines the zero-variable-cost goal.
- **OpenSign:** Fully free including cloud-hosted version. Less mature API. Viable fallback.

**Fallback option — minimal DIY signing flow:** If maintaining a separate signing service proves burdensome, HOAproxy could implement a lightweight signing flow directly: user clicks "I agree to assign my proxy" → system records authenticated identity + IP + timestamp + user-agent → generates proxy PDF → computes SHA-256 hash → stores hash + audit metadata immutably → delivers signed PDF + audit certificate to board. This is legally valid under ESIGN/UETA. The trade-off is slightly less visual polish (no drawn/typed signature image) and a thinner audit paper trail if challenged.

Requirements the chosen solution must satisfy:
- Embedded signing (the user signs within HOAproxy, not redirected to a third-party site)
- Template-based: HOAproxy maintains a proxy form template per state, with merge fields for names, dates, meeting info, and voting instructions
- Audit trail: tamper-evident certificate of completion with timestamps, IP address, and document hash
- Witness co-signing (for DC): the platform can add a second signer field

**Cost:** $0 variable. Only cost is the server resources to run Documenso, which are marginal on existing infrastructure.

### 4.2 Email Delivery

**Recommendation: Self-hosted email or a free-tier transactional service.**

The emails HOAproxy sends to boards are legally significant documents, not marketing. Deliverability and proof of delivery matter. Options in order of preference:

- **Self-hosted (Postfix/DKIM on HOAproxy's server):** Zero cost. Requires DNS configuration (SPF, DKIM, DMARC) for deliverability. Adequate for low volume. Risk: management company spam filters may be aggressive toward unknown sending domains.
- **Free-tier transactional email:** Services like Postmark (free for first 100 emails/month), Resend (3,000 free/month), or Brevo (300 free/day) provide better deliverability with no cost at HOAproxy's likely volume. A single community generates at most 20–50 proxy delivery emails per election cycle.
- **Fallback to Gmail/personal email:** In the worst case, the platform could generate the signed proxy PDF and provide it to the resident to email themselves. This is clunkier but costs nothing and sidesteps all deliverability issues — the email comes from the homeowner, not an unknown platform domain.

Each proxy delivery email should:
- Come from a verified HOAproxy domain (e.g., `proxies@hoaware.com`) or the resident's own email
- Attach the signed proxy PDF
- Include a plain-text summary: "This email contains a proxy form signed by [Owner Name] on [Date], assigning their vote to [Delegate Name] for [Meeting Name/Date]."
- Request read receipt (informational, not legally required)
- Log delivery status (delivered, bounced, opened) in HOAproxy's database

**Fallback:** If the management company's email bounces or goes unacknowledged after 7 days, escalate to physical mail.

**Cost:** $0 at expected volumes.

### 4.3 Physical Mail (Postcard / Letter Service)

**Approach: User-paid physical mail, with platform-generated documents.**

Physical mail is the one category where real variable costs exist. HOAproxy's role is to *generate the documents* (proxy delivery letters, solicitation postcards, pre-printed forms); the *mailing cost* is borne by the delegate or resident who triggers it.

Three use cases for physical mail:

1. **Proxy delivery letters to boards:** A formal letter with the signed proxy enclosed, sent via USPS First Class with tracking. This is the escalation path for boards that ignore emails. HOAproxy generates a print-ready PDF; the resident prints and mails it themselves, or the platform offers one-click mailing through an API like Lob.com (~$1.10/letter) charged directly to the resident's payment method.

2. **Proxy solicitation postcards to residents:** For communities where a delegate wants to reach neighbors who aren't on HOAproxy yet. A postcard saying: "Your neighbor [Delegate Name] is collecting directed proxies for the upcoming [HOA Name] annual meeting on [Date]. Assign your vote at hoaware.com/[community-slug]." HOAproxy generates the postcard design; the delegate pays for printing and postage (~$0.70/postcard via Lob, or ~$0.35 if the delegate prints and stamps them at home).

3. **Pre-printed proxy forms with return envelopes:** For residents without internet access. The form includes a QR code linking to HOAproxy. HOAproxy generates the PDF; the delegate prints and distributes them.

**Cost to HOAproxy:** $0. The platform generates documents; users pay their own postage. For communities that want subsidized mailing, this could be a future grant-funded program or donation-supported feature.

**Why this works:** The delegate who is motivated enough to collect 20 proxies from their neighbors will spend $15–140 on postcards. That's the cost of civic engagement — comparable to yard signs in a local political campaign. HOAproxy's job is to make the *coordination* free, not the *atoms.*

### 4.4 Identity Verification

The e-signature itself provides baseline identity verification (email confirmation + IP logging). But a board might challenge a proxy by claiming the signer isn't actually the owner.

**Tiered approach:**
- **Basic (default):** Email verification. The resident creates a HOAproxy account with their email, and the e-signature is tied to that email. Sufficient for most situations.
- **Enhanced (optional):** The resident uploads a photo of a document tying them to the property (utility bill, tax statement, deed). HOAproxy stores a hash, not the document itself. This is available if challenged.
- **High-assurance (future):** Integration with a county property records API to verify that the signer matches the property owner on file. Several counties and data providers (ATTOM, CoreLogic) offer this data.

At launch, basic is fine. Most boards won't challenge individual proxies — the volume of organized proxies is the pressure, not the technical validity of any single one.

## 5. State-Specific Proxy Form Templates

The platform needs a template library, not a one-size-fits-all form. Key variations:

| Element | Default | State-Specific Override |
|---------|---------|------------------------|
| Instrument type | "Proxy" | AZ: "Absentee Ballot" |
| Holder restriction | Any person named | CA: "Must be association member" (enforced in UI) |
| Directed/undirected | Directed (recommended) | MD: Directed required for elections (enforced in UI) |
| Voting instructions | Separate section | CA: Must be on a *separate detachable page* |
| Duration | State maximum | Per state table in §2.3 |
| Meeting identification | Named + dated | IA, FL, NJ: Required (specific meeting) |
| Witness line | None | DC: Required (witness name + address) |
| Holder cap warning | None | NH: "This delegate holds X% of proxies (limit: 10%)" / ID: 50% cap |
| Revocation clause | Standard | Included in all forms per state statute |

Each template is a parameterized document stored in HOAproxy's legal corpus. When a user initiates a proxy assignment, the platform selects the correct template based on jurisdiction and community type (HOA, condo, coop), fills in the merge fields, and sends it to the e-signature provider.

## 6. Strategic Considerations

### 6.1 Constructive Engagement vs. Pressure

HOAproxy's proxy tool is inherently confrontational from the board's perspective — it redistributes power they currently hold by default. But the framing and sequencing matter enormously.

**The carrot-first sequence:**

1. **Phase 1 (current): Understand.** HOAproxy is a document transparency tool. It helps *everyone*, including boards, by making HOA rules searchable. Boards have no reason to oppose this. Some may even link to it. This phase builds HOAproxy's reputation as a good-faith civic platform.

2. **Phase 2: Participation Transparency.** Before facilitating proxy *assignment*, HOAproxy should make the *participation landscape* visible. Using meeting minutes (already uploadable in Phase 1) and unit counts from CC&Rs, the platform computes the "magic number" — how many directed proxies a delegate would need to shift election outcomes. See §6.4 for the detailed mechanics. This requires no board cooperation and no adversarial records requests.

3. **Phase 3: Proxy Assignment (the tool described in this memo).** Once a community is on HOAproxy and residents see the proxy imbalance, the assignment tool becomes the natural next step. By this point, HOAproxy has credibility and the community context to use it effectively.

**The olive branch:** For each community, HOAproxy should prominently display an invitation for the board to participate. "Is your HOA board using HOAproxy? Boards can post meeting agendas, candidate statements, and official proxy forms directly." Some boards will engage. When they do, HOAproxy becomes *the* governance platform — not an insurgent tool. This is the best outcome.

**The stick (when needed):** If a board refuses to accept e-signed proxies that are legally valid, HOAproxy should:
1. Generate a formal demand letter citing the applicable state statute and ESIGN/UETA.
2. Log the refusal publicly on the community's HOAproxy page.
3. Provide the resident with a template complaint to file with the state HOA oversight body (where one exists — e.g., Colorado DRE, Nevada Ombudsman).
4. Escalate to physical mail delivery as described in §4.3.

The key insight: HOAproxy doesn't need to sue anyone. The proxy laws already exist. Boards that refuse valid proxies are the ones breaking the law. HOAproxy just needs to make exercising existing rights frictionless enough that residents actually do it.

### 6.2 The Agent Future

When autonomous agents can handle information gathering — reading HOA documents, monitoring meeting agendas, tracking proxy deadlines — the coordination problem becomes the scarce resource. An agent can read your CC&Rs, but it can't build consensus among your neighbors.

HOAproxy's durable value in an agent-rich future is as a **coordination layer**:

- **Identity and trust.** Agents can gather information, but proxy assignment requires verified identity and legally binding signatures. HOAproxy's identity verification + e-signature pipeline is a trust layer that agents will need to plug into, not replace.

- **Community state.** The aggregate picture — who has assigned proxies, what positions have support, what the board is doing — is a coordination artifact that no individual agent can construct alone. HOAproxy is the shared ledger.

- **Collective action timing.** The strategic question isn't "what does the law say" (agents can answer that) but "when should we file our proxies to maximize impact and minimize the board's ability to counter-organize." This is game theory, not information retrieval. The platform's value is in coordinating the *when* and *how* of collective action.

- **Escalation infrastructure.** When a board rejects proxies, the response needs to be coordinated: multiple residents filing complaints simultaneously, demand letters with specific legal citations, perhaps press attention. Agents can draft the letters, but the orchestration — ensuring 15 homeowners file on the same day — is a coordination function.

In practice, this means HOAproxy's API should be agent-friendly from day one. An agent acting on behalf of a homeowner should be able to: check proxy status for a community, initiate a proxy assignment (pending human e-signature), check deadline compliance, and trigger escalation workflows. The e-signature step remains a human-in-the-loop gate — you can't delegate a legally binding signature to an agent (yet).

### 6.3 The Network Effect

HOAproxy's proxy tool has a strong within-community network effect: the more neighbors who assign proxies through the platform, the more visible and powerful the alternative bloc becomes, which motivates more neighbors to join. The critical mass for a single community is probably 10-20% of units — enough to be visible in the proxy count and credible as a voting bloc.

The cross-community network effect is weaker but still present: success stories from one community make the platform credible in the next. The legal corpus (already built) means HOAproxy can launch in any state with confidence. The template library means standing up a new community is near-zero marginal cost.

**Growth loop:**
1. One engaged resident adds their community to HOAproxy (Understand phase — already works).
2. They upload meeting minutes; HOAproxy shows "31 of 200 homeowners decided the last election — a bloc of 35 proxies could change the outcome" (Phase 2 participation transparency).
3. They volunteer as a delegate and send postcard solicitations to neighbors.
4. 10-20 neighbors assign directed proxies through HOAproxy.
5. The delegate attends the meeting with a visible bloc. Win or lose, this is newsworthy within the community.
6. Word spreads. More communities join.

The postcard is the growth hack. It's physical, it's local, and it reaches residents who aren't online. At $0.70 per postcard and 200 units in a typical HOA, that's $140 to reach every household — a cost one motivated resident can absorb or that HOAproxy can subsidize as a growth investment.

### 6.4 Phase 2 Deep Dive: Participation Transparency

Phase 2 (proxy transparency) is the bridge between document search and proxy assignment. The original concept — having residents file records requests to obtain copies of proxy assignments the board holds — is a stretch. It requires repeated formal requests, board cooperation, and resident effort that's hard to sustain. Most proxy inspection rights are post-meeting, and even in strong-statute states (FL, CO, AZ, CA) the board can drag its feet.

A better foundation: **infer the participation landscape from data that's already accessible.**

#### 6.4.1 The Core Inference

Two numbers are enough to estimate the opportunity:

1. **Total units in the community.** Available from the declaration/CC&Rs (already uploaded in Phase 1), public plat records, or county property data. HOAproxy likely already knows this from Phase 1 document ingestion.

2. **Total votes cast at the last annual meeting.** Available from meeting minutes, which are among the most universally accessible association records. Nearly every state requires associations to keep meeting minutes and make them available to owners — this is far less contested than requesting proxy forms specifically.

From these two numbers, HOAproxy can compute:

- **Participation rate:** "At the 2025 annual meeting, 47 of 200 units cast votes (24% participation)."
- **Quorum threshold:** "Your community's quorum requirement is 20% (40 units). Last year, quorum was barely met."
- **Control estimate:** "The winning board candidate received 31 votes. With 200 units in the community, **16% of homeowners decided the election.** A bloc of 32 directed proxies would have been enough to change the outcome."

This is the killer number. It tells a resident exactly how achievable change is — and in most HOAs, the answer is *shockingly* achievable. Participation is typically 15–30%, and the winning margin is often a fraction of that.

#### 6.4.2 Where the Data Comes From

**Meeting minutes (primary source).** Minutes typically record: quorum count (units represented in person + by proxy), election results (votes per candidate or yes/no tallies), and sometimes the total proxy count. This is the single most valuable data point for Phase 2, and it's the easiest to get:

- Nearly every state requires associations to retain minutes. Florida requires minutes for the preceding 7 years. North Dakota requires 6 years. Most states require at least 1–3 years of retention.
- Minutes are among the least contested records requests. Boards are accustomed to sharing minutes — they're typically posted to the community portal or distributed after the meeting. Some states (IL) require that minutes be available within 10 business days.
- A resident who attended the meeting can simply report the numbers without needing a formal records request at all.
- Phase 1's document upload pipeline already handles meeting minutes PDFs. HOAproxy can extract participation numbers from minutes that have already been uploaded for the "Understand" chatbot.

**Declaration / CC&Rs / bylaws (for quorum and unit count).** These documents, already ingested in Phase 1, specify the quorum percentage and total voting interests. HOAproxy's legal corpus and chatbot can extract the quorum rule automatically.

**Election results (if reported separately).** Some communities distribute election results by email or post them to the community portal. A resident can upload these.

**Owner-uploaded proxy solicitation postcards (supplementary).** When a resident uploads the board's proxy solicitation postcard, HOAproxy learns: who the default proxy holder is (usually the board president), the meeting date, and what the proxy form looks like. This requires zero board cooperation — the resident is sharing a document they received. It enriches the dashboard without requiring a formal records request.

#### 6.4.3 What Phase 2 Actually Publishes

The participation dashboard per community:

- **"X of Y homeowners decided the last election."** The headline number. e.g., "31 of 200 homeowners elected the current board." This is the single most motivating data point — it makes the power vacuum visible.
- **Participation rate over time.** If minutes from multiple years are available: "Participation has declined from 34% (2022) to 24% (2025)." Trend data makes the case for engagement.
- **Quorum fragility.** "Your quorum is 20%. Last year, 24% participated — just 8 units above the minimum. If 9 fewer people had voted, the meeting would have failed to achieve quorum." This reframes apathy as a near-miss institutional failure.
- **The magic number.** "Based on historical turnout, **a coordinated bloc of ~35 directed proxies** would likely be enough to determine board elections in this community." This is the call to action — it tells a potential delegate exactly what the target is.
- **Board's proxy solicitation.** If uploaded: "The board sent proxy forms on [Date]. The default proxy holder is [Board President Name]. If you returned the postcard without naming someone else, your vote is assigned to them."
- **Time remaining.** "The annual meeting is [Date]. There are [N] days to assign or reassign your proxy."

#### 6.4.4 Why This Approach Works

**The data is already there.** Meeting minutes are the most accessible association records — they're routinely shared, rarely contested, and often already uploaded to HOAproxy in Phase 1. No adversarial records request needed.

**One number does the work.** The "magic number" — how many directed proxies would shift the outcome — is the single most important piece of information for motivating a potential delegate. It converts abstract civic duty into a concrete, achievable goal. "You need 35 neighbors" is actionable. "The board holds an unknown number of proxies" is not.

**It compounds across cycles.** Each annual meeting produces a new data point. After 2–3 years, HOAproxy has a participation trend for the community. Declining participation makes the case for engagement; stable low participation makes the case that the status quo is entrenched.

**No board cooperation required.** The entire Phase 2 dashboard can be populated from: (a) documents already uploaded in Phase 1 (CC&Rs for unit count and quorum rules), (b) meeting minutes uploaded by any attendee, and (c) optionally, an uploaded proxy solicitation postcard. The board never needs to respond to a records request.

#### 6.4.5 Direct Proxy Inspection (Stretch Goal, Not Core)

For completeness: in strong-statute states, residents *do* have legal rights to inspect proxy records. Proxies are "association records" in FL, CO, CA, UCIOA states, and others. Colorado requires one-year retention of proxies and forbids requiring a "proper purpose" for records requests. Florida mandates production within 10 business days with penalties for non-compliance. California makes election materials (including proxies) inspectable for 12 months post-election.

But this is a stretch goal, not the core of Phase 2. The timing problem is real: proxy inspection rights are primarily post-meeting, and pre-meeting requests for a running proxy tally face resistance. If a particularly motivated resident in a strong-statute state wants to file proxy records requests, HOAproxy can provide template letters citing the right statute. But the platform should not depend on this data for the participation dashboard to be useful.

Connecticut adds a further wrinkle: unredacted proxy forms identifying how a specific owner voted must be withheld (Conn. Gen. Stat. § 47-260). The proxy *assignment* (who authorized whom) is legally distinct from the *voting instructions*, but the distinction gets muddied in practice.

## 7. Technical Implementation Priorities

### Phase 2a (Participation Transparency — build first)
- Extract unit count and quorum rules from CC&Rs/bylaws already uploaded in Phase 1 (can be automated via the existing chatbot/search pipeline or manually entered)
- Meeting minutes upload + structured data extraction: total votes cast, quorum count, election results (votes per candidate), in-person vs. proxy breakdown if reported
- "Magic number" calculator: given unit count, quorum threshold, and historical turnout, compute the approximate bloc size needed to determine outcomes
- Participation dashboard per community: headline participation rate, trend over time, quorum fragility, magic number, meeting date countdown
- Optional: upload interface for board proxy solicitation postcards (enriches the dashboard but not required for core functionality)
- No e-signature integration needed yet
- No adversarial records requests needed — all data comes from documents residents already have access to
- Low cost, high information value, minimal resident effort

### Phase 3a (Proxy Assignment MVP)
- State-aware proxy form template engine (parameterized by jurisdiction + community type)
- Self-hosted Documenso deployment for embedded e-signing ($0 variable cost)
- Email delivery of signed proxies to boards (free-tier transactional email or self-hosted)
- Proxy status tracking (filed, delivered, acknowledged, challenged)
- Duration/expiration management with renewal prompts
- Revocation workflow
- Public dashboard updates (aggregate proxy counts by delegate)

### Phase 3b (Growth and Resilience)
- Print-ready PDF generation for proxy delivery letters and solicitation postcards
- Optional Lob API integration for one-click mailing (user-paid, pass-through)
- Delegate registration and profile pages
- Voting instruction builder (agenda items, candidates, positions)
- Community property records integration for identity verification
- Board participation portal (post agendas, candidate statements, official proxy forms)
- Escalation toolkit: demand letter templates, state complaint filing guides

### Phase 3c (Agent-Ready)
- REST API for proxy status queries and assignment initiation
- Webhook notifications for proxy events (filed, expiring, challenged)
- Agent authentication framework (agent acts on behalf of verified human)
- Bulk operations for agents managing proxies across multiple communities

## 8. Cost Model

**HOAproxy's costs are entirely fixed** — server infrastructure to run the web app, Documenso, and the database. There are no per-proxy variable costs.

| Item | Cost to HOAproxy | Cost to resident/delegate |
|------|----------------|--------------------------|
| E-signatures (self-hosted Documenso) | $0 | $0 |
| Email delivery to board | $0 (free-tier or self-hosted) | $0 |
| Physical letter to board (if needed) | $0 | ~$1–3 (print + stamp, or ~$1.10 via Lob) |
| Postcard solicitation (200 units) | $0 | ~$70–140 (delegate pays) |
| Pre-printed proxy forms (20) | $0 | ~$5 (delegate prints at home) |

**Fixed infrastructure cost:** HOAproxy already runs on Render. Adding Documenso as a Docker service adds marginal compute. Estimated incremental cost: $5–15/month for the signing service container + database storage. This is fundable through donations, grants, or the project maintainer's pocket.

**The principle:** HOAproxy bears the coordination costs (software, templates, legal corpus). Users bear the atoms costs (postage, printing). This is the standard model for open-source civic tech — the platform is free; the campaign costs money.

## 9. Design Decisions

1. **A volunteer delegate is required.** The community delegate model requires at least one motivated resident per community to serve as proxy holder and attend the meeting. There is no "quorum-only" or "proxy escrow" mode. If no one volunteers, the proxy assignment tool is not available for that community — the participation dashboard (Phase 2) still functions and may motivate someone to step up.

2. **Directed proxies are the default.** The UI defaults to directed proxy assignment, where the resident specifies voting instructions per agenda item. Undirected proxies (general authorization to the delegate) are available as an option for residents who prefer simplicity, but the platform steers toward directed because it's legally safer (required for board elections in MD, stronger standing generally) and more empowering for the grantor.

3. **Board refusal playbook.** If a board refuses to seat a delegate or accept valid proxies at the meeting, HOAproxy does not attempt to litigate. Instead, the proxy assignment page includes a "What If?" section with state-specific information on where to file complaints — state HOA oversight bodies (CO Division of Real Estate, NV Ombudsman, etc.), attorney general consumer protection divisions, and relevant statutes. This information is compiled from the legal corpus. Escalation beyond that is the delegate's judgment call.

4. **Sustainability:** As a nonprofit open-source project, HOAproxy's proxy feature has near-zero variable costs. Fixed costs (server, domain, maintainer time) could be covered by: fiscal sponsorship through an existing civic tech nonprofit (Code for America, etc.), small grants from democracy/housing foundations, a GitHub Sponsors or Open Collective donation page, or pro bono contributions from civic-minded developers. The proxy feature should never have a per-use fee — that would undermine adoption at exactly the communities that need it most.

5. **Privacy as a platform-wide concern.** Proxy forms contain names and addresses. HOAproxy publishes aggregate statistics, not individual forms. But the e-signed PDFs exist in the system. Privacy protection filters must be applied throughout HOAproxy — not just the proxy feature — to ensure compliance with CCPA, state privacy laws, and basic data hygiene. This includes: PII scrubbing on public-facing dashboards, data retention limits, deletion workflows, and clear user consent for any data stored. This is an architectural requirement that applies to Phase 1 (document uploads) as much as Phase 3 (proxy assignment).

## 10. Threat Model: Vulnerability to Bad Actors

HOAproxy sits at the intersection of legal instruments, community politics, and public-facing data. This creates attack surface from two distinct adversary profiles: **hostile board members / HOA insiders** who want to neutralize the platform, and **vandals / trolls** who want to cause mischief or discredit it.

### 10.1 Hostile Board Insiders

These are the more dangerous adversaries because they're motivated, persistent, and have inside knowledge of the community.

**Threat: Fraudulent proxy revocation.** A board member or ally claims that a proxy assigned through HOAproxy has been revoked — either by forging a revocation notice or by pressuring the homeowner to revoke and reassign to the board's preferred holder. Since most states require "actual notice of revocation to the presiding officer," the board controls the revocation intake.

*Mitigation:* HOAproxy's proxy includes explicit revocation procedures and warns the grantor that only they can revoke. The platform logs the chain of custody: assignment timestamp, delivery confirmation, and any revocation initiated through HOAproxy. If a board claims revocation that didn't come through the platform, the delegate can challenge it at the meeting with HOAproxy's audit trail. This is ultimately a he-said-she-said at the meeting, but a documented audit trail favors the HOAproxy proxy.

**Threat: Rejecting proxies on technicalities.** A board or their attorney scrutinizes HOAproxy proxies for procedural defects — wrong date format, missing meeting address, e-signature not "duly executed," etc. — that they would never apply to their own proxy solicitations. The goal is to disqualify enough proxies to maintain control.

*Mitigation:* State-specific templates must be legally rigorous. Every template should be reviewed against the applicable statute's requirements (date, meeting identification, signature, witness if DC, directed/undirected form). The platform should generate proxies that are *more* procedurally correct than the typical board postcard. HOAproxy's legal corpus already catalogues these requirements; the template engine should enforce them. Additionally, the "What If?" section should prepare delegates for this tactic and advise them to object on the record if proxies are rejected, citing the statute.

**Threat: Infiltrating the delegate role.** A board ally registers as a community delegate on HOAproxy to collect proxies, then either doesn't attend the meeting, attends but votes contrary to the directed instructions, or collects proxies and then revokes/reassigns them.

*Mitigation:* Directed proxies are the default. A delegate who votes contrary to directed instructions is violating the proxy instrument itself — the homeowner has a legal claim. The platform should clearly communicate to grantors that they can verify their delegate's standing and revoke/reassign at any time before the meeting. HOAproxy should also display the delegate's identity prominently so the community can vet them socially. There is no way to fully prevent a bad-faith delegate, but directed proxies plus transparency minimize the damage — the delegate can't secretly defect because the grantor's instructions are on record.

**Threat: Legal intimidation.** A board sends a cease-and-desist to HOAproxy or to individual residents using the platform, claiming copyright infringement (for uploaded documents), defamation (for the participation dashboard), or tortious interference (for the proxy assignment tool).

*Mitigation:* HOAproxy's legal basis for document sharing is already addressed on the About page with state-specific citations. For proxy assignment, the legal basis is ESIGN/UETA plus the state's proxy statute — HOAproxy is facilitating a right the homeowner already has. The participation dashboard publishes only aggregate statistics derived from association records that owners are entitled to access. Cease-and-desist letters should be taken seriously but are unlikely to have merit. HOAproxy should have a pro bono legal counsel relationship (a law school clinic or a civic tech legal partner) to respond to these. Public logging of intimidation attempts (with the homeowner's consent) can also deter boards from overreach.

**Threat: Poisoning participation data.** A board member or ally uploads fabricated meeting minutes or false participation numbers to make the dashboard inaccurate — either inflating participation (to make organizing seem hopeless) or deflating it (to discredit the platform when the real numbers come out).

*Mitigation:* Uploaded documents should be attributed to the uploader's account. Multiple uploads of conflicting minutes for the same meeting should trigger a review flag. Cross-referencing with other uploaded documents (e.g., does the quorum count match the number of units in the CC&Rs?) can catch obvious fabrications. Ultimately, any resident who attended the meeting can contest fabricated minutes by uploading the real version. The platform should surface conflicts rather than silently accepting the latest upload.

### 10.2 Vandals and Trolls

Lower stakes, but they can undermine credibility and waste volunteer time.

**Threat: Fake communities.** Someone creates a bogus HOA on HOAproxy ("Fake Estates HOA") and populates it with joke documents or false data. If this scales, it dilutes trust in the platform.

*Mitigation:* Community creation should require a verifiable anchor: an uploaded declaration/CC&Rs with a real county recording reference, or a link to a real management company portal. Phase 1 already requires document uploads — a community with no real governing documents is easy to flag. Moderation (even lightweight, manual review of new communities) can catch this. At scale, automated checks against county recorder databases or property data APIs can validate that a community exists.

**Threat: Fake proxy assignments.** Someone creates accounts for homeowners who don't exist (or who haven't consented) and assigns proxies through the platform to inflate a delegate's count.

*Mitigation:* Account creation requires email verification (baseline). For proxy assignment, the e-signature step is the primary gate — a real e-signature with audit trail is hard to forge at scale. The identity verification tiers (§4.4) add further protection: enhanced verification (property document upload) and high-assurance (county records API) can be required for communities that face this threat. Additionally, the board will verify proxies against the membership roll at the meeting — fake proxies for non-existent owners will be rejected at the point of use. The damage is to HOAproxy's credibility, not to the election itself.

**Threat: Spam or offensive content in delegate profiles / community pages.** A troll registers as a delegate and posts inflammatory content.

*Mitigation:* Standard content moderation. Delegate profiles and community pages should have reporting mechanisms. HOAproxy already has "content filters to keep it kosher" (per the About page). Delegate registration can require manual approval by the community's first uploader or by HOAproxy moderators.

**Threat: Scraping PII from the platform.** Even though HOAproxy scrubs PII from public dashboards, a motivated actor might try to extract homeowner information from uploaded documents, proxy metadata, or delegate records.

*Mitigation:* The privacy filters (Design Decision #5) are the primary defense. PII scrubbing must happen at upload time, not display time — documents should be processed to redact names, addresses, account numbers, and signatures before storage. Proxy metadata visible on the dashboard should be aggregated: "12 proxies assigned to Delegate A" not "John Smith at 123 Oak St assigned to Delegate A." Access to raw e-signed PDFs should be restricted to the grantor, the delegate, and HOAproxy administrators.

### 10.3 Systemic Risks

**Risk: HOAproxy becomes the tool of a different kind of crank.** The same platform that empowers constructive reformers can empower a neighborhood busybody or political actor to weaponize the proxy system against a well-functioning board. HOAproxy has no way to evaluate whether a delegate's agenda is "good" — and shouldn't try.

*Mitigation:* Directed proxies are the structural answer. If every proxy includes specific voting instructions from the grantor, no delegate can unilaterally impose their own agenda. The platform empowers individual homeowners, not delegates — the delegate is a logistics role, not a political role. The transparency dashboard also cuts both ways: if a delegate is collecting proxies for a frivolous cause, the community can see the voting instructions and decide not to participate.

**Risk: A board uses HOAproxy against residents.** A savvy board could use the platform's own transparency tools — the participation dashboard, the proxy assignment feature — to organize *their* supporters more effectively. The board president registers as a delegate and collects directed proxies through HOAproxy, adding the platform's audit trail and legitimacy to their existing advantage.

*Mitigation:* This is actually a success condition, not a failure. If the board is organizing transparently through a public platform alongside challenger delegates, the election is more democratic regardless of who wins. The board loses its structural advantage (default proxies via opaque postcards) and must compete for support on a level playing field. HOAproxy's goal is democratic participation, not a particular outcome.

**Risk: Platform captures / single point of failure.** If HOAproxy goes down during election season, communities that depend on it lose their coordination infrastructure.

*Mitigation:* Open source is the structural answer. The codebase is public. Communities or civic tech organizations can fork and self-host if the main instance becomes unavailable. Proxy PDFs are delivered to both the grantor and the board — they exist independently of the platform. The platform is a convenience layer, not a custodian of the legal instruments themselves.

## 11. Founder Liability Protection

HOAproxy sits in legally contested territory — helping residents exercise rights that boards may resist. The threat model (§10) addresses platform-level attacks, but the founder also faces personal liability risk from hostile boards, disgruntled homeowners, or unanticipated legal theories. The following layers of protection should be implemented roughly in order of priority and cost.

### 11.1 Incorporate a Nonprofit Corporation

The single most important step is to **never operate HOAproxy as a personal project or sole proprietorship.** Form a 501(c)(3) or 501(c)(4) nonprofit corporation (the choice depends on whether HOAproxy's primary activity is "educational" vs. "social welfare/lobbying" — a 501(c)(4) is likely more appropriate given the advocacy dimension, though it means donations aren't tax-deductible).

A nonprofit corporation creates a legal entity separate from you. If someone sues HOAproxy, they sue the corporation — not Wyatt Clarke personally. The corporate shield protects your personal assets (house, savings, etc.) as long as you maintain the corporate formalities: keep a board of directors, hold annual meetings, maintain separate bank accounts, don't commingle personal and organizational funds.

**Cost:** $50–300 in state filing fees (varies by state). Legal help for articles of incorporation: free if you use a law school clinic or SCORE volunteer, or $500–1,500 for an attorney. Annual compliance: minimal (annual report filing, ~$25–75/year in most states).

**Faster alternative: fiscal sponsorship.** If forming your own nonprofit feels premature, you can operate under the umbrella of an existing nonprofit through fiscal sponsorship. Organizations like the Open Source Collective (via Open Collective), Hack Club, or civic tech sponsors like Code for America accept projects. The sponsor's 501(c)(3) status covers you, they handle administrative overhead, and they typically take 5–10% of any donated funds. You can always "graduate" to your own nonprofit later.

### 11.2 Maintain the Corporate Shield

Incorporation only protects you if you respect the separation between yourself and the entity. Courts can "pierce the corporate veil" — holding the founder personally liable — if the corporation is treated as an alter ego. The key practices:

- **Separate finances.** Open a bank account in the nonprofit's name. Pay all HOAproxy expenses from that account. Never pay HOAproxy server bills from your personal credit card (or if you must in the early days, reimburse yourself formally through the organization with a board resolution).
- **Board of directors.** Even a small board (3 people) establishes that HOAproxy isn't a one-person show. Recruit a couple of allies — a civic tech friend, a housing advocate — willing to serve as directors. Hold at least one documented meeting per year.
- **Written policies.** Have a brief conflict of interest policy, a data retention policy, and terms of service. These don't need to be long, but they need to exist.
- **Insurance.** Directors and Officers (D&O) insurance is available for small nonprofits at $500–1,500/year. It covers the cost of defending you personally if someone names you individually in a lawsuit despite the corporate shield. This is the single best expenditure for peace of mind. General liability insurance (~$400–800/year) covers the organization itself.

### 11.3 Section 230 of the Communications Decency Act

Section 230 provides that "no provider or user of an interactive computer service shall be treated as the publisher or speaker of any information provided by another information content provider." HOAproxy, as a platform where residents upload documents and assign proxies, is an interactive computer service hosting third-party content.

This means: if a homeowner uploads meeting minutes that contain defamatory statements about a board member, the board member's defamation claim lies against the homeowner — not against HOAproxy. If the participation dashboard publishes statistics that a board considers damaging, Section 230 protects the platform as long as the underlying data was provided by users.

**What Section 230 does NOT protect:** Content that HOAproxy itself creates. If the platform generates an analysis that says "this board is corrupt" based on participation data, that's HOAproxy's own speech. Section 230 protects you from liability for *other people's* content on your platform. Keep HOAproxy's own editorial voice factual and measured — the platform presents data and facilitates legal instruments, it doesn't make accusations.

### 11.4 The Volunteer Protection Act

The federal Volunteer Protection Act of 1997 limits personal liability for volunteers of nonprofit organizations. If HOAproxy is a registered nonprofit, you (as an unpaid volunteer/founder) are shielded from liability for harm caused by your actions on behalf of the organization — as long as the harm wasn't caused by willful misconduct, gross negligence, or criminal behavior, and you were acting within the scope of your responsibilities.

This is an additional layer on top of the corporate shield. Even if someone tries to sue you personally (piercing the corporate veil), the VPA provides a federal floor of protection for nonprofit volunteers. To qualify, you must not be receiving compensation beyond $500/year in expense reimbursements.

### 11.5 Terms of Service and Disclaimers

HOAproxy should have clear Terms of Service that:

- **Disclaim legal advice.** "HOAproxy provides legal information compiled from publicly available statutes. It is not a law firm, does not provide legal advice, and no attorney-client relationship is created by using this platform."
- **Disclaim proxy outcomes.** "HOAproxy facilitates the creation and delivery of proxy instruments based on your state's laws. It does not guarantee that any board will accept a proxy, that any election outcome will change, or that any legal strategy will succeed."
- **Require user responsibility.** "You are responsible for verifying that the proxy form generated by HOAproxy is appropriate for your community and complies with your governing documents."
- **Include an indemnification clause.** Users agree to hold HOAproxy harmless for claims arising from their use of the platform.
- **Include an arbitration/dispute resolution clause.** This can prevent expensive litigation by routing disputes to arbitration.

### 11.6 Specific Lawsuit Vectors and How to Deflect Them

**Tortious interference with contract.** This is the most plausible claim a hostile board could bring. The argument: HOAproxy is intentionally interfering with the contractual relationship between the HOA and its members (the CC&Rs) by encouraging residents to assign proxies away from the board's preferred holder. The defense: proxy assignment is a right explicitly granted by the CC&Rs and state statute. Facilitating the exercise of a legal right is not tortious interference — it's the opposite. The platform doesn't encourage breach of any contract; it helps homeowners exercise a right the contract gives them.

**Defamation via the participation dashboard.** A board claims that publishing low participation numbers (e.g., "only 15% of homeowners voted in 2024") is defamatory. The defense: truth is an absolute defense to defamation. If the numbers are derived from meeting minutes that the homeowner lawfully obtained and uploaded, publishing aggregate statistics is protected speech. Section 230 adds a layer if the data was user-uploaded.

**Unauthorized practice of law (UPL).** A state bar complains that generating state-specific proxy forms constitutes legal advice. The defense: providing legal information (including form templates) is distinct from providing legal advice. LegalZoom, Rocket Lawyer, and dozens of form-generation services operate on this distinction. HOAproxy should include the disclaimer (§11.5), should never tell a user "you should do X" (instead: "your state's law permits X"), and should recommend consulting an attorney for complex situations.

**Copyright infringement for uploaded documents.** A management company claims copyright over meeting minutes, CC&Rs, or other governing documents that residents upload to HOAproxy. The defense: CC&Rs are recorded public documents. Meeting minutes are association records that owners are entitled to access and copy under state law. Even if a management company claims copyright, fair use and the owners' statutory access rights provide a strong defense. Additionally, Section 230 and the DMCA safe harbor (if HOAproxy implements a proper DMCA takedown process) protect the platform from liability for user-uploaded content.

### 11.7 Priority Checklist

1. **Immediately:** Form a nonprofit corporation (or enter a fiscal sponsorship arrangement). This is non-negotiable before the proxy feature launches.
2. **Before proxy launch:** Draft Terms of Service with the disclaimers above. Post them prominently.
3. **Before proxy launch:** Implement a DMCA takedown process (even a simple email address — dmca@hoaware.com — with a documented procedure).
4. **Within first year:** Obtain D&O insurance. Shop through a nonprofit insurance broker; Hartford, Philadelphia Insurance Companies, and Nonprofits Insurance Alliance are common carriers.
5. **Within first year:** Recruit 2–3 board members for the nonprofit. This strengthens the corporate shield and distributes governance.
6. **Ongoing:** Maintain corporate formalities — annual meeting, separate bank account, board minutes. This is unglamorous but it's what keeps the shield intact.
7. **If/when a cease-and-desist arrives:** Do not respond yourself. Have the nonprofit's counsel (pro bono, law school clinic, or civic tech legal partner) respond on behalf of the organization. The response comes from "HOAproxy, Inc." — not from Wyatt Clarke.

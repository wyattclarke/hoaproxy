# HOAware UX Fix List

Issues and improvements identified during exploratory testing (Mar 2026).

1. **Register/Login pages** — add show/hide toggle on password field(s)

2. **Claim Membership** — allow searching for an HOA by address (not just name/ID)

3. **Claim Membership** — remove the Unit # field; collect full property address (street address OR unit number) at proxy assignment instead, where it's actually needed for verification

4. **Delegates** — prevent a user from designating themselves as their own delegate/proxy

5. **Directed proxy** — the current blank JSON field is inadequate; design and build a full agenda-item workflow: board creates meeting agenda items, member selects per-item vote direction (Yes/No/Abstain/No direction), proxy form captures structured choices — no raw JSON exposed to users. Likely warrants its own milestone.

6. **Proxy creation** — confirmation message wrongly says "Proxy created" when board notification isn't implemented yet; fix messaging to accurately reflect what actually happened (saved locally) vs. what's pending (board notification, delivery); also explain the signing step before redirecting

7. **Proxy signing page** — stuck on "Loading proxy form..." — proxy never loads; needs debugging

8. **Education** — the site assumes users understand proxy voting; add contextual explanations throughout: what a proxy is, why it matters, what a directed vs. undirected proxy means, what a delegate does, what happens after signing, and what legal rights members have. The legal corpus already in the DB makes it possible to personalize this content to the user's state once they've claimed HOA membership — a significant opportunity.

9. **Security: login rate limiting** — `/auth/login` has no server-side rate limiting; brute-force password attacks are possible via the API directly (bypassing any frontend checks)

10. **Security: email verification** — users can register with any email address and immediately access the system; add a verification step (send confirmation link) before granting full access, especially important given PII is being collected

## Bugs (found via automated browser scan)

11. **`/proxy-form` is broken** — returns raw `{"detail":"Not Found"}` JSON instead of an HTML page; route is missing or misconfigured

12. **`/legal` is broken** — same issue, returns raw `{"detail":"Not Found"}` JSON

13. **`/participation` — HOA name missing from heading** — "Meeting attendance and voting data for" is cut off; HOA name not rendering

14. **`/participation` — "Could not find HOA"** — page immediately errors with no guidance on what to do; needs a better empty state (e.g. prompt to claim membership first)

## Dashboard UX (found via automated browser scan)

15. **`self_declared` badge** — internal status label leaking to UI; should say "Pending verification" or be omitted entirely

16. **"Become a delegate" link is duplicated** — appears once inside each HOA membership card AND again in the Delegate Status section below; consolidate to one location

17. **Nav is incomplete** — Dashboard nav only shows "Dashboard / My Proxies / Logout"; missing links to Participation and Legal pages

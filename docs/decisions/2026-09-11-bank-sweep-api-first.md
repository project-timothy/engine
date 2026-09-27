# The bank-feed sweep moves to API-posted transactions; browser lane opt-in; banks never
Date: 2026-09-11
Type: One-way door for the product

PRD v2 section 5.2: the Thursday bank-feed sweep as built (a Claude Code
session driving the QuickBooks web UI through a Chrome browser tool with a
passkey) does not port to the product, and should not. Two paths, both
shipped:

- **Path 1, API first, the default.** The result of every Match the ledger
  corroborates is a Purchase or Deposit the QuickBooks API can create;
  QuickBooks then auto-matches the feed line on its own. The engine posts
  the corroborated transaction through the API on a card (or unattended
  where the policy table allows), and the feed line clears with one click
  or by Intuit's own matcher. The browser leaves the money path for the
  matched set. The parked-rows-to-cards automation already on the next-dev
  list is this (phase 7 row 7.4).
- **Path 2, an opt-in local assistant.** Playwright (Python) with a
  persistent context and a CDP virtual authenticator re-seeded from the
  tenant's encrypted secrets on each run, headed by default, run on the
  owner's or consultant's own machine. Shipped as a plugin with a
  terms-of-service note, never the default (phase 7 row 7.5 makes the
  sweep a tenant-local plugin). Intuit's QBO terms forbid scraping; an
  owner driving their own file from their own machine is the lowest-risk
  version and the realistic consequence is an account block.

Banks are never browser-driven, full stop; their agreements forbid
credential sharing. Bank data is three-tiered instead (section 5.3): an
aggregator adapter (SimpleFIN first, Plaid second, Teller third), a
fintech direct API where the tenant banks there, and a statement-file
parser as the floor that always works. QuickBooks' own bank feed is not
exposed by its API; the engine reads what the feed posted, not the feed.

The rule generalizes as row 6 of the decision table (section 4.1): a
target system with no API gets a document or report-inbox lane first, and
a browser lane only opt-in, local, headed, rate-limited, and never for
banks. Risk 3 (terms of service on browser lanes) is mitigated by exactly
this: API first, browser opt-in and local, banks never.

Why one-way for the product: an open, installable engine that browsed a
bank or shipped browser automation as the default would carry a
terms-of-service and account-block risk to every tenant; the product's
money path is API-posted from its first release. The first tenant
keeps its browser sweep as tenant-local wiring.

Refines 2026-09-09 (the sweep as a scoped unattended exception): that
decision stands for the first tenant; this one places it outside the
product core.

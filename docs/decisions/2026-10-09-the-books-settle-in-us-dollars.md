# The books settle in US dollars; other currencies arrive as a recorded conversion
Date: 2026-10-09
Type: Two-way door

Every amount the engine books is US dollars, held as integer cents and shown
as an exact decimal string (`core/tools/catalog.py` `money`). That stays true
for now (the owner, 2026-10-09): the sponsors are American, the gifts are
dollars, and the books report in dollars.

The need is real all the same. Field missionaries spend in local currency
(meticais in Mozambique, rupees in Nepal; Tim walkthrough 2), and today a
receipt in another currency becomes whatever number someone types. The way
in, when it is built, keeps the dollars the books' only currency and records
the conversion beside every foreign amount:

- each foreign line keeps its original amount, its ISO 4217 currency code,
  the rate used, the rate's date, and where the rate came from;
- the dollar amount is computed in code from those (Decimal, a stated
  rounding rule), never by a model and never typed by hand (invariant 2);
- the books' currency is one setting per tenant, so a tenant whose books are
  in another currency is a configuration, not a fork;
- the accounting connection stays single-currency: in QuickBooks Online,
  multicurrency cannot be turned off once it is on, so the engine never
  turns it on for a tenant.

Building it adds columns to the ledger, a schema migration, which is the
owner's call (a ledger schema change is a one-way door, CLAUDE.md). Until then, code
that handles money keeps it in integer cents with no currency assumptions
beyond the one setting, so the conversion slots in at the edge where a
document enters.

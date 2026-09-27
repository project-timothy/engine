# proposal: the W-9 lane flips the QBO vendor's 1099 flag through the API

Candidate: 1fd15cf3

Lens 12 (`vendor-1099`, `flag-missing`) has now named **7 taxpayers in 60
days** whose QBO vendor record has "Track payments for 1099" switched off
while the registry says a 1099-NEC is due. Every one of them was cleared the
same way: the owner opened QBO, found the vendor, clicked Edit, ticked the
box. The finding is correct, the fix is two minutes, and it has been done by
hand seven times — most recently on 2026-09-15, the night
after that contractor's `vendors.toml` row landed. The lens's own docstring already says
where this ends: *"the flag flip is the owner's UI act (or a future gated
write)."* This is the future gated write.

## Design

**The mechanism.** A new job, `ap vendor-1099-flags`, in the engine's
existing gated-write idiom (the shape W1 `qbo-push` and W2
`qbo-push-payments` already use):

1. **Decide, in pure code.** `core/agents/ap/vendor_1099_flags.py` takes the
   taxpayer picture and the QBO vendor list and returns one proposal per
   taxpayer that is due a 1099-NEC and whose matched QBO vendor(s) carry
   `Vendor1099 = false`. No I/O, no model, deterministic (invariant 4), the
   `direct_payment.py` split.
2. **Park one card per taxpayer**, `ap.qbo_vendor_1099_flag`, keyed on the
   taxpayer so it never doubles. The card carries: the registry taxpayer, the
   QBO vendor ids and display names to flip, the tax-year paid total that puts
   it over the floor, the classification that makes it reportable, and whether
   a W-9 is on file. An approval-time check (`APPROVAL_CHECKS`, the 7.1 shape)
   refuses a card whose vendor has since been flipped or whose taxpayer has
   since gone exempt, so a card is never approved-and-wrong.
3. **Execute on approval, and verify by readback.** A sparse Vendor update
   sets `Vendor1099 = true` through `core/adapters/qbo.py`, then RE-READS the
   vendor. `ap.qbo.vendor_1099_flipped` is recorded **only** when the readback
   shows the flag on. A readback that comes back false records
   `ap.qbo.vendor_1099_unverified` naming the vendor, and the lane says so and
   stops — it never claims a write that did not take.

**Why the readback is the load-bearing part.** 2026-08-03: the QBO API
accepted a Preferences update setting `BookCloseDate` and silently ignored
it, because that field is UI-only; the seal had been "verified" earlier by a
write that was a no-op, so the ignore was masked. `Vendor1099` is read back
by lens 12 today, so the field is visible on the read side — nothing proves
it is writable until one live flip is read back. The build starts with that
probe on a single vendor. If QBO refuses the field, the row closes with the
answer recorded and the flip stays the owner's UI act forever, which is a
better outcome than a lane that reports success it cannot prove.

**The inputs, and the one real cost.** The taxpayer picture — who is over the
$600 floor, per `tax_entity`, W-9 on file, classification not exempt — lives
in the auditor's CPA appendix, and `core/` may not import `auditor/` (the
package-independence lint). So the engine grows its own rule over
`VendorRegistry` (`vendors.toml`: `tax_entity`, `ledger_aliases`,
`tax_classification`, `w9`) plus Paid `ap_invoices` rows in the tax year.
Two implementations of "who is due a 1099" can drift, and that is this row's
main risk. Acceptance pins it: the engine-side rule must agree with lens 12
on the live subject list, taxpayer for taxpayer, before the write path is
enabled.

**Timing: the card parks when the finding first appears, not in January.**
The candidate sentence says "at filing time". Waiting until January is the
weaker half of the idea — QBO's own reports are wrong in the meantime, and
the January packet is the worst moment to discover that a vendor was never
tracked. The flag is on long before the module runs.

**What it must never do.**

- Never turn a flag **off**. `flag-set-on-exempt` stays a finding and an owner
  decision: switching tracking off for money already paid changes filing
  treatment.
- Never touch `TaxIdentifier` or anything TIN-shaped, and never request it
  (`docs/w9-1099-design.md` §2 — the TIN invariant is non-negotiable; QBO's
  Vendor query does not return the field and the client must not ask).
- Never create, rename, or merge a QBO vendor.
- Never match fuzzily. `qbo-vendor-unmatched` stays a finding; a flip needs an
  exact registry-name or `ledger_alias` hit, the join lens 12 already uses.
- Never flip without an approved card, and never in a shadow run.
- Never write `vendors.toml` (the build-3 precedent: emit the diff).
- Never move money and never file anything. This sets a reporting flag on a
  vendor record; invariant 7 is untouched.

## Failing eval

`evals/proposals/test_w9_lane_flips_the_qbo_1099_flag.py`, two assertions:

1. **`test_a_1099_due_vendor_with_the_qbo_flag_off_parks_one_card`** — a
   taxpayer over the floor with a W-9 and a non-exempt classification, whose
   matched QBO vendor has `Vendor1099 = false`, produces exactly one
   `ap.qbo_vendor_1099_flag` proposal naming that vendor id; an exempt
   taxpayer, a taxpayer under the floor, and a taxpayer whose QBO vendor
   already carries the flag produce none.
2. **`test_an_unverified_readback_never_records_the_flip`** — against a fake
   QBO client that accepts the update and returns `Vendor1099 = false` on
   readback, execution records no `ap.qbo.vendor_1099_flipped` event and
   reports the vendor as unverified. This is the 2026-08-03 contract, written
   before the code exists rather than after it fails.

**Why it fails today.** `core.agents.ap.vendor_1099_flags` does not exist and
`core.agents.ap.jobs` has no `VENDOR_1099_CARD`; the flip lives only in the
owner's hands. The eval fails at the import, by design, and stays red until
the build lane makes it pass and moves it into `core/agents/ap/evals/`.

## Issue

**Goal.** Retire the owner's repeated QBO click: a taxpayer due a 1099-NEC
whose QBO vendor is not flagged parks one approval card, and an approved card
flips `Vendor1099` through the API with a verified readback.

**Acceptance.**
- A live probe proves QBO persists `Vendor1099` through the API on one vendor,
  read back. If it does not, the row closes with that recorded and the lane
  stays a finding.
- The engine-side "due a 1099" rule agrees with lens 12's `flag-missing`
  subjects taxpayer-for-taxpayer on the live registry before any write runs.
- `evals/proposals/test_w9_lane_flips_the_qbo_1099_flag.py` passes and moves to
  `core/agents/ap/evals/`.
- An unverified readback records no flip event, names the vendor, and does not
  retry silently.
- No TIN-shaped field is requested or stored anywhere in the diff.
- Nothing turns a flag off; no vendor is created; no `vendors.toml` write.

**Touches.** `core/agents/ap/vendor_1099_flags.py` (new, pure),
`core/agents/ap/jobs.py` (card, approval check, execution),
`core/adapters/qbo.py` (sparse Vendor update + vendor read),
`core/agents/ap/brief.md` (invariant 6, same PR),
`tenants/<tenant>/tenant.toml` (`[qbo].vendor_1099_cards`, off by default),
`scripts/engine-ap-daily.sh` (one job line), `docs/w9-1099-design.md` §5.

**Size.** M. The card and the pure rule are a morning; the adapter probe comes
first and can kill the row in an hour.

**Depends.** Nothing blocking. The QBO write side (W1, W2) and the approval
queue already exist; lens 12 supplies the subject list to check the rule
against.

**Door.** Two-way and recoverable. The lane is off behind a tenant flag until
an owner flips it; a flag set in error is one click to unset in the UI; no
money moves and no return is filed.

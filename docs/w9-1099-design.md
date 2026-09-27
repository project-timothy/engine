# W-9 / 1099 vertical: the design

Status: builds 2 and 3 **BUILT** (2026-08-26, 2026-09-01); build 1 folded into
the auditor's appendix; build 4 slotted before January. Written 2026-07-28 for
the gate day on which the first tenant's W-9 pass found the counting bug. The
first tenant's live punch list and vendor picture from that day stay in its
own repository; this file keeps the design.

## 1. The problem

Vendor tax compliance is a moving target that, without the engine, depends on
the owner's memory:

- W-9s arrive ad hoc by email and get filed by hand into whatever folder the
  owner last used.
- The facts live in three places that nothing reconciles: the paper (a cloud
  folder), the registry (the tenant's `vendors.toml`), and QBO vendor records
  (`Vendor1099`, `TaxIdentifier`).
- The year-end lens was partially blind (section 3): a vendor raised as
  "missing a W-9" and corrected by hand four separate times, and a rule retired
  for firing on it, which silenced the symptom without fixing the counting.
- QBO's own flags drift from the truth: in the first tenant's book only two of
  the 1099-due vendors carried `Vendor1099 = true`, so QBO's own 1099 module
  would have dropped the rest at filing time.

January is the deadline that makes this real: 1099-NEC filing needs correct
recipients (W-9 line 1, not the check payee), correct box-1 totals per taxpayer
per year, and correct corp exemptions.

## 2. The invariant (non-negotiable)

**No TIN — SSN or EIN — ever enters this repo, the ledger, any commit, log,
test fixture, or generated report.** The W-9 PDFs in the tenant's W-9 folder and the QBO
vendor `TaxIdentifier` field are the only two homes a TIN has. QBO is the filing
surface: its 1099 e-file pulls the TIN from the vendor record, which is exactly
why the engine never needs to touch one. The engine computes names, amounts,
coverage, and exemptions — nothing else. Any check the engine makes against QBO
`TaxIdentifier` is presence-only (set/unset), never the value.

Corollary for intake (build 3): the engine may *move* a W-9 PDF (a file that
contains an SSN) into the W-9 folder, but never parses Part I, never
extracts a TIN, and never logs file contents.

## 3. What existed at the gate (2026-07-28)

| Fact | Where it lives | State at the gate |
|---|---|---|
| W-9 on file + verified date | `vendors.toml` `w9` + dated comment | Set for a few vendors; more forms on paper, unflagged |
| Federal tax classification | `vendors.toml` `tax_classification` | Recorded for some vendors; **nothing consumed it** |
| Same-taxpayer link | `vendors.toml` `tax_entity` + `registry.tax_siblings` | Modeled correctly; `tax_siblings` had **zero callers** |
| 1099 recipient (W-9 line 1) | channel_note prose only | No field |
| Year-end candidate list | `auditor/advisory/facts.py` `_year_end_cpa` | Advisory checklist; three counting defects below |
| QBO 1099 flags + TIN | QBO vendor records | Out of sync with the registry; writes owner-gated |
| The paper | the W-9 folder + an INDEX | Consolidated by hand |

The three counting defects:

1. `_load_registry` keyed by display name only, blind to `ledger_aliases`, so a
   contractor whose payments sat under the ledger spelling of his name missed
   the 1099 candidate list entirely.
2. W-9 coverage and candidate flagging were per vendor row, not per
   `tax_entity`: the generator of the recurring "missing W-9" false positive.
3. The $600 floor tested per row; two sibling rows individually under but
   jointly over both vanished. The floor is a per-taxpayer test.

## 4. Build 1 — make the counting honest (small; can precede the expenses agent)

All in the auditor plus one registry field. TDD; the recurring false positive
and the alias miss become regression evals (the spirit of invariant 8 —
this recurring incident finally gets its eval).

- Key the auditor's registry view under `ledger_aliases` too.
- Group W-9 coverage, candidate flagging, and the $600 floor by `tax_entity`
  (fall back to the single row when no entity is declared). The auditor cannot
  import `registry.tax_siblings` (independence lint), so it grows its own
  grouping over the raw TOML — it already half-does this for paid totals at
  `facts.py:169-175`.
- Consume `tax_classification`: corporations render as "corp — no 1099-NEC
  due", never silently dropped. Empty stays reportable (the field's contract).
- New optional `tax_recipient` field (W-9 line 1 name) on `VendorEntry` and in
  the year-end appendix, so disregarded SMLLCs carry the right recipient next
  to the vendor display name.

## 5. Build 2 — registry-vs-QBO sync lens (read-only, nightly) — BUILT 2026-08-26

Auditor lens 12, `vendor-1099` (`auditor/lenses/vendor_1099.py`, external,
switch `[auditor.vendor_1099] enabled = true`): the CPA appendix's taxpayer
units (the same `_year_end_cpa` computation, shared) joined to QBO vendors by
registry name + `ledger_aliases`, exact after case/whitespace/punctuation
normalization, never fuzzy. Three WARN conditions, all nag nightly until
fixed: `flag-missing` (1099-due, QBO `Vendor1099` off), `flag-set-on-exempt`
(registry says S/C corp, QBO flag on), `qbo-vendor-unmatched` (1099-due, no
QBO vendor by any registry spelling). Findings only; the flag flip is the
owner's QBO UI act (Expenses > Vendors > Edit > Track payments for 1099).

Scope change from the sketch above: QBO's Vendor query returns **no
`TaxIdentifier` field** (probed live 2026-08-26 against all 63 vendors), so
TIN presence is not checkable through the API and stays with QBO's own 1099
module at filing time. The client never requests anything TIN-shaped.

## 6. Build 3 — W-9 intake routing (BUILT 2026-09-01, as follows)

AP intake recognizes an inbound W-9 **deterministically and pre-model**
(`core/agents/ap/w9.py`): filename tokens first (`w9`/`w-9`/`fw9` as a token —
no content read at all), then the PDF text layer's form markers, checked in
memory and discarded. A detected form never reaches the extractor model — the
strongest possible reading of the TIN invariant. Detection routes the file
(`ap.intake.routed.w9`, so the janitor archives it as settled) and parks **one
approval card** (`ap.w9_file_and_flip`, content-keyed on the file md5):
vendor attribution is a subject-alias PROPOSAL, never a guess — "unknown" (or
a wrong proposal) is corrected at approval with `--param vendor=`; the
classification is supplied the same way (`--param classification=`) as the
owner reads it off the form. The engine never reads Part I.

**Two deltas from the original sketch, both deliberate:** (1) the copy into
`Vendor W-9s/` happens at APPROVED-CARD EXECUTION, not at detection — the
confirmed vendor names the file (`<Vendor_Slug>_W9_<TaxYear>.<ext>`), and no
headless run writes the cloud folder on a guess; a failed copy (folder
offline/evicted) records `ap.w9_copy_failed` and retries next run, while a
write-guard refusal (the folder outside its carve-out) is a job error, never
a retry (honesty audit 2026-09-03, 02-F8). The
landing original never moves (audit artifact). (2) The gate decision landed
as recommended: **emit the diff**. Execution records `ap.w9.filed` carrying
the registry diff as text — the event is the diff's durable home, the PR flow
picks it up, and the engine never writes vendors.toml.

Known limitation: an image-only W-9 with a neutral filename (no `w9` token)
has no text layer to detect and falls through to the normal intake lanes —
the extractor's doc-type router still files it as reference, never as an
invoice. Every live form seen to date carried the token.

Two edges the honesty audit (2026-09-03) pinned down:

- **`[w9].folder` gates filing, never detection** (02-F4). The TIN
  invariant outranks configuration: detection always runs, and with the
  folder unset a detected form is still routed (`ap.intake.routed.w9`,
  counted ROUTED, the extractor never sees it) and named by
  `ap.w9_folder_unset` ("configure [w9].folder to file it"), but NO
  `ap.w9_file_and_flip` card parks, because a card that can never execute is
  the lie the audit named. A card approved while the folder was set and then
  orphaned by unsetting it is named by the same anomaly on every executed
  run, never left approved-and-silent.
- **An unreadable text layer is a failed check, not a negative one** (02-F7).
  When a PDF candidate's text layer raises (encrypted, malformed), the file
  is parked in the manual-review shape (`ap.intake.needs_ocr` +
  `ap.review_needs_ocr`, counted NEEDS_OCR) with `ap.w9_text_unreadable`
  recorded, and it never reaches the extractor model on the strength of the
  failed read. The owner opens it by hand: a W-9 files through the lane, a
  non-W-9 gets OCR or manual review.

## 7. Build 4 — January 1099-NEC packet

A once-a-year job rendering the CPA/filing evidence workbook (openpyxl, tenant
metadata per invariant 10): per-taxpayer (tax_entity-grouped) box-1 totals per
calendar year from Paid ledger rows, line-1 recipient names, classifications,
exemption reasoning, W-9 file references. Explicitly: no TIN column exists in
the render, and the engine generates no 1099 form — filing runs through QBO's
1099 module, which holds the TINs.

## 8. What the appendix counts (refreshed 2026-08-26)

The appendix counts the ledger row's own cost type (unregistered
subcontractors had been invisible), excludes owners, counts only the tax
year's payments (cash basis; January reports the prior year), and nags W-9
gaps only where a 1099 is due. A tenant's live punch list (which flags to turn
on, which forms to eyeball) is its own record, never this file's.

## 9. Doors

- New registry fields (`tax_recipient`) and all of build 1: **two-way**,
  additive config and auditor code.
- Engine writing tenant config (build 3 variant): **one-way in process terms**
  — flagged above, recommendation is the diff-emitting variant.
- Ledger schema: **untouched.** This vertical reads `ap_invoices` only; no new
  tables, nothing for the schema-change gate.
- Sequencing: the expenses agent is committed first; build 1 is a day-scale slot-in if
  wanted before it. Builds 2-4 come after the expenses agent, with build 4 needed well
  before January.

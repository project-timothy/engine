# Design archetypes

`engine init <slug> --archetype A|B|C` renders a tenant from
`tenants/_templates/tenant.toml.tmpl` and the per-archetype overrides in
`tenants/_templates/archetypes.toml`. An archetype is a starting shape, never a
constraint: every key it sets is ordinary tenant configuration and can be
edited afterwards. The shapes below are what the engine is designed around;
they describe how the money moves, not who the business is.

## A. Project-coded technical services

Engineering, testing, inspection and compliance consulting, training, and
technical consulting as the wider pool; small hardware makers as a sub-shape
that adds contract cost pools.

Every dollar carries a project number. Vendors are moderate in count and high
in value, field staff are often 1099 contractors, expenses are travel-heavy,
customers pay on their own terms (often through a customer portal), and the
owner approves every payment.

Built today: AP, expenses, timesheets, the month-end close with book-lock and
statements, W-9 and 1099 tracking, the bank-feed sweep, the auditor. Next, in
order:

1. **AR**: aging bands with customer-specific terms, remittance parsing,
   issued-invoice register writes, a renderer for customers without a portal,
   PO burn against PO total, a follow-up queue. The engine's own table is the
   record; the accounting system receives the invoice and payment as writes;
   the workbook is a view (the AP pattern).
2. **Payroll prep**: hours from timesheet cards into a payroll draft and a
   handoff report; submission stays a human act.
3. **Contractor onboarding**: intake fields, agreement templates by entity
   type, a rate registry; the W-9 lane already catches the form.
4. **Class and roster billing with a certificate register**: per-seat and
   prepaid packages, roster to invoice, certificates with expiry and re-cert
   reminders as a lens.
5. **Contract cost accounting**: daily timekeeping with an audit trail, direct
   versus indirect segregation, indirect-rate computation, unallowable-cost
   flags, an R&D-credit substantiation export.
6. **The 1099-NEC January packet and the year-end accountant's package.**

## B. Construction subcontractors, with field-service trades as the on-ramp

Vendor-heavy and job-coded, with cash tied up in the billing chain. Start with
service shops (HVAC, plumbing, electrical: job costing, supply-house statement
reconciliation, license registers, no pay-application layer), then add the
commercial-sub layer. Pay applications arrive as PDF or CSV where the upstream
tool has no API, so ingestion accepts both from day one.

1. **Field-service sync**: jobs become project codes; invoices and payments
   are read, never entered.
2. **Supply-house statement reconciliation**: a vendor statement rather than
   one invoice per job, matched line by line against recorded invoices.
3. **Job costing**: budget against actual with committed costs, per job, as a
   lens and a report.
4. **License and permit register** with expiry lenses.
5. **Technician commission feed** into payroll prep.
6. **Progress billing** (commercial subs): AIA G702/G703 pay applications,
   schedule of values, retainage receivable and payable, conditional and
   unconditional lien waivers with jurisdiction timing, certified payroll
   (WH-347) for public work, WIP and over/under billing.
7. **COI register**: ACORD 25 parsing on arrival, an expiry lens,
   additional-insured tracking, the workers' comp audit packet assembled from
   payroll and vendor data.

## C. Agencies, creative and IT studios, with nonprofits as the alternate

Contractor-heavy, project-coded, retainer-billed, usually already on tools
with open APIs.

1. A retainer deferred-revenue schedule; milestone invoicing tied to an SOW
   registry.
2. Pass-through media accounts with gross versus net reporting; client-level
   P&L.
3. Time-to-invoice from the studio's time tool.
4. For nonprofits (its own design, because it touches every posting rule): the
   fund and restriction dimension on every posting, grant budget against
   actual with deadline reminders, functional expense allocation, donor
   restriction releases, a Form 990 export pack.

## Where each shape declines

A business whose point-of-sale or practice-management system owns the daily
journal (restaurants, salons, medical and dental practices) is out of scope:
that vendor already sells the AP and payroll layer, and per-transaction human
approval does not scale to hundreds of tickets a day.

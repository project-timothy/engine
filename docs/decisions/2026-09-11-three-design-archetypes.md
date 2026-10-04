# Three design archetypes; restaurants, medical, dental, and salons declined
Date: 2026-09-11
Type: Two-way door

PRD v2 section 3.1 ranks sixteen small-business archetypes by firm count
(Census SUSB 2022, employer firms with 1 to 99 employees), fit, and the
durability of the core business against AI over 20 years, and designs
against three:

- **A. Project-coded technical services**: engineering, testing, safety
  and compliance consulting, training; consulting as the wider pool;
  hardware startups as the SBIR sub-shape. Every dollar carries a project
  number, vendors are moderate in count and high in value, field staff are
  often 1099, expenses are travel-heavy, customers pay on their own terms
  through portals, and owners approve every payment. Both design partners
  live here.
- **B. Construction subcontractors, with field-service trades as the
  on-ramp**: the largest vendor-heavy, job-coded population in the country
  with the lowest AI exposure in the table. Start with HVAC, plumbing, and
  electrical service shops, then add progress billing, retainage, lien
  waivers, and COI tracking for commercial subs. Residential builders on
  Buildertrend have no API, so the design accepts PDF and CSV pay-app
  ingestion from day one.
- **C. Agencies, creative, and IT studios, with nonprofits as the
  alternate**: contractor-heavy, project-coded, retainer-billed, already on
  open-API tools. Price per tenant, never per seat, because headcount per
  agency will fall. Nonprofits rank fourth only because the fund and
  restriction dimension touches every posting rule, a schema decision that
  gets its own design day after AR lands.

Declined for now: restaurants, medical and dental, salons. The POS or
practice-management vendor owns the daily journal, tips, or claims,
already sells the AP and payroll layer, and per-transaction human approval
does not scale to 300 tickets a day or a claims queue. Anything that puts
PHI in the engine is out of scope (section 12).

Why two-way: archetypes shape lanes and the domain roadmap (section 7),
never the core; a fourth archetype or a reversal on a declined one adds
lanes without touching what is built. Design partners shape lanes, never
the roadmap (risk 7).

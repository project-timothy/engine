# Time is UTC with a named zone, resolved from plain words at setup
Date: 2026-10-09
Type: Two-way door

The ledger keeps every instant in UTC; each tenant carries an IANA zone name
(`[identity].timezone`), and every calendar decision (which day, which month)
converts through it at the edge (`core/engine/clock.py`, #136). A zone name,
never a fixed offset: an offset carries no daylight-saving rules, and an
abbreviation is not an identifier (CST is Chicago to an American and Shanghai
to a Chinese reader). This is the common practice across the IANA tz project,
PostgreSQL guidance (timestamptz plus a zone name for display), and database
design guides: store UTC, store the zone's name, convert for people.

What changed on 2026-10-09: the Tim walkthrough's personas answered the time
zone question as people do ("nepal", "Eastern", "CAT (UTC+2)"). Onboarding
stored each as typed, doctor called the tenant ok, and the read server died on
start. Onboarding now resolves the answer to a zone name (exact names,
everyday names, single-zone countries, cities, from the host's IANA tables)
and asks again in plain words for a country with several zones, an ambiguous
abbreviation, or a bare offset; doctor names a tenant whose zone is not real
(#457).

Next, when it is needed: a person's own zone beside the tenant's (a church in
Ohio, its missionary in Nepal), for reminders that reach that person at a
sensible hour. Same rule: a zone name on the person, UTC in the ledger.

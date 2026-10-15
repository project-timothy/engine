# Routing

The clock behind every card on a route (docs/tenant-kit-design.md, section 3,
"Routing: one document, on a clock"; #436). Runs only for a tenant with
`authority.toml`; for any other tenant every job is a no-op that says so.

## Jobs

- **tick**: for every pending card, works out its route's current step, who
  it waits on and its date (the document's deadline, else the step's window),
  and records one `route.reminder` event per person and level as each comes
  due. The levels, never about the person:
  1. a reminder to the approver, copied to no one;
  2. a second reminder offering a handoff (`engine queue handoff`) or an
     "on it by" date (`engine queue on-it`), which pauses the clock;
  3. the person's named backup, as "covering for", only when
     `[routing].backup = true`;
  4. upward to `[routing].upward_role`, organization shape only, worded about
     the document and its date.
- **notices**: the emails the box sends (#453's delivery half, 2026-10-13).
  A reviewer (a `review` grant over the person's unit, not the person, not
  someone the card already waits on) hears once per card that a person's
  month is ready (`month_ready`, naming the person and the month). Anyone a
  pending card waits on, and whose grant decides it, hears at most once a
  day that a yes is waiting (`waiting`, naming nothing). On Fridays, a
  person with a drive in the last seven days that nobody has said what it
  was for hears once that day (`drives_week`, naming nothing, never a place
  or a count of miles). Each is a
  `route.notice` event (id, to, kind, about, month, day): never a figure or
  what a card is. `engine notices <tenant>` prints the last week's as JSON
  lines; the doorkeeper's `send-notices` words each one and sends it once to
  the address on the roster.
- **delegate** / **revoke**: record a dated delegation (`route.delegated`) or
  its end (`route.revoked`). Called by `engine queue delegate|revoke`, which
  checks that a person at a terminal is giving away a role they hold
  directly; the job checks the delegation's own rules again.

## Boundaries (never)

- Never decides a card, never moves money, never sends anything itself.
  Reminders are ledger events, read by `engine queue waiting` and the
  process owner's brief line; notices are events the box's doorkeeper words
  and mails (no figures, the roster's own addresses only).
- Never writes `authority.toml`. Delegations are dated ledger events that
  lapse on their date without anyone revoking them.
- Never names who has not acted to anyone but that person and the process
  owner.

## Inputs and outputs

- In: `authority.toml` (`[routing]`, people, roles), the pending cards.
- Out: `route.reminder`, `route.notice`, `route.delegated`, `route.revoked` events.

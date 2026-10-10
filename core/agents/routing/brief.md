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
- **delegate** / **revoke**: record a dated delegation (`route.delegated`) or
  its end (`route.revoked`). Called by `engine queue delegate|revoke`, which
  checks that a person at a terminal is giving away a role they hold
  directly; the job checks the delegation's own rules again.

## Boundaries (never)

- Never decides a card, never moves money, never sends anything. Reminders
  are ledger events, read by `engine queue waiting` and the process owner's
  brief line; delivering them by mail or Telegram waits for the notification
  channel (#453).
- Never writes `authority.toml`. Delegations are dated ledger events that
  lapse on their date without anyone revoking them.
- Never names who has not acted to anyone but that person and the process
  owner.

## Inputs and outputs

- In: `authority.toml` (`[routing]`, people, roles), the pending cards.
- Out: `route.reminder`, `route.delegated`, `route.revoked` events.

# Security charter

The rules that must never break, each tied to the tests that prove it. A
reviewer should be able to read this page, pick any rule, open the tests it
names, and see the rule fail if the code bent it.

How the tie works: a test proves a rule by carrying its marker,
`@pytest.mark.security("SEC-05")`. The gate `core.evals.security_charter`
runs in `scripts/check.sh` and in CI, and it fails the build when a rule has
no test, when a marker names a rule this page lacks, or when a rule's
"Proven by" line disagrees with where its markers really are. Add a rule by
adding its heading here and its marker on a test in the same PR; the gate
refuses one without the other.

Contents: [The authority floor](#the-authority-floor) ·
[The door](#the-door) · [The books](#the-books) ·
[Boundaries](#boundaries) · [Held by the red team](#held-by-the-red-team) ·
[Findings and their clocks](#findings-and-their-clocks)

## The authority floor

These live in code (`core/authority`, `_floor`), and they run before any
grant is read, so no `authority.toml` can switch them off.

### SEC-01: Money and outside sends happen only through a card

A payment is released and a message leaves the building only as the decision
on an approval card (invariant 7), for every shape the kit ships.

Proven by: tests/unit/test_authority.py

### SEC-02: No agent releases money

`[money].out` is `"human"`, the only value the engine accepts. An agent
granted `release` is still refused.

Proven by: tests/unit/test_authority.py

### SEC-03: A person confirms an authority change, and never the one who proposed it

Agents never confirm a change to who may do what. Where the shape asks for a
second person, the proposer cannot also confirm.

Proven by: tests/unit/test_authority.py

### SEC-04: An agent that reads outside documents never proposes an authority change

Text in an email, a receipt or a statement can never start a change to the
rules, because the agent that reads it cannot propose one.

Proven by: tests/unit/test_authority.py

### SEC-05: Nobody decides a card that covers them

A plan or rule that covers a person is adopted by someone it does not cover,
even where that person may approve their own spending. A plan that names its
own adopter does not load.

Proven by: core/agents/policy/evals/test_plan_card.py

### SEC-06: Every authority change is written down

Each propose and confirm carries the record flag, and the record names who
proposed and who confirmed.

Proven by: tests/unit/test_authority.py

### SEC-07: Nobody grants what they do not hold

A change is checked against what the confirmer holds, so authority only ever
flows from someone who has it.

Proven by: tests/unit/test_authority.py

### SEC-08: The new-vendor decision is human-only

No agent shell decides it, no one writes its gate fields by hand, an older
unstamped card of that type is still human-only, and in a list of cards it
always gets its own tap.

Proven by: tests/unit/test_door_batch_yes.py, tests/unit/test_queue_human_only.py

## The door

The door is how a person's yes reaches the engine from their phone. The
doorkeeper is a separate service; these are the engine's half.

### SEC-09: A yes through the door is the person's own tap

No terminal and no witness is no yes. A tenant that did not open the door
refuses a witness, a witness of anything else is refused, a reply the engine
cannot read as a yes is none, a wrong secret hears nothing, and a tap from
someone else's phone decides nothing.

Proven by: tests/unit/test_door_batch_yes.py, tests/unit/test_door_presence.py

### SEC-10: Only the named person may vouch, and never on their own card

A vouch comes only from the person the ministry named for it, never on a card
of their own, and never as a second vote on a card they already voted on.

Proven by: tests/unit/test_door_presence.py

### SEC-11: One tap on a list decides only the cards that were on it

A card added after the link went out never rides along, a card the person
cannot decide is left out and named, and each card keeps its own witness
record pointing at the one tap.

Proven by: tests/unit/test_door_batch_yes.py

## The books

### SEC-12: The model never types an amount

A model's reading of a photo is only a proposal until the person says yes,
their own amount wins over the reading, and with no reader nothing is counted
until they say how much (invariant 2). When the receipt page reads a photo as
it lands (card R1), the reading counts only on her tap, and an amount she said
that the receipt contradicts is not kept until she picks one. A bank
statement read off a photo or a PDF (card R2) is offered only when code proves
its lines add up to the balances it prints, to the cent, and is kept only on
her tap.

Proven by: tests/unit/test_door_receipt_amount.py, tests/unit/test_receipt_read.py, tests/unit/test_statement_read.py

### SEC-13: Every write is safe to run again

A re-run is a no-op and never double-posts; the ledger is the single home of
each write (invariant 3).

Proven by: core/evals/seed/test_phase1_incidents.py

### SEC-20: An AI's draft never puts a rule in force

When a person's own AI drafts rules from a new handbook copy
(t-rule-drafting, 2026-10-29), the handbook's pages are data it reads, never
instructions, and code decides what goes in: a draft's quote must be in the
new copy word for word, it must parse as a rule and read no looser than a
higher layer, and it is in force only on its own card's yes. Until then the
rule it replaces keeps counting. A page written to steer the AI can at most
propose a draft that a person still has to approve.

Proven by: tests/unit/test_policy_draft.py

## Boundaries

### SEC-14: Nothing under core names a tenant

The bleed-through lint scans code, scripts, the Dockerfile and dotfiles, and
only the demo and the templates live under `tenants/` (invariant 5).

Proven by: core/evals/test_bleedthrough.py, core/evals/test_tenant_boundary.py

### SEC-15: The auditor imports nothing from the engine

A checker that shares the worker's code shares its bugs. The lint catches
every import form, including `importlib`.

Proven by: auditor/evals/test_independence.py

### SEC-16: Secrets and tax IDs never reach a card, a transcript or the repository

A declared secret's value is replaced by its variable name, a TIN shape is
redacted, a planted key never reaches a card, and no secret is committed.

Proven by: core/evals/seed/test_phase1_incidents.py, tests/unit/test_redaction.py

### SEC-17: An agentic session stays inside its own folder

A line the shell would split or expand is refused, git and gh arguments that
leave the root are refused, and an extraction session reads only its own
attachments and scratch.

Proven by: tests/unit/test_sdk_extraction_read_confinement.py, tests/unit/test_sdk_gate_bypass.py

### SEC-18: Every outside call is https, and only clean values reach a query

Plain http goes only to this machine, an adapter never opens a local file,
only an ISO date or a digit id reaches a QuickBooks query, and only known
columns reach an expense-report update. The doorkeeper is https or loopback.

Proven by: tests/unit/test_door_presence.py, tests/unit/test_security_guards.py

### SEC-19: Every dependency is pinned

An unpinned addition fails the build (invariant 9). The advisory scan in
`scripts/check.sh` runs on the same lock.

Proven by: core/evals/seed/test_phase1_incidents.py

## Held by the red team

The proving ground attacks a whole box the way an outside reviewer would
(`proving_ground/redteam.py`); every judge there is an attack that must fail.
These are outside this repository's gate and run with the scenarios job.
They hold the doorkeeper's half of the rules above:

- Sign-in and tokens: `red_code_replay`, `red_code_replay_revokes`,
  `red_wrong_verifier`, `red_redirect_elsewhere`, `red_lookalike_client`,
  `red_refresh_rotates`, `red_refresh_reuse_revokes`, `red_old_refresh_refused`.
- The door: `red_door_no_token`, `red_door_forged_token`, `red_door_huge_body`,
  `red_live_forged_host`, `red_live_spoofed_network`, `red_live_no_trace`.
- One person, their own books (SEC-09): `red_only_her_cards`,
  `red_decide_not_hers`, `red_category_not_hers`, `red_month_outside_units`.
- The tap (SEC-09, SEC-11): `red_witness_link`, `red_witness_other_phone`,
  `red_witness_replay`, `red_batch_link`, `red_batch_late_card`,
  `red_batch_other_phone`, `red_batch_replay`, `red_month_button`,
  `red_month_bent_button`, `red_month_other_phone`, `red_month_grant_twice`,
  `red_month_made_up_grant`.
- Receipts: `red_receipt_link_twice`, `red_receipt_made_up_link`,
  `red_receipt_not_a_photo`, `red_receipt_other_origin`, `red_receipt_too_big`.
- File tricks (the weekly attacker's, 2026-10-16): `red_file_svg`,
  `red_file_empty`, `red_file_statement_on_one_receipt`,
  `red_file_pixel_flood`, `red_file_huge_statement`.
- The plan (SEC-05): `red_plan_card`.

The weekly attacker (`proving_ground/attacker.py`, board chunk 6) runs on the
Mini every Sunday inside the lane container: the file tricks, instructions
hidden in a receipt photo, a forwarded email and a bank statement for a real
model playing the person's AI, a model briefed as a tester, and a dependency
scan. A hit is a finding here (a `security` issue with its severity) and
becomes a red-team judge in the list above.

## Findings and their clocks

Every security finding is a GitHub issue labeled `security` plus one
severity label. The clock starts the day the severity label goes on, and the
finding is due within its window:

| Label | Window | Meaning |
|---|---|---|
| `sev:critical` | 7 days | Money or a secret can leave today, or one person can reach another's books. |
| `sev:high` | 30 days | A path to either exists, with a step an attacker must still take. |
| `sev:medium` | 90 days | A safeguard is weaker than designed; no path to harm is known. |
| `sev:low` | 180 days | Hardening and hygiene. |

The windows follow FedRAMP's remediation deadlines (high 30, moderate 90,
low 180), with critical tightened to 7.

`uv run python -m core.evals.security_clocks` lists each open finding with
its due date and says how many are overdue (`--json` for the build board). It
reads GitHub, so it is a report, never a gate. A finding is closed by the PR
that fixes it, and that PR adds or marks the test that proves the fix, under
an existing rule or a new one.

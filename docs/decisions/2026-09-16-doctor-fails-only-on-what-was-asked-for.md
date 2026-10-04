# engine doctor fails only on something the tenant asked for
Date: 2026-09-16
Type: Two-way door

`engine doctor <tenant>` is the pre-flight for a whole install (row 7.21): the
tenant file loads, the configured lanes have what they need, the folders exist
and are writable, the ledger is a repository with a remote when the nightly
push is scheduled, the scheduler and every command its crontab names are
present. Exit 0 when nothing is owed; non-zero with one line per missing item.

**Three statuses, and only one of them fails.** `ok`, `MISSING`, `skip`. A
tenant with no mailbox and no accounting connection is a legal tenant: those
lanes are out of scope, they land on `skip` lines, and the exit code stays 0.
A doctor that nagged about lanes nobody asked for would be argued with once
and ignored after that, the way any crying-wolf check is. Doctor fails only on
something the tenant asked for and did not get: a token file named and absent,
a model tier that calls a provider with its key variable unset, a folder the
tenant names that nothing created, a scheduled command that is not on this
host.

**`[secrets]` is a registry of names, not a requirement list.** `secrets.ref`
carries the same list; an archetype declares the accounting variables before
anything on the host consumes them (nothing in `core/` resolves one today: the
accounting adapter reads its own token file). So an unset declared secret is
reported by NAME and never fails the install. When an adapter starts resolving
a logical name, its own check goes beside the others and names the lane doing
the asking.

**A secret's VALUE never leaves the module.** Doctor reads the variable name
from `tenant.toml` and reports set or unset. Same for `HC_PING_BASE`, which is
a URL with a ping key in it. The W-9 TIN discipline, extended to tokens (PRD
v2 5.4). One test plants a value in the environment and asserts it appears
nowhere in the output.

**A freshly created tenant owes exactly one thing: a remote for the ledger
push.** `engine init` builds the folders and the ledger; the 23:00 push is
scheduled by default and a fresh ledger has no remote, so that job would fail
every night from the day the box was installed. Doctor says so on day one, and
`docs/install.md` makes it a step. Turning the entry off in
`[host.schedule].ledger_backup` is the other legal answer, and doctor accepts
it: the tenant said it did not want the push.

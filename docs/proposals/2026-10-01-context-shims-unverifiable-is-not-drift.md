# proposal: an unverifiable context shim is a coverage gap, not drift

Candidate: 9ef65b3a

Lens 9 (`auditor/lenses/context.py`) verifies the stamped shims a generator
builds from the owner's context repo: line 1 carries the source commit and a
hash of every byte after it, and the lens recomputes that hash nightly. A
hand-edit is the failure mode that killed the predecessor system, so the lens
is deliberately the scream.

Some of those shims live on a cloud-sync mount. A sync client may evict any of
them to a dataless placeholder, and reading a placeholder from a headless
session fails with `EDEADLK` without hydrating it. On 2026-08-11 that error
took all five context checks down for the night; `#106` fixed the crash by
mapping `EDEADLK`, and only `EDEADLK`, to a per-shim `cloud-only` WARN and
carrying on. That fix was right and it is not what this row changes.

What `#106` left behind is the verdict. The lens has two outcomes — clean, or
drifted — and a placeholder is neither. It gets filed as a WARN beside
`hand-edited` and `stale-source`, with a remedy that is a manual act per
file: pin this one "Always Keep on This Device". The next eviction raises it
again, per file, forever.

That is not hypothetical and the shape is unusually clear. Lens 19 has counted
this class at four subjects: three shims under one synced documents tree,
raised together on a single morning, and a fourth on a cloud-storage mount two
days later. Every one was resolved by the owner pinning that file by hand
within the day — four hand pins for one arrangement. The lens reported the
symptom four times and never once reported the cause, which is that shims the
audit must verify are sitting on a mount whose contents can be withdrawn.

**The candidate names two branches and only one of them is in this
repository.** "The shim writer hydrates on write" belongs to the generator,
which lives in the owner's context repo and is not this engine's code. The
other branch — the lens stops reading a placeholder as drift — is here, and it
is also the better half: hydration on write cannot help a file evicted three
weeks after it was written.

## Design

**The mechanism.** One three-valued verdict in place of today's two, and one
finding about the arrangement in place of N findings about its symptoms.

1. **`shim_verification_state(path, *, read)`** returns one of three states:
   - `verified` — the read succeeded and the stamp recomputes clean;
   - `drifted` — the read succeeded and the content disagrees with its stamp
     (`no-stamp`, `hand-edited`, `stale-source`). **Unchanged, severity and
     all**;
   - `unverifiable` — the read could not happen at all (`EDEADLK` on a
     dataless placeholder). The lens learned nothing about this file's
     content, which is a different fact from learning something bad.
2. **`unverifiable_findings(subjects, *, sync_roots)`** reports the
   unverifiable set as a **coverage gap about a location**: where every
   unverifiable shim sits under one configured sync root, it raises **one**
   INFO finding naming that root and how many shims under it could not be
   read, rather than one WARN per file. Shims not under any configured root
   keep a per-file finding — an unexplained unreadable shim is its own
   question.
3. **`[auditor.context].sync_roots`**, a list of path prefixes, is how a
   tenant says which trees are synced. **Absent means today's behaviour
   exactly**: no root configured, no grouping, per-file findings as now.
4. **Persistence stays lens 19's job, deliberately.** This row does not add a
   nights counter or an escalation window. The grouped finding persists while
   the condition holds, so `long-lived` escalates it on its own after
   `long_nights` — which is precisely why this must remain a finding and never
   become silence.

**Why INFO and not WARN.** Severity here is a claim about what was learned. A
`hand-edited` shim is content that will die at the next regenerate: the engine
knows something is wrong. A placeholder is the engine knowing nothing, about a
file that is very probably fine and is in any case not being read by the agent
surface either while it is dataless. Spending a WARN on it is what made four
mornings cost four hand pins, and the report's own contract is that a WARN is
answerable. The coverage gap is answerable — move the shims off the synced
mount, or accept the gap — and that answer is a decision, not a chore.

**What it must never do.**

- **Never report an unverifiable shim as clean.** Absence of evidence about a
  stamp is not evidence of a good one. The finding exists on every night the
  read fails; the only thing that changes is how it is grouped and how loud it
  is.
- **Never fold a readable-but-wrong shim into the unverifiable bucket.** A
  `hand-edited` shim stays CRITICAL. That is the failure mode the lens was
  built for, and a three-valued verdict whose middle state can absorb it is
  worse than the two-valued one it replaces.
- **Never go permanently quiet.** A shim unverifiable for weeks must get
  louder, not softer — via lens 19, which already does this, rather than a
  second timer in this lens.
- **Never hydrate by writing.** The auditor reads (independence principle 3);
  it does not touch the synced tree, the generator, or the owner's context
  repo. A read that fails is reported, never worked around.
- **Never map any errno but `EDEADLK` into this state.** `#106`'s narrow map
  is the load-bearing part; a broadened `except OSError` would turn a genuinely
  missing shim into a shrug.
- **Never group shims under a root a tenant did not configure.** Inferring
  "this looks like a sync mount" from a path substring is how a lens starts
  explaining things it cannot see.

**What this does not fix, deliberately.** It does not decide whether the shims
should live on a synced mount — that is the owner's call, and this row's whole
point is to put that call on the report instead of a per-file chore. It does
not touch the generator. And it does not retract the four findings already
resolved.

## Failing eval

`evals/proposals/test_context_shims_unverifiable_is_not_drift.py`, two
assertions:

1. **`test_an_unverifiable_shim_is_a_coverage_gap_not_drift`** — three shims
   under one configured sync root, all dataless placeholders, yield the
   `unverifiable` state and exactly ONE INFO finding naming the root and the
   count of three; not three WARNs, and not a `drifted` verdict. With
   `sync_roots` unset the same three come back as per-file findings, today's
   behaviour.
2. **`test_a_readable_shim_that_disagrees_with_its_stamp_is_still_loud`** —
   the converse fence. A shim whose body no longer matches its stamped hash
   reads `drifted`, never `unverifiable`, and a placeholder is never reported
   `verified`. This is what stops the row being a mute wearing a costume.

**Why it fails today.** `auditor.lenses.context` has no
`shim_verification_state` and no `unverifiable_findings`; `check_shims` inlines
a two-valued verdict and emits one WARN per evicted file. The eval fails on
the missing helpers, by design, and stays red until the build lane makes it
pass and moves it into `auditor/evals/`.

## Issue

**Goal.** A context shim the audit could not read stops being reported as
drift and stops costing one hand pin per file per eviction. The report names
the arrangement that makes shims unverifiable — once — and the owner decides
about the arrangement instead of clearing symptoms.

**Acceptance.**
- A dataless placeholder yields state `unverifiable`; a readable shim that
  disagrees with its stamp yields `drifted` at today's severity; a clean one
  yields `verified`.
- With `[auditor.context].sync_roots` set and every unverifiable shim beneath
  one root, the lens raises exactly one INFO finding naming the root and the
  count.
- An unverifiable shim outside every configured root keeps its own finding.
- With `sync_roots` unset, output is byte-identical to today's on the live
  tenant.
- No errno but `EDEADLK` reaches the new state; every other read error still
  propagates.
- `evals/proposals/test_context_shims_unverifiable_is_not_drift.py` passes and
  moves to `auditor/evals/`.

**Touches.** `auditor/lenses/context.py` (the three-valued verdict and the
grouped finding), `auditor/config.py` (`sync_roots` ->
`context_sync_roots`), `tenants/<tenant>/tenant.toml` (the synced trees this
host has), `docs/auditor-design.md` (lens 9's entry).

**Size.** S. One state split and one grouping. The care is in keeping
`hand-edited` CRITICAL and in proving the unset default is inert.

**Depends.** Nothing blocking. `#106`'s `EDEADLK` map and the `_shim_text`
read seam both exist and are what this builds on.

**Door.** Two-way. Unset `sync_roots` and the lens reports per file exactly as
tonight. The worst failure mode is a coverage gap stated too quietly, which
lens 19's `long-lived` kind is already positioned to escalate.

**Retires.** Candidate `9ef65b3a` (`context`/`cloud-only`, 4 subjects in 60
days, every one resolved by a hand pin on a file that was never drifted). The
particulars are in the triage note for 2026-10-01, which stays private.

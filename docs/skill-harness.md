# Skill regression harness (phase 7 row 7.14)

A prose skill (`tenants/<tenant>/skills/<name>/SKILL.md`) is executed by a
Claude Code session, so CI cannot run it. What CI can hold still is the
contract around it: the shape of the note and the markers the skill text
must keep. `core/llm/skill_harness.py` is that library;
`tests/skills/test_skill_contracts.py` runs it on every push, no network, no key.

## The contract file

Each skill folder carries a `contract.toml` beside its `SKILL.md` (an
unknown field is an error, so a typo cannot silently switch a check off):

```toml
name = "audit-triage"
required_sections = ["New findings", "Root cause", "Actions taken",
                     "Recommended actions for the owner", "Recurring patterns",
                     "Watch list"]
banned_strings = ["gh pr merge", "git push origin main"]
first_line_never = ["preliminary"]
skill_must_mention = ["AUDIT_TRIAGE_STAGE", "AUDIT_TRIAGE_NOTE_STAGE", "never merge"]
```

- `required_sections`: headings the note must contain, in this order. A
  heading is any `#` line or a line that is only bold text; a section
  matches a heading whose text starts with it, case-insensitive, so
  `## Matched (unattended)` satisfies `Matched`.
- `banned_strings`: case-insensitive substrings that must never appear in a
  note. Each names something the skill is forbidden to do (the triage never
  merges, the sweep never authenticates or runs an engine job), so its
  presence in a note is evidence the session did it.
- `first_line_never`: words that mark a note unfinished. The headless
  wrappers treat a note whose first line still says `preliminary` as a
  failed run; the harness enforces the same rule on the fixtures.
- `skill_must_mention`: strings the `SKILL.md` itself must contain, verbatim.
  Together with `required_sections` (also checked against the skill text),
  this is the static gate.

## The two checks

- `check_note(note_text, contract) -> list[str]`: one violation per missing
  or out-of-order section, per banned string found, per unfinished marker
  in the first line. An empty list is a pass.
- `check_skill(skill_text, contract) -> list[str]`: every required section
  name and every `skill_must_mention` marker must appear in the skill text.

## How a skill edit is gated

Rename a section in `SKILL.md` (say `Recurring patterns` to `Repeat
offenders`) and `test_each_skill_passes_its_static_check` goes red: the
contract still names the old heading. The edit lands only when the
contract, the fixtures under `tests/skills/fixtures/<skill>/`, and any
consumer of the section (the morning brief lifts two of the triage
sections by name) move in the same PR. Dropping a guardrail marker such as
`No authentication, ever` fails the same way.

Fixtures are synthetic (no real vendor, amount, or person): one good note
per skill, one bad note per skill with a renamed section plus a banned
string. The good note is the executable example of the contract.

## A skill with two outputs (the build lane, row 7.6)

The build skill writes a note AND opens a PR, so it carries two contract
files: `contract.toml` for the note (Issue, Eval, Gates, PR, Decisions this
row did not specify, Handoff) and `pr-body-contract.toml` for the PR body
(Goal, Eval, Verification, Issue, Decisions this row did not specify,
Checker review; `gh pr merge` banned). Both run through the same
`check_note` and `check_skill`; the fixtures are `good-note.md`,
`bad-note.md`, `good-pr-body.md`, and `bad-pr-body.md` under
`tests/skills/fixtures/build/`. A rename of a PR heading in `SKILL.md`
fails `test_a_build_skill_edit_that_drops_the_pr_body_verification_fails_statically`.

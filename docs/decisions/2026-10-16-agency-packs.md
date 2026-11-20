# Agency packs: a sending agency sets up as itself
Date: 2026-10-16
Type: Two-way door

Designed 2026-10-15, built in chunk 8. Board item t-agency-pack.

## Decision

A sending agency's ministry is set up from a **pack**, a folder the host names
(`engine mcp --onboarding --pack <folder>`); the person setting up never names it.

- `pack.toml`: the agency's name, the shape (an organization only), the categories
  it adds to the starter list, and every number it sets as a `[[number]]`: where it
  sits in the authority file (`at`), the amount, Tim's words for it, and its
  `source` (document, section, page, the words).
- `authority.toml`: the agency's roles, safeguards and routes, in the authority
  file's own format, with no people. Setup adds the people and whom each looks
  after.
- `rules/`: its handbook layers (core/policy), beside the copies they were read
  from. The morning watch keeps reading them once the ministry runs.

`engine pack check <folder>` refuses an amount in the authority file that no number
cites, a citation that no longer matches what the file sets, a rule naming a role
the pack lacks, and a quote (a rule's or a number's) no longer in its copy. Setup
refuses to serve a pack that does not check clean.

Setup from a pack offers the pack's roles, never asks the numbers the pack sets
(the second-person line, the two-approval line, the dollar line), and lays the
pack's files down as the new ministry's own, with `pack.toml` kept beside them.

## Why

The owner's decisions of 2026-10-15: numbers and the shape of the organization
vary freely per agency; the logic only by choosing among steps the engine already
has, never logic written in config. An agency runs as itself, never on top of a
church (the archetypes never stack). Every number an agency's people live by
cites the page it came from.

## What it does not do yet

- A pack is laid down once, at setup. A later edit to the pack reaches a ministry
  by a new rules file (a card, as any rule) or an authority change (proposed and
  confirmed); several ministries inheriting one pack is later work, when a second
  ministry needs it.
- Every number a pack sets is the agency's own. "Open to the ministry" numbers wait
  for the same day.

## Found while building it

A rule that names a role ("your team leader's OK") let anyone holding the role
give the OK, whatever their scope. Harmless in a church with one pastor; in an
agency with several team leaders, any one of them could OK anyone's purchase.
core/authority now holds a rule-named OK to what its giver can see: a team leader
OKs only for the people they look after.

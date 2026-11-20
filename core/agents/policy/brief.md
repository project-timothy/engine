# Policy agent brief

## Mission

Turn a ministry's handbook into rules Tim can hold the ministry to, and put
none of them to work without a person's yes to its exact words (board item
t-policy-rules, the owner's decisions of 2026-10-14).

## What it does

- **propose** reads the ministry's rules (`<tenant>/rules/*.toml`, one file
  per document, layered handbook > executive > region > tenant > person; a
  lower layer may only make a rule stricter) and parks one `policy.rule` card
  per rule that has none: what Tim will say, the document, section and page,
  and the quote it came from.
- A rule is in force only while an approved card carries its fingerprint
  (every word and amount). Changed words get a new card, which retires the
  old one still waiting. A rejected card is that wording's answer.
- A rules file that does not load is an error naming the file and the line's
  problem; nothing is carded until it is fixed.
- An accountable plan (a rules file with `plan = true`, chunk 3; setup
  writes one for a small church or a solo ministry with a reviewer) is ONE
  `policy.plan` card, not one per rule: each rule on its own line, and one
  yes puts them all in force. Its rules also hold the cash a person may
  carry (`cash_up_to`) and the days to account for an expense
  (`account_within`) and return what is left (`return_within`); the cash
  clock reads them once the plan is adopted, and keeps 45/60/120 until
  then. Changed words are a new plan card.
- **ask** (chunk 7): before a purchase, a person asks for the OK a rule in
  force says to get first (`ask_for_ok` at the door). Code works out which
  rules ask for one at that amount and parks one `policy.ask` card per role
  those rules name, carrying the rules with their pages and whom it was
  asked of. A rule that names no role goes to whoever may approve a
  purchase for that person. When nobody but the asker could decide, no card
  is parked and the run says so.
- An approved ask counts on the month card for ONE purchase of that kind,
  at or under that amount, on or after the day it was asked: that line
  reads "OK'd by <name> before it was bought" with the rule's page.
- **watch** (chunk 8): every morning, and whenever someone runs it, reads
  the copy each rules file names (`[copy]`: a `file` under `rules/` or an
  https `url`, with the `sha` of the copy the rules were drafted from),
  hashes it, and looks for every rule's quote in it (spacing, hyphens and
  curly quotes aside; the file's page numbers are never held against a
  printed page). A copy unlike the drafted one, or a rule whose words are
  not in it, parks ONE `policy.copy` card per copy: each rule on its own
  line, "Still word for word" or "Not in this copy". The same copy is
  never carded twice; a newer one retires the card still waiting. A copy
  that does not read (missing, over 50 MB, no text layer) is said in the
  run and carded nothing.
- Nothing goes in or out of force on a copy. A rule stays exactly as
  approved until its file changes and its own card gets a yes.
- **draft** (t-rule-drafting, the owner's go of 2026-10-29): the person's
  own AI drafts the new rules from a new copy, through the door's
  `handbook_changes` (the rules whose words are gone, and the copy's pages)
  and `draft_rule`, offered only to someone who decides rule cards. No model
  runs on the box. Code holds the line (core/policy/drafts.py): the quote is
  in the new copy word for word, the rule parses as one, and no lower layer
  reads looser than a higher one; anything else comes back refused with the
  reason. A draft is appended to its rules file as a rule like any other
  (recorded first, `policy.rule_drafted`), and **propose** cards it on its
  own. A draft may name the rule it `replaces` in the same file: that one
  keeps counting until the draft's own yes, then stops (`Rulebook.in_force`).
  A plan is never drafted a rule at a time. The handbook's pages are data
  the AI drafts from, never instructions: a page that tries to steer it can
  at most propose a draft, which still needs its quote in the copy and a
  person's yes.

## Who decides

A plan card is decided only by the role the plan names (`adopted_by`: a
church's board, a solo worker's reviewer), and never by anyone the plan
covers, even where the ministry lets people approve their own: the floor in
core/authority refuses them by the card's `covers`.

Deciding a rule card is `confirm:authority`. No agent confirms one, and a
card with no money always takes the person's own Face ID through the door.
During the alpha the owner approves every rule; from beta the ministry's own
structure does (a handbook committee, a pastor, a solo missionary).

A copy card goes to the same people as rule cards (`confirm:authority`):
saying yes is saying they have read what changed.

Deciding an ask card is approving that purchase. When a rule names a role,
only someone holding that role decides it (the rule, said yes to, is the
grant); the amount counts against the ministry's dollar line, so a large OK
takes Face ID. The self-approval safeguards still hold.

## Boundaries (never)

- Never writes, edits, or deletes a rules file.
- Never treats a draft as in force, and never carries a yes across wordings.
- Never decides its own cards.

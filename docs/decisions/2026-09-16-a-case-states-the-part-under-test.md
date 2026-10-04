# An eval case states the part under test, and a refusal is not a reply
Date: 2026-09-16
Type: Two-way door (phase 7 row 7.13b, issue #272; follows 2026-09-16-eval-gated-tier-changes)

Row 7.13 built the eval gate and left `scan_group` ungated for one reason:
its reply contract, `GroupingReply`, landed with row 7.11 (PR #269) after the
gate was written, so no case had a class to validate into. Both are on main,
so this row writes the set and turns the gate on for the last `complete_for`
site the engine has. Four gated jobs now, and the only ungated ones are the
auditor's vendored `draft_advisory`, the multi-turn `audit_triage` runner
lane, and `w9_detect`, which is deterministic by policy.

The set itself is four cases, and writing them forced five choices the row
did not specify.

## 1. An expectation is a subset all the way down

The grouper is the first gated contract whose reply holds a LIST of objects:
`{"groups": [{"pages": [1,2], "vendor": "", "amount": "", "date": ""}, ...]}`.
Only the pages are under test. The brief calls vendor, amount, and date
claims, says they may be empty, and the split pass puts them nowhere but a
child FILENAME, where extract re-verifies them anyway. Asserting them would
also make a bad case by this repo's own rule: on the live seat the model read
`GREENFIELD MARKET` off the page and answered `Greenfield Market`, and both
are right.

The harness compared top-level fields exactly, which cannot say "these pages,
and never mind the rest". Two ways out were available and rejected. Writing a
probe prompt that tells the model to leave the naming fields empty measures a
task production does not run. Asserting the whole object makes a case that
fails on capitalisation.

So `expect` stays a subset, and the rule now reads downward: a dict
expectation checks the keys it names, a list checks position by position with
the length under test. One property makes this pay for itself twice: a nested
expectation is still a VALID partial reply, so the seeded fixture adapter can
keep answering from `expect` alone and the contract fills in the rest. No
second field in the case file, no inverse projection, nothing to drift. The
results file still records the whole reply, so a reader sees every value that
was not asserted.

## 2. `{page_count}`, because the live ask names it

The site asks about "the attached 4-page scan", counting with pypdf. A job's
`user` template is one string for the whole set, so a per-case count has to be
a substitution: `{page_count}` is the pages of that case's document. Dropping
the count instead would have made the probe HARDER than production for no
gain, and a model that miscounted an attachment would then fail a case for a
reason the 08:00 run does not have. A false red is expensive here: the gate
refuses config load on red results, so it would take the tenant down.

## 3. A multi-page document is one string with form feeds in it

Every case document is `conftest.minimal_pdf(case["text"])`, pinned by a test
so the file and the inlined text cannot drift. A combined scan needs several
pages, so `minimal_pdf` now starts a new page at each form feed. The
single-page object layout was generalised, not replaced: for one page it
emits the same bytes it always did, and the three eval sets already committed
prove it byte for byte. The alternative, stitching pages with pypdf, would
have made the document something other than "the PDF of the case text" and
cost that invariant for every set.

## 4. The refusal is tested where the rule lives

The row asked for a negative case "whose expected outcome is a refusal". The
harness cannot express that, and it should not: a case states what a right
reply looks like, while the refusal belongs to the code that reads a reply.
`GroupingReply` validates a grouping that lists page 2 twice perfectly well;
`validate_groups` is what rejects it, and the scan is then HELD with an
anomaly and nothing is filed. So the negative is proved in
`tests/unit/test_scan_group_eval_set.py`, against a real case document, in
both shapes the row named (a page listed twice, a page dropped), next to a
test that the contract accepts what the code then refuses. That pairing is
invariant 2 in one file: the model proposes, the code decides.

The fourth case carries the same failure mode as its subject. Page 2 of
`a_terms_page_belongs_to_its_receipt` is the back side of a receipt, with no
vendor header and no total, and a grouper that drops it as "not a receipt"
returns pages `[1, 3]` of a 3-page scan. That is the live way this job fails:
not a wrong split, a held scan.

## 5. The probe prompt is pinned to the site's, and says so out loud

Row 7.13 decision 5 keeps the set's prompt a fixed probe and flagged
**[NEEDS REVIEW]** that nothing tells anyone when a site's prompt has moved
under its set. For this job a test does: it asserts the probe's system turn
IS `_GROUP_SPEC` and its ask IS `_GROUP_ASK` with the page-count token. A
reworded brief now turns that test red, and a reviewer decides whether the set
is re-scored on the new wording or deliberately left on the old one. It is one
job's answer to a general gap. **[NEEDS REVIEW]** the other three sets carry
no such alarm, and the same test would fit them.

## What merging changes about the 08:00 run: nothing

No job, no key, no prompt, and no tenant file moved. Both shipped tenants
point `scan_group` at a tier that now needs evidence, and both have it: the
seat scored 4 of 4 twice, byte-identical apart from the timestamp, and the
fixture model scores the harness for the demo tenant and every `engine init`
tenant. The day this bites is the day somebody repoints the grouper, which is
the point.

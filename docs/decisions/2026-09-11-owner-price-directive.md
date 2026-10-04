# The owner can price a receipt the extractor could not
Date: 2026-09-11
Type: Refines 2026-08-14

The first vendor-role expense report (a contractor's, 23 receipts)
stalled on one file: a single scanned page holding two receipts (airport
parking and a gas pump), which the extractor correctly refused to
price ("no amount found; the review card must resolve this line"). The
report job then refused to build ("resolve extraction first"), and no
owner path existed to resolve it: a proposal is keyed by the receipt's
hash and never re-extracted, the split directive only divides an amount
that already exists, and reference reclassification is the model's call.
One unreadable receipt blocked a whole report.

The fix is the smallest primitive beside the split directive: a
`price:<sha16>=amount:category[:note]` param on `expenses report`. The
owner's value is recorded as `expense.owner_priced` and overlays the
proposal on every later run, so the approval that builds the report needs
no param. A correction of an extractor amount is allowed and keeps the
previous amount in the event. Malformed values and prefixes naming no
receipt refuse the whole build and record nothing (the split rule,
invariant 2). Two receipts on one page therefore go: price the page, then
split it at approval.

Never a guess: the extractor still says "no amount" when none is printed;
only the owner supplies one, and only through the directive.

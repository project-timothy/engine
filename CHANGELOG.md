# Changelog

What changed between releases, newest first. Every section calls out two
things an operator must act on before upgrading: **tenant files** (a key in
`tenant.toml` or `vendors.toml` added, renamed, or removed) and **ledger
schema** (a migration; the engine applies it on the first run after the
upgrade, and there is no down-migration, so back up the ledger first).

`engine --version` and `auditor --version` print the version you run and the
commit (or, in a container, the image). A release is a `vX.Y.Z` tag on the
merge that sets `version` in `pyproject.toml`; that merge moves the
Unreleased section under the new number.

## Unreleased

**What waits under Tim's message, after the year on his own voice
(proving-ground #112, 2026-11-18).** A tool's link for her to tap (a
receipt's camera, a catch-up page) shows as a button under his message
(`Waiting.link`); the model is told the page shows it and never sees the
link to copy, and any Markdown link it writes anyway reaches her as its
words only. Her yes page button remembers the cards it decides, and her page
drops it once none of them is waiting. **Tenant files:** none. **Ledger
schema:** none.

**Tim's page after the second live trial (2026-11-18, the owner's phone on
practice).** Where Tim's voice is on, the page shows him working the moment
she taps Send, and Send can't go twice (its one script, under the page's
nonce; a voice-off page stays byte for byte as pinned); the box answers a
repeat of her last words, sent while Tim was still on them, with the reply he
already gave (`REPEAT_SECONDS`), and runs her messages one at a time. The
brief brings the waiting item up only when she asks (her page already shows
it on top) and sends a tax question to her packet before Tim says he can't
answer; the `talk` set's tax case now asks it mid-conversation, and Haiku,
Sonnet, and Opus 5.5 pass it 5 of 5 again. A stray closing brace at the end
of a reply is trimmed. **Tenant files:** none. **Ledger schema:** none.

**Real models scored on Tim's voice (`core/llm/eval_sets/talk/results`).**
Claude Haiku 5.5, Sonnet 5.5, and Opus 5.5 each pass the `talk` set 5 of 5
on the `anthropic_messages` adapter (the first live calls through that
adapter), so a ministry may now name any of the three for `talk` or
`talk_hard`. **Tenant files:** none. **Ledger schema:** none.

**Tim's voice has an eval set (`core/llm/eval_sets/talk`, issue #681).**
Five moments of a conversation, each checking the turn a model takes: it
reaches for the books before answering about money, reads them again when
asked where a figure came from, calls the packet for a tax question, and
calls the deciding tool for a decision she asks for. The gate now covers
`talk` and `talk_hard` (one set for both speeds). The harness gains
conversation cases (`messages`), a job-built system text (`system_from`),
and a case `seed` for a seeded run. **Tenant files:** a ministry that names
a model for `talk` or `talk_hard` needs that model's green results
committed first (`uv run engine evals run talk --tenant <slug> --tier
<tier>`); `fixture-model` ships green. **Ledger schema:** none.

**Tim's voice: a plain tier only, and earlier replies show their tools
(issue #681).** The first live trial on the owner's Claude seat failed: the
`claude_agent_sdk` adapter answers as an agent session, and the model looked
for Tim's tools there and said the books weren't connected. A `talk` or
`talk_hard` tier on that adapter now counts as not named: his voice stays
off (his plain line, no call) and a hard question stays on the everyday
model. Earlier replies reach the model with `read_from`, the tools they were
read from, and the brief says to call them again before repeating an amount
and to write no em dash. **Tenant files:** a ministry that routes `talk` to
the seat finds Tim's voice off until it names a plain tier, such as
`anthropic_messages`. **Ledger schema:** none.

**Tim's own voice (`core/talk`, Standalone Tim shot 1, card V1).** Tim can
hold the conversation himself instead of a person's own Claude or ChatGPT:
the person's words in, the same tools called as them through `Tools.call`,
Tim's words out. `engine talk <tenant> --as <person>` talks in the terminal
over the read tools. Off until a ministry's own file routes the `talk` job
to a tier (`[llm.jobs] talk = "<tier>"`); every call is priced, recorded in
`llm_calls`, and refused past `[llm.budget].monthly_usd`, the same cap the
nightly jobs use. Conversations are files beside the ledger (never inside
it), kept 90 days. The gateway takes `cache=True` to have a provider cache a
prompt's repeated opening, and counts cache tokens apart. **Tenant files:**
two optional keys on a tier's `pricing`, `cache_write_usd_per_mtok` and
`cache_read_usd_per_mtok`; left out, cached tokens are charged the input
price. **Ledger schema:** none.

**Records and catch-up.** Every receipt and drive is a record
(`core/agents/expenses/records.py`), read from the books: captured, read,
said, placed, approved, reported, sent, paid back. A review card names its
receipts and its yes is to them, so a late receipt waits on a small card of
its own and approved stays approved. Ministry drives ride the month's card
at their own day's rate. New door tools `catch_up_link` (one link for a
pile of receipts, good for a day) and `set_receipt_day` (a receipt files in
its own day's month, at that day's rate); new job `expenses date`. The
person's own Claude reads a pile (`receipts_to_read` hands it the images,
`record_reading` keeps a proposal); `agree_to_readings` is their one yes for
a month, `send_month_for_review` raises its one card, and `year_report`
says where the year stands. No tenant files change. No ledger migration.

**Mileage.** A person's phone tracker app (Traccar Client) reports to the
box's `/track`; the box keeps each drive as person, start, end and miles
(`mileage.drive`, the new `expenses drive` job) and never a position past
the drive's end. `engine tracker <tenant> <person>` makes the phone's token;
the tenant folder gains `trackers.toml` (token hashes only). No ledger
migration. New door tool `set_drive_purpose` and `month_report` drives: what each
drive was for in the person's words, priced at its own day's rate when it was
the ministry's.

**Routing.** New job `routing notices` and command `engine notices <tenant>`:
a reviewer hears once that a person's month is ready, and anyone a card
waits on hears at most once a day that a yes is waiting. The engine records
who, which kind, about whom and which month (`route.notice`); the box's
doorkeeper words and mails each one (`engine notices <slug> | doorkeeper
send-notices`). No figures in any notice. No tenant files change. No ledger
migration.

**Tenant files.** New optional `[expenses] reader` ("" by default, or
`pretend`): what reads a door receipt's photo when the person's words carry
no amount. A reading is a proposal; the person's words decide it. New seam
`core/contracts/reader.py` (`Reading`, `ReceiptReader`). Door: a receipt with
no amount in the words gets its link, and the new `set_amount` tool takes
the amount afterward (`expenses amount` job). Tenants without `reader` get
Tim asking how much after the photo. No ledger migration.

**Tenant files.** New optional `[institution]` table, the sending
organization's profile (Robb's idea 1): `name`, `categorizes` (default true;
false when the institution's own system places receipts, as AGWM's Sage
does, so Tim never guesses one), `submit_to` (empty = the books pay it out;
`pretend` = a stand-in that takes claims until an institution's interface is
in hand), and `mileage_rate` (unset = the IRS rate on the drive's day). New
seam `core/contracts/institution.py` (`Claim`, `ClaimSink`,
`InstitutionDown`); a claim the institution can't take is held and the
person hears one plain sentence, never a status code. Tenants without the
table are unchanged. No ledger migration.

**Expenses.** New job `expenses send`: for a ministry with `[institution]
submit_to`, a missionary's receipts wait on one card for their own yes, then
go to the institution, held in plain words when it isn't answering and sent
on a later run. The month job builds no report for such a ministry.
`month_report` lines say where each receipt stands. Run it on the box's
daily timer beside `expenses month`. Tenants without `submit_to` are
unchanged. No ledger migration.

**Onboarding.** The person setting up may give their own email with their
name ("Caleb Meer, caleb@example.org"), as the reviewer already could; it is
written beside them in `authority.toml`, so the invitation at apply mails them
their welcome link too. A name alone still works. No ledger migration.

**Tenant files.** A new nonprofit tenant's `[expenses.category_accounts]`
starts from a ministry starter list (Travel, Lodging, Meals, Ministry
Supplies, Communication, Medical, Housing, Other; placeholder account names
to replace). Commercial archetypes are unchanged. New job `expenses
categorize` and door tool `set_category`: a receipt's category from the
person's words, or by the reviewer's ask, recorded as an event. No ledger
migration.

**Ledger schema: ledger migration 9.** Five columns on `expense_line`
(`said_amount`, `said_currency`, `fx_rate`, `fx_date`, `fx_source`): a
receipt paid in another currency keeps the amount as the person said it and
the rate on the receipt's own date that set its dollar value. Purely
additive; every existing line reads '' in each. New job `expenses month`
raises one review card per person for a month; door receipts in another
currency get their dollar value from a free daily rates feed
(`ENGINE_FX_FEED` points it elsewhere). The door gains a read tool,
`month_report`.

**Tenant files.** New optional `[ap.provenance]` section in `tenant.toml`
(`internal_sender_domains`, `platform_sender_domains`), and an optional
`senders` list per vendor in `vendors.toml`. Both default to empty; nothing
existing is renamed.

**Tenant files.** New optional `[llm].document_tiers` (a list of tier
names) and a per-tier `zero_data_retention` (true, false, or unset). With
`document_tiers` set, a model call that carries a document (an invoice,
receipt, or scan sent as an attachment) goes only to a listed tier; any
other tier, fallbacks included, is refused before the call and recorded as
`documents_refused`. `engine doctor` lists the allowed tiers with their
retention and names any document job routed off the list. Unset keeps
today's behavior (#358).

**Auditor.** The approvals lens holds every yes said by word through the
box's door to the ministry's dollar line: over the line stamped on the card,
or with no amount or line to hold it to, is `word-yes-over-the-line`
(CRITICAL); a stamped line that differs from authority.toml's
`yes_by_word_up_to` today is `word-line-changed` (WARN).

**Act before upgrading: project codes are tenant config now.** The engine
no longer knows the `PYY_NNNN` project-number scheme; `[books.cost_object]`
in `tenant.toml` names it (`pattern`, `canonical`, optional `tag_pattern`).
With no pattern, no text reads as a project code: the hand-check card has
no project hint, receipt file names carry no project tag, and the
auditor's projects lens resolves only nicknames. A tenant that relied on
the old built-in scheme adds, before upgrading:

    [books.cost_object]
    pattern = "(?i)p\\s?n?\\s?(\\d{2})\\s?_?\\s?(\\d{4})"
    canonical = "P{0}_{1}"
    tag_pattern = "\\bPN?(?:(\\d{2})[_-])?(\\d{4})\\b"

Those are the three regexes the engine hard-coded before, and a replay of
the first tenant's ledger and receipt tree through old and new code found
no difference (#340).

**Ledger schema.** No change (still ledger migration 8).

- The agent lanes get their own container (issue #359): `Dockerfile.lane`
  (the lockfile's dependencies with the `[claude]` extra and the dev group,
  no engine code, uid 10001) and `compose.lane.yaml` (an internal network,
  a digest-pinned squid allowing GitHub and the Anthropic API only, and only
  the environment the file names). The product image is unchanged and stays
  SDK-free. Nothing schedules it yet; a tenant's wrapper runs it.
- `engine --version`, `auditor --version`, and a version line at the top of
  `engine doctor` (public #7).
- The engine refuses a ledger directory that has git history but no
  `ledger.sqlite3` (a restore that checked out nothing, or a deleted file)
  instead of starting a fresh, empty ledger over it; `engine run` exits 2 and
  names the re-clone. A first install, even with its backup remote already
  added, opens as before.
- The ledger can be restored from its remote, and a test proves it;
  `docs/recovery.md` walks a new box through it. The 23:00 job now packs the
  ledger repository (`git gc`) after the push, and `engine doctor` reports
  the ledger's commit count and size (public #13).
- AP records who mailed each invoice (`ap.provenance.recorded`, shadow only:
  a verdict, never a hold) and a new auditor lens, `provenance`. A new-vendor
  approval card can only be decided by a person at a terminal; the auditor
  raises CRITICAL on any other decision.
- `engine doctor --create-folders`, clearer configuration errors, a bounded
  read of the event log, and the Apache-2.0 `core/contracts/` package.
- `scripts/check.sh` runs every CI gate locally; CI lints its own workflows
  with zizmor (no persisted checkout credentials, no cache in the image job).
- CI: the suite passes as root, shellcheck runs on every shell script,
  accepted advisories carry a reason and a review date, and mypy checks the
  ledger, contracts, gateway, and adapters.
- The two QuickBooks push jobs moved out of `core/agents/ap/jobs.py` into
  `qbo_push.py` and `qbo_push_payments.py`, with no change in what they do;
  the payments run, complexity 28, is cut into steps of 9 or less (public #2).
- `engine doctor` fails when another tenant's credentials (its accounting
  token file, a configured mailbox, or a secrets file this user can decrypt)
  are reachable by the same OS user: one tenant per user or container.

## 0.1.0 (2026-10-04)

First public release.

**Ledger schema** at ledger migration 8. A ledger created by any earlier
build is brought forward on its first run:

- ledger migration 1: runs, events, and the approval queue.
- ledger migration 2: AP invoices and their status history.
- ledger migration 3: accounting-system bill and payment ids on AP invoices.
- ledger migration 4: expense reports and expense lines.
- ledger migration 5: the payment instrument on an expense report.
- ledger migration 6: job records (a file move is recorded before it happens).
- ledger migration 7: model-call telemetry (`llm_calls`).
- ledger migration 8: retry policy and attempt state on job records.

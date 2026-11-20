# Tim's screens: what a person sees, and what is still to build

Written 2026-11-16 from the owner's direction that day. This is the design of record for
every screen a person meets when they use Tim. Agents read this file; people read the
mockups (the 32 phone screens under `mockups/`, drawn on the "Tim Solo Welcome" design canvas).

## Contents

1. True north
2. The rules every screen follows
3. The three things that do not exist yet
4. The screen list (32), with what exists today
5. Build order (five chunks), each with its proof
6. Constraints a builder must keep
7. The mockups

## 1. True north

The complexity is real and the computer hides it. A missionary does not know what Tim can do
and will never read the FAQ (88 answers today, and growing). So Tim leads: Tim asks, the person
answers, and Tim always says the one next thing. The FAQ is for the person's AI, which already
receives it from `start_here`. If a person has to learn the machinery, the design is not done.

## 2. The rules every screen follows

1. **Tim asks, the person answers.** No menu, no list of capabilities, no "what would you like
   to do?"
2. **One question per screen**, with tap answers wherever a tap will do.
3. **Code never asks what it can find out.** A photo of the IRS letter gives the legal name and
   the Employer Identification Number. The town gives the time zone and the currency. Defaults
   (calendar year, the IRS mileage rate, the dollar line) are shown as "I'll assume these. Tell
   me if I'm wrong" and are never asked.
4. **Every answer ends with the one next thing**, or with "Nothing's waiting on you." The next
   thing is worked out by code from the state of the person's books. No model picks it.
5. **A capability shows up when it is needed**, never before. The first time a person meets
   something new (Face ID, a second currency, cash, a split receipt), Tim shows one screen:
   what is happening, why, and the rule. After that it is one line.
6. **Tim's voice is visible.** What Tim says is set in the serif face; what the person types or
   taps is in the plain face. A rule sits in a ruled box labeled "The rule."
7. **Words a person reads carry no machinery**: no card numbers, tool names, shapes, scopes, or
   file types. "Accountable plan" is said as "your ministry's expense plan."
8. **Every rule on a screen is taken from Tim's own FAQ answer** (`core/capabilities/faq.toml`).
   A screen never states policy the FAQ does not. The FAQ id is named beside each screen below.

## 3. The three things that do not exist yet

**A. The next thing** (built 2026-11-16, `core/tools/next_thing.py`; words in
`next_thing.toml`; not yet ranked: travel dates and tax facts, which matter near year end).
One function in the core that reads a person's books and returns the
single most useful thing waiting on them, in plain words, with what it takes ("about 2
minutes") and the tool or page that does it. It ranks what is missing: a yes waiting on them,
cash near its 60 days, a month ready to send, a bank statement not yet in, a plan not yet
adopted, travel dates blank, a tax fact missing. `start_here` returns it first, and every door
tool's answer carries it at the end. Today `start_here` returns who the person is and the FAQ
answers, and nothing about what is waiting.

**B. Explain once** (built 2026-11-16, `core/tools/first_time.py`; words in
`first_time.toml`; the yes page shows Face ID's why and rule under the card). A per-person
record of which first-time moments they have been shown (`shown.json` beside `setup.json`,
mode 0600, no ledger migration). The first time, the
tool answer and the page carry the full first-time words (what, why, the rule). After that,
one line. "Ask me why any time" brings the full words back.

**C. The missionary answers the setup questions.** Today a maintainer drafts a ministry file
by hand and runs the deploy; the questions in `core/onboarding/questions.toml` are answered by
the installer, in installer words. Screens 4 to 9 turn those questions to face the missionary,
and screen 4 replaces "which kind are you" with "How does support reach you? Pick every one
that fits," because one person can have a nonprofit and also be supported directly.

## 4. The screen list

Status: **built** = a page or email does this today; **partly** = the capability exists but the
screen does not match; **new** = nothing yet.

### Sign-up and the interview (mockups `Main`, `S02` to `S09`)

| # | Screen | Status | Where it lives or would live | Rule source |
|---|---|---|---|---|
| 1 | Front page: "I'm Tim." one button | new | the public website | |
| 2 | Name, email, mobile number | new | doorkeeper (a sign-up door) | `sign-in` |
| 3 | The welcome email | partly | `doorkeeper/invite.py` (today it is an invitation from a ministry) | |
| 4 | How support reaches you (pick every one) | built (c49): only "my own nonprofit" sets up; straight to me alone, or a church, is said and kept for the maintainer | `core/onboarding/interview.py`, `interview.toml`, `core/tools/interview_page.py` | `set-up-yourself`, `missionary-fund`, `gift-or-pay` |
| 5 | Picture of the IRS letter, read back | built (c49, card R2): where the box's file names `letter_read`, the photo is read and the legal name and Employer Identification Number said back for her tap; otherwise the photo is kept and she types the name | `core/onboarding/interview.py`, `core/tools/letter_read.py` | `set-up-yourself`, `letter-read-at-setup` |
| 6 | Where you live and work | built (c49): town and country set the time zone (`zones.resolve_zone`); any currency at its day's rate needs no setting | `core/onboarding/interview.py` | `other-currency`, `travel-dates` |
| 7 | Who looks over your spending | built (c49) | the interview's reviewer screen, with setup's `reviewer_approves` asks whether they also approve each month (c48) | `accountable-plan`, `month-approver` |
| 8 | Do you drive for the ministry | built (c49): asked and said back; mileage itself is the person's setup step `mileage` | `core/onboarding/interview.py`, `person.toml` | `drives` |
| 9 | Here's what I heard, with the assumed defaults | built (c49): every assumption read from the setup questions' defaults; her yes writes the ministry file | `core/onboarding/interview.py` | `why-face-id`, `set-up-yourself` |

**How the interview runs before sign-up exists (c49, the owner's calls of 2026-11-17).** A
maintainer makes a one-time link on a box (`engine interview new --name --email --by --base`)
and sends it. The link is `/start#<token>`: the fragment never reaches a server or a log; the
start page's one script posts it, and every other page is plain HTML. Her answers live under
the box's ledger root (`_interviews/`, the token kept only as its hash), the letter's photo
beside them. Her yes writes `<key>.ministry.toml`, the same file a maintainer drafts by hand
(proving-ground `practice-box/ministry.example.toml`), and `engine notices` tells whoever sent
the link (doorkeeper notice kind `interview`). `engine interview list` says where each stands.
Chunk 5 moves the same screens to self-serve sign-up and reads the letter for her.

### The first win, then Tim leads (`S10` to `S14`)

| # | Screen | Status | Where | Rule source |
|---|---|---|---|---|
| 10 | First receipt, while the box is made ready | partly | `core/tools/receipts.py` page (today it comes after adding an AI) | `who-reads` |
| 11 | Receipt page on the home screen | built; where Tim's own voice is on, his talk page is the one on the home screen (card T2) | `person.toml` step `home_screen` and its `[step.voice]` | `receipt-home-screen`, `talk-to-tim` |
| 12 | Where do you want to talk to me | partly (card T2): where Tim's own voice is on, "Right here" comes first and Claude or ChatGPT is the "also", offered to everyone and done at her first word to Tim; with the voice off, Claude and ChatGPT only, as before. "Just email me" is still new | `person.toml` step `ai_connector` and its `[step.voice]`, `core/tools/setup_page.py`, the doorkeeper's setup page | `add-tim`, `no-ai-month`, `who-reads` |
| 13 | First "Hey Tim": the one thing this week | built | `core/tools/next_thing.py`, said first by `start_here` | `next-thing` |
| 14 | Every answer ends with the next thing | built | `Tools.call` adds `next` to every answer through a person's door | `next-thing` |

### The first time (`F1` to `F6`, `M1` to `M4`)

| Screen | Status | Rule source |
|---|---|---|
| First yes over the dollar line (Face ID) | built: the answer carries it, and the yes page shows the why and the rule under the card | `why-face-id` |
| First receipt in another currency | built (in the answer) | `other-currency` |
| First cash withdrawal | built (in the answer) | `atm-cash`, `cash-clock` |
| First receipt that is partly personal | built (in the answer) | `split-receipt` |
| First Friday drives | built (in the answer, at the first drive); new as a page | `drives` |
| First month sent to the reviewer | built (in the answer, when a month is first ready); new as a page | `approved-stays`, `rules-after` |
| First bank statement: whose account | partly: the catch-up page asks | `bank-statements`, `personal-spending` |
| First gift-or-pay question, with "Some of each" | new as a page; the split is a new capability (see below) | `gift-or-pay` |
| First tax facts | new as a page | `tax-facts` |
| Tax packet ready | new as a page | `what-tim-leaves-to-your-preparer` |

All ten need B.

**One payer, some of each (the owner, 2026-11-16).** Someone who sends money every month may
send part because of the person's ministry work and part as a personal gift. The gift-or-pay
screen offers three answers: all for my ministry work, all a personal gift, or some of each.
"Some of each" asks "Of each $200, how much is for your ministry work?" in money, says the two
parts back with their percentages, and splits every later deposit from that payer the same way
until the person says it changed. The split is built in `keep_payers` (board item
`t-payer-split`, FAQ `some-of-each`): `ministry_work = "some"` with `work_part` in dollars or a
percentage, kept on the payer as an exact fraction; the part for their ministry work rounds
down to the cent and the gift takes the rest. The page itself is still to build.

### The reviewer's side (`R1` to `R4`)

| Screen | Status | Where | Rule source |
|---|---|---|---|
| The email from Tim: one tap a month | built (chunk 3): the reviewer's welcome names who asked and what it takes | `doorkeeper/invite.py` | |
| What you are being asked to do, and what you will and will not see | built (chunk 3): setup's first screen to whoever a waiting plan asks to adopt it (step `role`) | `core/onboarding/person.toml`, `core/tools/setup_page.py` | `personal-spending`, `reviewer-asked` |
| The expense plan to adopt | built (chunk 3): setup's second screen (step `plan`); its button is the plan card's own yes page, so Face ID is asked right then, never borrowed from setup | `core/tools/setup_page.py` | `accountable-plan` |
| A month to look over: what Tim checked, the one thing worth a look, one yes | built (chunk 3): owed back, the counts, three checks said passing, failing, or why not made, then the one thing worth a look, and "Approve October" | `core/tools/month_page.py`, `month_page.toml` | `what-tim-checked`, `approve` |

**What the checks are (chunk 3).** Each comes from the books, never a model: every bank line
the ministry spent has its receipt; the month fits the ministry's rules (read from the month's
own review card, the one place that check is worked out; with no rule in force it says so);
every bank line is accounted for (with no statement for the month it says so). The mockup's
"The totals match the bank statement" became "Every bank line is accounted for": Tim matches
each line, and does not tie a statement's balance, so the screen says what was checked.

**Who approves a solo ministry's month (c48, the owner's decision of 2026-11-17).** A ministry
one person runs approves its own months, and many have to. Setup asks a solo missionary whether
the person who looks over her spending should also approve each month (`reviewer_approves`,
asked only when she named someone). If so, their role gains `approve:expense.report#month`,
which decides the month's review card and no other card, so the Approve button appears for
them. She keeps her own yes for when they can't, and `report_self_approvals = "always"` stamps
it, so the approver's month page says "Daniel approved October without you, on November 20."

### Every day, and when something goes wrong (`M5` to `M8`)

| Screen | Status | Where | Rule source |
|---|---|---|---|
| The daily email: one thing, one button, never an amount | partly | the brief job | `one-email` |
| A receipt Tim cannot read | new | `core/tools/receipts.py` | `fix-an-amount` |
| A link that ran out: a fresh one, nothing lost | partly: words exist, no recovery button | | `link-stopped` |
| Paid back: the amount, the date, the three steps the month took | new as a page | | `paid-back` |

## 5. Build order

One chunk per session, one builder, its own worktree, test first. Each chunk ends with a
proving-ground scenario a persona walks.

1. **The next thing (A).** `core/` function plus `start_here` returning it first, plus the
   closing line on door tool answers. Proof: unit tests over a ledger in each state (a yes
   waiting, cash at 45 days, month ready, nothing waiting); a scenario where a new person
   says only "Hey Tim" and is led to their first bank statement with no other prompt.
2. **Explain once (B)** plus the six first-time moments `F1` to `F6`, as tool-answer words and,
   where a page exists (`yes_page`), on the page. Proof: the second time shows one line; "why"
   brings the full words back; every rule string is tested equal to its FAQ source.
3. **The reviewer's side** (`R1` to `R4`): the invitation words, the role screen, the plan
   screen, and "what I checked" on the month page. Proof: a reviewer persona adopts the plan
   and approves a month in two taps, having read no instructions.
4. **The interview faces the missionary (C)**, screens 4 to 9, writing the same ministry file
   the installer writes today. Proof: a persona's answers produce a file byte-identical to the
   hand-drafted one for the same ministry. Screen 4's "straight to me" alone depends on the
   seventh setup shape (a missionary with no nonprofit), which is its own chunk.
5. **Self-serve sign-up** (screens 1 to 3, and the box made ready during screen 10), and Tim's
   own talk page ("Right here"). Last, because a maintainer can stand up the box by hand in
   alpha, and the talk page needs a model on the box.

The remaining first-time and everyday screens (`M1` to `M8`) ride with whichever chunk touches
their page.

## 6. Constraints a builder must keep

- **The real pages load nothing from outside**: no web fonts, images, or scripts, a few
  kilobytes, for a 2G phone (see the header of `doorkeeper/pages.py`). The mockups use Source
  Serif 4 and Inter; on the real pages the serif is the system serif (Georgia) and the plain
  face is the system face. The colors already match `doorkeeper/pages.py` `_STYLE`.
- **Words live in one place**, a `.toml` beside the code that serves them, as `person.toml`
  does, with a test that fails on machinery words and on em dashes.
- **Emails never carry an amount** (`one-email`).
- **No model writes a money value** (engine invariant 2). The next thing, the first-time
  words, and every figure on a screen come from code.
- **Every person-facing capability ships its FAQ entry** in the same PR.
- The doorkeeper is a security boundary: it copies small helpers and imports nothing from the
  engine.
- Changing what the 08:00 run does is the owner's call on a coding day. The daily email's words
  (`M5`) are in that run.

## 7. The mockups

`mockups/` holds the design canvas source: one `.dc.html` per screen and `canvas.json` for the
layout. Each file's copy is literal text in the markup, so the words can be read without a
renderer. The names, amounts, and dates in them are invented (Hannah Cole, Sarah Whitfield,
Hope Springs Ministries, Nakuru).

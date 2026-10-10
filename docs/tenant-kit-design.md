# The tenant kit: design

Status: **DRAFT for owner review** (issue #360). Merging the PR approves the
shape; code follows in the issues listed under "Build order", each eval-first.
Owner direction 2026-09-27 (the issue) and three review rounds on 2026-10-08
(the decisions table below).

Contents

- True north
- The idea
- Shapes: two families on one backbone
- Where each part lives
- Owner decisions (2026-10-08)
- 1. Brand
- 2. Voice
- 3. Authority and routing
- 4. Books
- 5. Connections and data
- 6. Onboarding
- What the kit does not cover
- Build order
- Not decided here

## True north

**The complexity is real, and the computer hides it.** The model for everything
built on this engine is the ship's computer in Star Trek: a person asks for
what they need in plain words, and the computer does the work and returns the
result. Nobody aboard edits a configuration file.

Two layers make that safe:

- **A deterministic core.** Python owns every dollar, every write, every
  permission check and every record, and the nightly auditor checks it
  independently. People trust the system because the core is exact and
  verifiable, not because a model was careful.
- **Agents over the core.** The agent the tenant chooses (theirs, or one
  built on this engine for a particular kind of tenant) does the structural
  work: it runs onboarding as a conversation, keeps the books current, drafts
  the authority settings, routes documents, chases them, and answers questions.
  It reaches the core through an API or MCP first; a browser only where no
  API exists.

A person's part shrinks to what only a person should do: saying what they
want, and giving a yes or no where an approval is required. Even then the
agent asks the question, and the code records the answer. Every design choice
in this document, and in what is built on it, is measured against that: if a
person has to learn the machinery to get their work done, the design is not
finished.

## The idea

The first tenant built its brand, its voice rules, its approval habits and its
books by hand, over months, mostly outside the engine. Every tenant after it
gets the same things as a template the engine ships, fills them in during
onboarding, keeps them in its own private repository, and can change any of
them later. `engine doctor` checks the parts a tenant's lanes actually use and
nothing else (2026-09-16, doctor fails only on what was asked for).

The engine exists to get people out of administration. So the rule for
everything below: **whatever complexity the kit needs, the engine carries it
and the agents keep it out of the way.** A missionary has a conversation and
never meets the words "role", "scope" or "permission". The full machinery is
there for the organization that has an IT team and security groups and wants
to see it; most tenants will not be that organization.

## Shapes: two families on one backbone

A tenant's **shape** is the first thing onboarding asks, and it picks the
defaults for everything else: who approves what, how routing runs, which roles
exist, what the books look like, and how much of it the people ever see.

There are two families, commercial and nonprofit, and three sizes. The
backbone (the ledger, the evaluator, routing, the auditor) is the same for all
six; the families differ in vocabulary and books, and the sizes differ in how
much control the defaults put in.

| Size | Commercial | Nonprofit |
|---|---|---|
| **Solo** | Owner-operator: one person runs it; the bookkeeper is an agent | Individual missionary: one person or couple, usually under a sponsoring church or agency that holds the funds; the bookkeeper is an agent |
| **Small** | Small business: owners plus a few staff, a bookkeeper in or out of house | Small church: a pastor, a treasurer or bookkeeper, a board, often sponsoring a few missionaries |
| **Organization** | A company with departments, an IT function and directory groups | A ministry organization: a sending agency with regions, many missionaries, IT and security groups |

The design archetypes (A, B, C in `docs/archetypes.md`) still describe how the
money moves. Shape and archetype are separate questions: a small church and a
ministry organization share a nonprofit archetype and differ in size.

A shape can change. A church that grows re-runs the onboarding interview, and
the engine proposes the new defaults as a change the tenant approves
(section 3), keeping everything the tenant had already customized.

**The sponsored missionary.** Most individual missionaries are not tenants of
their own. They are a unit inside their sponsoring church's or agency's
tenant, because the sponsor must hold discretion and control over the funds.
The missionary sees and submits within their own unit; the sponsor's treasurer
approves, or the sponsor's agent does where the sponsor allows it. An
independent missionary with their own small nonprofit is a solo tenant. There
is usually no bookkeeper: the agent keeps the books. There may be a board, but
not necessarily. What a solo tenant always needs is an accurate account of
the spending, for themselves, for their donors, and for the IRS or whichever
government body regulates them, and producing that account is the agent's
job, not the missionary's.

## Where each part lives

```
tenants/<slug>/
  tenant.toml        shape, books, connections, onboarding, every lane's settings
  authority.toml     people, roles, routes, safeguards, the money rule (new; its own file)
  kit/
    brand.toml       names, colors, fonts, tagline, which template renders what
    brand/           logo files and the Office templates brand.toml names
    voice.toml       registers, banned words and shapes, glossary, worldview preset
```

Authority gets its own file because a change to it is itself a recorded,
approved act (section 3). As a separate file it can carry its own review rule
in the tenant repository and its own doctor and auditor checks, and the
owner can read the whole of it on one screen.

`engine init <slug> --shape <shape> --archetype A|B|C` renders all of it from
`tenants/_templates/`, the way it renders `tenant.toml` today (2026-09-15,
engine init templates). Most people never run it: the onboarding interview
(section 6) runs it for them.

## Owner decisions (2026-10-08)

| # | Question | Decision |
|---|----------|----------|
| 1 | Is "agents never move money" an engine rule or a tenant rule? | **A tenant setting, default human-only**, and the first tenant keeps it. Anything beyond human-only belongs to the payment-rails system, a separate design to be held when the engine reaches that stage; this design only reserves the setting. |
| 2 | Who defines a tenant's voice? | **The tenant, at onboarding.** The engine ships presets; a ministry tenant's default is a conservative Christian worldview preset, and a missionary or ministry can tune it. |
| 3 | What shape does authority take? | **Least-privilege roles with granular permissions**, with defaults per shape that the tenant can change, no movement up or sideways beyond a person's grants, and one document routed through its approvers on a clock. |
| 4 | Are separation-of-duties rules universal? | **No. They are per-tenant defaults set by shape.** A two-person rule that fits a sending agency would stop a solo missionary cold. Only a short floor (section 3) holds for everyone. |
| 5 | How does escalation feel? | **Graduated and kind.** Reminders to the approver come first, then a handoff the approver chooses, and only an organization ever routes upward, about the document and never about the person. |
| 6 | Where does the voice check run? | **In the engine**, as `engine voice-check <tenant> <file>` reading `kit/voice.toml`. The first tenant's host script becomes a thin wrapper around it. |
| 7 | Who uses a terminal? | **Operators, consultants and agents, not owners.** A non-technical person talks to their agent or approves on a screen (a later issue); everything behind that can stay terminal- and API-driven. |
| 8 | What is the overarching rule? | **The true north above:** a deterministic core, agents over it, and the complexity hidden by the agents. It governs every product built on the engine. |
| 9 | Who runs the permissions? | **Agents drive them.** The agent drafts, explains and maintains the authority settings from what the tenant says it wants. A person confirms a change with one plain yes, and the floor in section 3 keeps the agents that read outside documents away from authority altogether. |

## 1. Brand

`kit/brand.toml` holds the legal name and any trading name, the logo files,
the colors, the fonts, the tagline, and a table naming which template renders
which output: letterhead, invoice, statement, expense report, donor
acknowledgment, email signature. Invariant 10 already puts the tenant's legal
name in every Office file's metadata; the kit extends it from metadata to the
whole look of anything the tenant sends. A renderer reads the brand from the
kit or refuses to render; it never falls back to an engine default on an
outbound document, because a document in the wrong livery is worse than a card
saying the kit is incomplete.

Templates are Office files built with python-docx and openpyxl and kept in
`kit/brand/`. A tenant with no letterhead gets the shape's plain one with its
own name and colors filled in, and a solo missionary never has to think
about it.

Doctor: `brand` is `skip` until a lane that renders an outbound document is
on, then each template that lane names must exist and open.

## 2. Voice

`kit/voice.toml` holds:

- **Registers**: named sets of rules (for example `informal` for notes to a
  person and `formal` for anything on letterhead), each with its own rule on
  contractions, sentence length and sign-off.
- **Banned words and shapes**: plain words, and regular expressions for
  sentence shapes the tenant reads as machine-written.
- **A glossary**: the tenant's own terms, with the term to use and the ones
  to avoid ("say X, never Y").
- **Spelling**: the tenant's variety of English (`en-US` by default), so the
  check flags a British spelling in a US tenant's letter, and the reverse.
- **A worldview preset**: the frame a drafting model is given before it
  writes. Presets ship with the engine: `plain-business` (the commercial
  default) and `ministry-conservative-christian` (the nonprofit default). A
  tenant can tune its preset or replace it outright.

`engine voice-check <tenant> <file> [--register NAME]` is deterministic: it
reads the file, applies the register's rules, the banned lists and the
glossary, and prints one line per hit with the fix. No model call. Every model
site that drafts prose for the tenant (the brief, the advisory drafter, future
supporter letters and customer emails) runs the check on its draft and turns
a draft that fails into a card rather than sending it. The worldview preset
goes into the drafting prompt, and the check runs on the output.

Onboarding fills the voice in from the onboarding conversation (section 6): a few samples
of the person's own writing, the words they never want to see, and the terms
their field or tradition uses.

## 3. Authority and routing

This section covers the most new design. It replaces two things in the
engine today: the `HUMAN_ONLY` card types declared in code
(`core/agents/ap/jobs.py`), and the per-lane `unattended` lists in
`tenant.toml` (2026-09-11, the policy table is the unit of delegation). Both
keep working until `authority.toml` exists for a tenant; once it does, it is
the only source.

### The model

- **Principals** are people and agents. An agent is a principal with roles
  like anyone else, so "what may the AP agent do alone" is answered in the same
  table as "what may the treasurer approve". Today's `unattended` lists become
  grants to agent principals.
- **Permissions** are an action on a resource within a scope, with an optional
  limit: `approve` an `expense.report` in `any` unit up to `500.00`.
- **Roles** bundle permissions. Each shape ships default roles, and a tenant
  edits them.
- **Scopes** stop sideways movement. A permission held for one project, fund
  or missionary unit says nothing about another. A missionary sees and submits
  within their own unit and cannot see a neighbor's.
- **Nobody grants what they do not hold.** A role cannot raise itself, or
  anyone else, above where it stands. That stops upward movement in any shape
  with more than one person.
- **Default deny, and a forbid always wins.** A request with no grant is
  refused, and an explicit forbid beats any number of grants.

### The floor (every tenant, every shape)

Short on purpose, because everything above it is the tenant's choice:

1. **Every money movement and external send is a card** (invariant 7), and
   three-way payment verification runs in code (invariant 4).
2. **An agent drafts authority changes; a person confirms them.** The agent
   that runs onboarding or administration proposes a change, explains it in
   plain words, and a person says yes once. An agent that reads outside
   documents (mail, invoices, statements) never proposes or applies one: a
   forged invoice that talks to the agent must not be able to talk its way
   into more authority.
3. **Every change to `authority.toml` is recorded** and appears in the next
   morning's audit, in every shape.

### Safeguards, by shape

These are defaults, and each one is a line in `authority.toml` the tenant can
change. The onboarding agent sets them from the shape and a couple of plain
questions ("Does anyone look over your spending?", "Over what amount should a
second person okay a bill?"), and keeps them current as the tenant changes;
nobody edits the file by hand unless they want to.

| Safeguard | Solo | Small | Organization |
|---|---|---|---|
| Approving your own submission | Allowed. If a sponsor or board exists, they get a **monthly review-after** of everything paid, which the agent prepares, instead of approving each item | Allowed below a limit set at onboarding; above it, a second person approves | Never |
| Submitter, approver and payer are different people | Off | Approver and payer differ above the limit | All three differ |
| A second person for big items | Off, or the sponsor above a limit the sponsor sets | Above the onboarding limit | Two of N above a threshold the organization sets |
| Who confirms a change to authority | The person; the sponsor if there is one | A second person or an outside reviewer | The group the organization names (typically IT or security) |
| How the auditor reports self-approvals | Not at all; the monthly review covers it | Above the limit only | Every one |

**Review-after** is the key to the small shapes. Approving before every
purchase is the friction that keeps a missionary at a desk; a monthly sign-off
of what was paid, by someone outside the person, keeps the oversight a sponsor
needs (discretion and control over the funds) at a fraction of the clicks.

### Routing: one document, on a clock

A document (an invoice, an expense report, a purchase order, an authority
change) gets a **route** when it enters the engine: an ordered list of
approval steps computed from `authority.toml` by its kind, amount and scope.
Each step names a role, never a person, so a route survives someone leaving.
In a solo shape the route is usually one step or none.

- **One document, never copies.** Every approver acts on the same ledger
  record, and each decision is an event on it.
- **Parallel or serial.** A step can need one approver of a role, all of them,
  or two of N.
- **Delegation with an end date.** An approver away for a week hands their
  role to a named person until a date, only within what they hold themselves.
  The delegation ends on that date without anyone remembering to revoke it.

**The clock.** A step waits against the document's real deadline where there
is one (the invoice due date, the reimbursement promised by month end), and
against a per-route window where there is not.

**Escalation is graduated, and it is never about the person.** The defaults,
which a tenant can change:

1. **A reminder to the approver**, from the agent, copied to no one: what
   is waiting, by when, and one tap or one reply to decide.
2. **A second reminder that offers a handoff**: "Want someone else to take
   this one?" The approver picks who, or says "I'm on it, by Thursday", which
   pauses the clock until Thursday.
3. **The named backup** gets the document only if the tenant turned that on,
   and it arrives as "covering for", never as "overdue".
4. **Upward routing exists only in the organization shape**, and only as the
   last step. The message is about the document ("this needs a decision by
   Friday to pay on time"), never a report on who has not acted.

The morning brief lists what is waiting and on whom, for the person who owns
the process, and nobody else.

### The money rule

`[money].out` in `authority.toml` is `"human"` by default, for every shape and
for the first tenant: no agent principal can hold `money.release`, and nothing
in the engine initiates a payment. The engine accepts no other value today.

Anything beyond that belongs to the payment-rails system, which is its own
design, to be held with care when the engine reaches it. That design will
decide whether and how a tenant could ever let agents release money, and under
what conditions. This document only reserves the setting, so the kit does not
change shape when it lands.

### In the queue (#435)

Only for a tenant with `authority.toml`; a tenant without one runs exactly as
before, held byte for byte by `tests/unit/test_authority_equivalence.py`.

- **Every card decision names who made it**: `engine queue approve|reject
  --as NAME`. A person decides only at a terminal; a headless caller decides
  only as an agent. The card records `decided_by` and the evaluator's reason,
  which nobody can write with `--param`.
- **Each agent says what its cards are** (`CARD_AUTHORITY` beside `JOBS`:
  action, resource, the param holding the amount and the person). A test
  fails when a card type has no entry; an undescribed type needs `approve:*`
  and counts as unknown money.
- **The new-vendor door** is `approve:vendor`, which no template gives an
  agent, and a person still types the card number back.
- **Past the second-approver line it takes two people** (#436): the first
  yes is a vote kept on the card, and a second, distinct approver resolves
  it. Saying no needs no second approver.
- **Lanes run as agents** (`[agents.X].lanes`). The brief's send and the
  deadlines calendar ask whether that agent holds `send:message` or
  `send:calendar`; when it does, the lane acts and leaves its card already
  decided by the agent. Each lane's `unattended` list no longer counts, and a
  grant never decides a card a person was already asked.
- **`[approval].auto_file_under`** is read by no code; doctor says so under
  `authority.toml`, where a grant like `approve:ap.invoice<=N` says it. Real
  auto-filing is its own issue.

### On the route (#436)

- **Routes live in `[routing]`** of `authority.toml`: the clock settings, and
  `[[routing.route]]` entries giving a resource above an amount its steps
  (a role, and `one`, `two` or `all` of it). The most specific route wins;
  with none, one step. The route is computed when a card is first decided
  and kept on the card (its params), so it never changes under a document
  mid-flight. Each yes is a vote on that card; distinct people only.
- **The clock** is the document's own deadline param (`due_date`) or the
  step's window from when it opened. The `routing` lane's `tick` job records
  one `route.reminder` event per person and level as each falls due:
  the approver alone; then the offer to hand off or say "on it by"; then the
  named backup as "covering for" (only with `backup = true`); then upward to
  `upward_role` (only with `upward = true`, organization shape only).
  Reminders are ledger events read by `engine queue waiting --as NAME` and
  the process owner's brief; delivering them is #453.
- **A person's acts, at a terminal:** `engine queue handoff` (to someone who
  can decide the step), `engine queue on-it --by DATE` (pauses the clock, up
  to 30 days), `engine queue delegate --to NAME --role ROLE --until DATE`
  and `engine queue revoke`. Delegation follows SAP Concur's published delegate
  rules (the one system checked so far; a broader survey and directory
  sync are #455): only a role held directly, to another person, at most 90
  days, lapsing on its own; the delegate acts on the delegator's behalf
  (`on_behalf_of` on the card) and never on their own submission, and never
  passes it on.
- **The brief's "Waiting on others"** appears only when `[routing].owner`
  names a person: the page is theirs.

### How it is built

The evaluator is a small, pure-Python module (`core/authority/`) that reads
`authority.toml` and answers one question: may this principal take this
action on this resource, in this context? It is shaped the way Cedar shapes a
request (principal, action, resource, context; default deny; forbid wins), so
moving to Cedar later is a translation of the policy file, not a redesign.

Why not adopt a library now:

- **Cedar** (`cedarpy` 4.12, Apache-2.0, Rust wheels for Linux and macOS) is
  the strongest candidate. It would put a second policy language in front of
  operators who read TOML today, and the rule set here is small. It is the
  upgrade path once the rules outgrow one screen.
- **Casbin** embeds well, but its model files are a third syntax and its
  analysis story is weaker than Cedar's.
- **OpenFGA and SpiceDB** are services you run beside the app. One container
  on a small VPS is the product host (2026-09-11), so a second service is the
  wrong weight.

The guarantee is a test, not a library. Roles, actions and scopes are small
finite sets, so the suite enumerates every principal, action and scope for
each shape's defaults and asserts that the floor holds everywhere and each
shape's safeguards hold as declared. A new role, action or preset that breaks
one fails CI.

### Default roles, by shape

| Shape | Roles the engine creates (the people see job titles, not these names) |
|---|---|
| Owner-operator | owner, the bookkeeping agent, outside accountant (optional, read and review) |
| Small business | owner, approver, bookkeeper, submitter, viewer, the agent roles |
| Company organization | the small-business roles per department, plus administrator and auditor |
| Individual missionary (own nonprofit) | missionary, the bookkeeping agent, reviewer (a board member, optional) |
| Small church | pastor, treasurer or bookkeeper, board, missionary (scoped to the unit), the agent roles |
| Ministry organization | board, finance, regional director, member care, missionary (scoped to the unit), donor care, administrator, auditor, the agent roles |

## 4. Books

Both families are first-class here, and the shape picks the default.

`[books]` in `tenant.toml` gathers what is scattered today:

- **The accounting system** and its connection (QBO today; others are their
  own issues).
- **The chart-of-accounts mapping**, including the category accounts the
  expenses lane already reads.
- **The cost-object format**: project number for a commercial tenant, fund or
  unit code for a nonprofit. It is a pattern in config, never in code (#340).
- **The fiscal year** (exists).
- **The entity and its return**: S corp, partnership, C corp, sole
  proprietor, 501(c)(3) public charity (Form 990 family), or church (no 990).
  The year-end package and the obligations calendar read this.
- **1099 rules**: which vendor classifications get a 1099-NEC, plus the
  interest blind spot (#254). A sponsored missionary paid as a contractor is a
  1099 through the AP lane.
- **Nonprofit only:**
  - restricted and unrestricted funds;
  - gifts designated to a missionary unit;
  - the donor acknowledgment rules: a written acknowledgment for a single gift
    of $250 or more, and the quid-pro-quo disclosure above $75;
  - a yearly housing-allowance designation for ministers.

  The fund dimension itself is a separate build (below); `[books]` reserves
  its keys so the kit does not change shape when it lands.

## 5. Connections and data

Each of these exists or has its own issue. The kit gathers them and gives
onboarding a checklist, which in a solo shape is short:

- **Mail provider**: Graph or Gmail, behind one adapter (#329).
- **Bank data**: one bank's statement format today, and a person has to send
  the statement each month, which is a manual step the true north rules out.
  The goal is read-only bank access (SimpleFIN first, then Plaid, both in the
  phase 11 plan), so the agent pulls transactions and statements itself and
  nobody has to remember. Statement files stay the fallback.
- **Model providers allowed to see documents** (#358).
- **Retention**: how long filed documents and ledger events are kept.
- **Backup**: where the ledger goes nightly and the key that encrypts it
  (#357).
- **The notification channel**: where reminders, the morning brief and
  waiting documents go.
- **Directory groups** (organization shape only, later): roles read from the
  organization's own groups in its identity provider, so its IT team manages
  access where it already does.

## 6. Onboarding

1. **The onboarding conversation.** An agent runs it, not a form. The first
   question is who this is for, in plain words:
   "just me", "my business", "our church", "our organization", and so on, which
   maps to a shape. The rest follow from the shape and stay short for the
   small ones: who looks over the spending, the amount above which a second
   person should okay something, who covers when someone is away, a few
   samples of the person's writing, the logo, the bank, the books. Defaults
   stand for anything left blank. A consultant can sit in, and the agent
   runs `engine init` and doctor itself.
2. **Doctor, run by the agent** until it is clean for the lanes the tenant
   turned on; anything only a person can supply (a bank login, a signature) the
   agent asks for in plain words.
3. **Shadow stage.** `[onboarding].stage = "shadow"` runs every lane as live
   would, but forces every agent grant to approval-only and every write to
   the accounting system to a dry run. The audit runs nightly as normal.
4. **Live.** After at least a week of clean audits, the person the shape names
   (the owner, the sponsor, the organization's administrator) sets
   `stage = "live"`. That is an authority change, so it is recorded and the
   auditor marks the day the tenant went live.

What else a tenant will want to set, and the kit should hold once a second
tenant asks: the schedule's times, document numbering (invoice, PO, expense
report), currency and locale, a holiday calendar for the routing clock, and
the language of the approval screen.

## What the kit does not cover

These are capabilities a nonprofit tenant needs and the engine lacks. Each is
its own issue, and none blocks the kit:

- **Restricted-fund accounting and donor receipts** (the fund dimension).
- **Read-only bank access** (SimpleFIN, then Plaid) so statements stop being
  a manual send, plus statement formats beyond the one bank built so far.
- **Multi-currency**, for support that is raised in dollars and spent abroad.
- **Accounting systems other than QBO** (phase 12 names Xero first).
- **The approval screen**: for people who want a page rather than a
  conversation with their agent, a page that shows what is routed to them and
  takes the decision, built on the routes in section 3. The status
  page stays a reader (2026-09-16), and this is a separate surface with its
  own sign-in. Planned for phase 9.
- **Payment rails**: their own system and their own design (section 3, the
  money rule).

## Build order

Each step is an issue; steps marked **owner** change what a scheduled run
does and wait for a coding day with the owner.

1. **Kit skeleton.** Templates under `tenants/_templates/`, `engine init`
   renders them with `--shape`, doctor checks each part as `skip` or `ok`, and
   the demo tenant gets a synthetic kit. No behavior change.
2. **Voice check.** `engine voice-check`, `voice.toml`, the two presets, and
   the first tenant's host script as a wrapper.
3. **Authority model.** `authority.toml` schema, the evaluator, the floor, the
   safeguards and default roles per shape, review-after, and the enumeration
   tests. Read by nothing yet.
4. **Books and connections regrouped.** `[books]` and #340, with the existing
   keys as aliases so no tenant file breaks.
5. **Brand in the renderers.** Every outbound renderer reads `kit/brand.toml`
   or refuses to render.
6. **The onboarding conversation.** Shape first, plain questions, defaults
   for blanks; the agent writes the kit and runs `engine init` and doctor.
   The questions and their answers live in the core as data, so any agent the
   tenant chooses can ask them (over MCP), and the approval screen reuses them.
7. **Authority wired into the queue** (**owner**). Card decisions,
   `HUMAN_ONLY`, `unattended` and `auto_file_under` all go through the
   evaluator.
8. **Routing** (**owner**, because it changes the brief). Routes, the clock,
   graduated escalation, delegation with an end date, and the brief's
   waiting-documents line.
9. **Shadow stage** (**owner**).
10. The nonprofit issues above, starting with the approval screen and the
    fund dimension.

## Not decided here

- **The payment-rails design**, including whether agents could ever release
  money, for any tenant.
- **Whether an agent may ever confirm an authority change on its own.**
  Today a person gives the one yes. Revisit when an administrative agent can
  be shown, by the enumeration tests and a season of audits, never to have
  read untrusted input.
- **The upgrade to Cedar.** Decide when the authority file outgrows one
  screen or a tenant needs conditions the TOML cannot say.
- **The ministry worldview preset's text.** It is written with a ministry
  partner during the first nonprofit onboarding, not by the engine.
- **The approval screen's sign-in:** passkeys, an identity provider, or
  magic links. That gets its own design before the screen is built.
- **Directory-group sync** for the organization shape: which identity
  providers, and when.
- **Whether authority changes can be made from the approval screen**, or
  stay a pull request to the tenant repository. The second is the safer start.

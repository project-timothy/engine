# The mail seam: five methods, two adapters, no default, and a contracts package that exists
Date: 2026-09-22
Type: Two-way door

Gate 3 of the extraction plan (2026-09-21). Until today the engine assumed
Microsoft: three jobs (mail fetch, AR remittance, the close's statements
send) and `engine mail consent` each constructed `GraphMailClient` by name.
The first tenant Timothy serves is a Google shop, so the assumption had to
become a seam before the push, and the seam had to be real rather than
nominal.

## The shape is five methods, and the listing carries no bodies

`core/contracts/mail.py` names `MailClient`: `list_messages`, `get_body`,
`list_attachments`, `download`, `send_mail`. Exactly the surface the Graph
adapter already had, made a `Protocol`, with two frozen dataclasses for what
the listing returns (`MailSummary`, `MailAttachmentRef`) so two providers
list the same mailbox the same way: `received` is ISO-8601 UTC with a `Z`
whatever the provider's own representation, oldest first across every page.
The Graph adapter used to hand back raw Graph dictionaries and the two
reading jobs parsed `from.emailAddress.address` themselves; that was the
assumption, one layer down.

Bodies stay out of the listing. `get_body` is one call per message, after
the caller's own filters have matched, on both adapters. Gmail cannot say
whether a message carries an attachment without fetching the full message,
so `MailSummary` carries no `has_attachments` field at all: the
`with_attachments` filter is the contract, and nothing downstream read the
flag.

## No default. An adopter picks

`[mail].provider` is `graph` or `gmail`, and empty is refused BY NAME at the
one place an adapter is built (`core/adapters/mail.py`), never defaulted.
The shipped demo tenant and template carry `provider = ""`. The first tenant's own
`tenant.toml`, in the tenant repository, says `graph`, and that data change
merges before this one, because the 08:00 mail fetch would otherwise refuse
on the first morning. The refusal is deliberate over a silent Graph default:
a Google shop that forgot the line would have failed against Microsoft's
endpoints with a message about MSAL, which is the wrong error.

The refusal happens at factory time, not at config load. A load-time error
would take every job down (`load_tenant` runs for all of them); a factory
error takes down only the jobs that need the mailbox, with the setting
named.

## Gmail with no new dependency

`core/adapters/gmail.py` is the same five methods over the Gmail REST API in
the standard library (`urllib`, `email`), roughly the size the plan
estimated. Google's device flow does not serve the Gmail scopes, so consent
is the installed-app loopback flow: a URL to open, a code caught on
127.0.0.1, exchanged for a refresh token that lives in the OS keychain under
the tenant's names, the same place the Graph adapter keeps its MSAL cache.
An installed-app client needs its client secret for the exchange;
`[mail].client_secret_env` names the environment variable, never the value
(the secrets rule). Adding `google-auth` and its transitive tree would have
been a new dependency (a one-way door by the repo's rule) to save perhaps
eighty lines.

## The eager token stays a factory concern

The close's send acquires its token BEFORE stamping `send_started` on the
card (issue #135; honesty audit F11), so that a missing consent is recorded
as "nothing attempted" rather than "outcome unknown". That order is now
`client_for(mail, eager=True)`, provider-independent, and both adapters'
auth errors are `MailAuthError`, which is what the job catches.

## The contracts package exists now, with the mail shape in it

`core/contracts/` carries its own Apache-2.0 LICENSE and the mail seam.
The rest of the plan's five shapes (the model gateway's `Adapter`, the
runner's `Runner`, the job `JobHandler`) still live beside their behaviour;
they move in a following change, because each drags its data types with it
and this change is already the one that touches the 08:00 path. The rule
the package enforces from today: nothing in it imports behaviour from the
rest of `core` (`tests/unit/test_mail_seam.py` pins it, alongside the rule
that no job names a provider).

## What proves it

One fixture set, two adapters, driven through fake transports over the same
mailbox: identical listings, attachments, bodies, bytes, and sends. The full
suite is the same suite for both providers because no test outside the
adapters' own knows which one it is talking to.

# A person's setup is a yes per screen
Date: 2026-10-22
Type: Two-way door

Chunk 10, board item t-setup-wizard. The owner's design of 2026-10-15 ("the
Star Trek computer applies to setup too"), his go of 2026-10-22, and his yes
the same day to a standing receipt link.

## Decision

- **The welcome page keeps going after Face ID** and asks one thing per
  screen. The doorkeeper shows the screens and asks for Face ID; the engine
  holds the steps, their words and the answers (`core/onboarding/person.py`,
  `person.toml`) and answers the page as JSON text on the same origin
  (`core/tools/setup_page.py`).
- **Every yes is the person's own Face ID.** A yes reaches the engine only as
  a one-use grant the doorkeeper mints after a fresh Face ID, naming the
  person and the step; the engine spends it with the doorkeeper over
  localhost, the same bridge as the month page. Looking at the screens and
  "not now" ride a setup token the doorkeeper minted after Face ID, so a
  setup buddy can tap "not now" but never yes.
- **The ministry decides what's offered.** The baseline is offered wherever
  it fits: the daily email to someone the ministry gave an address, the
  receipt page on the home screen to someone who files receipts. Anything
  more is named in tenant.toml, `[setup] offer = ["mileage"]`, and is offered
  only to someone who files receipts, since drives become their expense lines.
- **A step is done only once Tim sees it work:** the hello's link opened, the
  receipt page opened from the home screen, the phone's tracker heard by the
  box. Until then the screen waits and moves on by itself.
- **The answers are settings,** `setup.json` in the tenant folder (0600), not
  the ledger: no migration. A step says yes, later, or again; "later" sets it
  aside, and the last screen offers it back.
- **The receipt page on the home screen is a standing link** (`/receipt/k/<key>`),
  one per person, kept only as its hash in `receipt_links.toml` and retired
  with `engine receipt-link <tenant> <person> --revoke` or by deleting its
  line. Its one power: each open trades the key for an ordinary one-use photo
  ticket into the person's own folder, at most 60 an hour. It carries its own
  home-screen entry (`app.webmanifest`, start_url the same link) and Tim's mark.
- **Mileage is one tap.** The yes makes the person's tracker token (their
  earlier one stops counting) and the screen shows Traccar Client's own setup
  link, `org.traccar.client://?url=<box>/track&id=<token>&...`, which the app
  confirms, then `org.traccar.client://action/start` to turn it on. On a
  computer the same settings go in a QR code the app scans from its own
  settings. The settings: Highest accuracy, offline buffering, stop
  detection; distance 50 m, interval 60 s, angle 30 degrees as starting
  values until the pocketed-phone drive test sets them.

## Why

A missionary setting up Tim should never type an address or a code, and a
helper beside them should never be able to say yes for them. The month page
already proved the bridge (Face ID at the doorkeeper, a one-use grant, the
engine spends it), so setup reuses it rather than adding a second way in.

The standing link is the one thing here that lives longer than fifteen
minutes. It sits in the same risk class as the tracker token the box already
keeps: it can add photos to one person's receipts and do nothing else, a
receipt commits no money, and the ministry can retire it in one line.

## Checked against the source

Traccar Client 10.2.1 (github.com/traccar/traccar-client, read 2026-10-22) is
one Flutter codebase for iPhone and Android. Both platforms register the
`org.traccar.client` scheme; `ConfigurationService.applyUri` takes `url` from
the query for that scheme (or the origin and path for an https QR) and reads
`id`, `accuracy`, `distance`, `interval`, `angle`, `heartbeat`, `buffer`,
`wakelock`, `stop_detection`; the app asks the person to confirm. The host
`action` with path `start` turns tracking on without a question. The SDK's
uploader posts a form (id, lat, lon, timestamp in seconds) to the server URL,
which is what `/track` reads.

Asked upstream (Traccar forum thread "Config link stability, and iOS settings
for mileage logging", October 2026), Anton Tananaev, Traccar's maintainer,
answered that the `org.traccar.client://` link and its parameter names have
no plan to change, so the phone tap stays the main way in and no QR-only
fallback is needed. On iOS he gave no numbers ("no guarantees in iOS world")
and pointed to traccar.org/client-configuration, which recommends only what
these settings already do: Highest accuracy (GPS stays on; interval and angle
apply), offline buffering, and unrestricted background use (the setup screen
says to choose Always). The drive test still sets distance, interval and
angle, and decides whether stop detection reads stop-and-go traffic as
parked; if it does, `stop_detection` goes to false.

## Reuses and adds

Reuses: the month page's grant bridge and `WitnessDesk`; `ReceiptDesk.issue`
and the receipt page; `add_tracker` and `TrackDoor`; the onboarding pattern of
steps in a file with the rules in code. Shares, now that each has a second
caller: the hashed key file (`core/engine/keyfile.py`, under trackers and
receipt links), `receipts.filer_name` and `receipts.person_entry`.

Adds: `segno` 1.6.6 (pure Python, BSD, no dependencies) for the QR code, the
one new dependency, imported only when a mileage screen is drawn.

## Not yet

The AI connector, calendar and AGWM claims as setup steps; the drive test that
sets the tracker's numbers; Android proven on a real phone.

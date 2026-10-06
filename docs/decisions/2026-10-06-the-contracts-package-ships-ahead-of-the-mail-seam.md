# The contracts package ships ahead of the mail seam
Date: 2026-10-06
Type: Refines 2026-09-22

The 2026-09-22 decision created `core/contracts/` (Apache-2.0, its own
LICENSE) inside the mail-seam change, and that change is held because it
touches the 08:00 path. Meanwhile CONTRIBUTING.md promised the package and
the repository did not carry it (project-timothy/engine#5).

The package, its LICENSE, and the mail shape (`core/contracts/mail.py`) now
land on their own, byte-identical to the mail-seam branch so that change
rebases onto them cleanly. Nothing in the engine imports the package yet;
the adapters and the factory still arrive with the seam. The rule the
package enforces holds from today: nothing in it imports from the rest of
`core` (`tests/unit/test_contracts_package.py`).

Why now: the Apache hitch is what lets someone who installs the engine for
a ministry or a small business keep a client's private adapter private,
while the engine itself stays AGPL. Publishing that promise without the
file behind it was the bug. Releasing these shapes under Apache-2.0 is
irreversible for the published text; the owner approved it on 2026-10-06.

# Contributing to the engine

Thank you for considering a contribution.

## License of your contribution

The engine is licensed under the GNU Affero General Public License,
version 3 (AGPL-3.0), with an additional permission under AGPL-3.0
section 7 (see [LICENSE](LICENSE)). The one exception is
`core/contracts/`, the plug shapes an adapter is built against: it is
Apache-2.0 under [its own license](core/contracts/LICENSE), so an adapter
built against those shapes can be kept private. The mail shape is there
today; the job contracts in `core/engine/contracts.py` are still AGPL and
move into the package later. The section 7 permission lets the project
accept your code without a separate Contributor License Agreement.

Add this three-line header to the top of each new or substantially modified
source file:

```
Copyright (C) <year> <your name or handle>
SPDX-License-Identifier: AGPL-3.0-or-later
Contributed under the Apache-2.0 inbound license granted in LICENSE.
```

That header is the entire process. No CLA, no signature, no separate form.
By adding it and opening a pull request, you license your contribution to
the project under Apache-2.0 in addition to the AGPL-3.0 terms under which
it is received. The engine as a whole, and your contribution once merged,
is still distributed downstream under AGPL-3.0.

## Ground rules

- The engine invariants in [CLAUDE.md](CLAUDE.md) are the contract: code
  computes every money value, every ledger write carries an idempotency key,
  every external send or money movement passes through the approval queue.
- A failing test or eval comes before the code that makes it pass, and an
  incident's eval lands in the same PR as its fix. The rule it taught goes
  into [docs/lessons.md](docs/lessons.md).
- Nothing under `core/` or `auditor/` names a business, and no business's
  folder ships under `tenants/`: only `tenants/demo/` and
  `tenants/_templates/`. CI runs the bleed-through lint and a boundary test
  on every pull request.
- The gates before a merge: `scripts/check.sh` runs every gate CI's `check`
  job runs and lists each failure. A test keeps the two in step.
- The maintainers are listed in [.github/CODEOWNERS](.github/CODEOWNERS).
  A maintainer reviews and merges every contributed PR. Automated agents
  help with review but never merge a PR they did not open. Never put a
  credential, a real customer or vendor name, or a real amount in a PR, an
  issue, or a comment: all of it is public the moment it is posted.
- Found a security problem? Report it privately, never in an issue:
  see [SECURITY.md](SECURITY.md).
- Open an issue before a large change so the shape can be agreed first. A
  ledger schema change, a new external dependency, or anything touching
  money movement is a one-way door: flag it in the PR rather than deciding.

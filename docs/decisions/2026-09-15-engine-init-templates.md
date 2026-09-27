# `engine init` renders a tenant from one template, and the demo tenant IS that render
Date: 2026-09-15
Type: Two-way door

Row 7.19 (issue #228) says `engine init <slug> [--archetype A|B|C]` creates
`tenants/<slug>/` with `tenant.toml` from an archetype template,
`vendors.toml`, `secrets.ref`, a ledger, a demo drop tree, and a first
audit, with no real business's path anywhere. Ten things the row left open, decided
in the build:

1. **One template, three knob sets, and the knobs are data.**
   `tenants/_templates/tenant.toml.tmpl` is the whole tenant config with
   `${placeholder}` markers; `archetypes.toml` carries the per-archetype
   values. Substitution is `string.Template.substitute`, which raises on a
   placeholder the knobs do not supply, so a template edit that forgets a
   key fails loudly instead of rendering a blank. A separate file per
   archetype was the alternative and was rejected: three near-identical
   200-line configs drift the moment one of them gains a section.

2. **An archetype changes exactly four keys.** `identity.archetype`,
   `ap.workbook_columns` (the unit column's label, "Project" or "Job"),
   `expenses.project_account_template`, and
   `expenses.category_accounts`. A test pins that set, so a fifth differing
   key is a failure until someone widens the set on purpose. Everything else
   in a rendered tenant is identical across A, B, and C and is the owner's
   to edit. The archetypes' later lanes (retainage and lien waivers for B,
   deferred revenue for C) get a comment block at the end of the rendered
   file and no config, because no code reads them yet and a config key that
   nothing honours is a lie.

3. **The data tree lives at `<slug>-data/` beside the tenants root, and
   every path in `tenant.toml` is relative.** The engine resolves them from
   its working directory, so the same tenant file works on the owner's Mac,
   in a container, and in a temp directory under test. `--data-root` moves
   the tree; the rendered path is still written relative to the current
   directory. No absolute path and no `~` ever reaches the file (a test
   greps for both).

4. **The first audit runs as a subprocess.** `init` shells
   `python -m auditor.cli run <slug> --local-only` rather than importing the
   auditor, because invariant 5 and the independence lint say `core/` imports
   nothing from `auditor/`. The owner sees the same command they will run
   every night, and its exit code decides the CLI's.

5. **Secrets are names, prefixed by the slug.** `secrets.ref` lists
   `<logical name> -> <ENV VAR>` and nothing else; the variables are the slug
   uppercased with hyphens as underscores plus the logical name
   (`ACME_QBO_CLIENT_ID`). A slug starting with a digit gets a `T_` prefix so
   the result is always a legal variable name. A test proves no line carries
   an `=`.

6. **Every model seat renders on the `fixture` adapter.** The first hour is
   offline: a new tenant runs its whole daily loop and its first audit with
   no key anywhere, and naming a real tier is a later, deliberate edit. The
   `w9_detect` seat renders as `deterministic`, which no model may serve.

7. **Refusals write nothing and exit 2.** A slug outside `[a-z0-9-]`, an
   unknown archetype, an existing tenant directory, or an existing ledger for
   the slug all refuse before the first `mkdir`. The existing-ledger check
   matters more than the directory one: a tenant directory can be deleted and
   rebuilt, but re-initializing over a live ledger would put a second
   genesis commit on top of real history.

8. **The bleed-through lint widens to cover the templates.** It now scans
   `tenants/_templates/` beside `core/` and `auditor/`, and it reads `.tmpl`
   and `.ref` files as text. A template is the one place in `tenants/` that
   must carry no real business, vendor, customer, host, or path, because
   every tenant anyone ever generates inherits it. This widens the lint's
   reach and exempts nothing.

9. **The demo tenant IS the rendered archetype A, and that made it a
   complete config.** A test renders archetype A with the demo's three
   identity facts and compares byte for byte, so a template edit that is not
   copied over `tenants/demo/` fails CI: one source of truth for what a
   tenant looks like. The cost, paid in this PR: the old demo was a minimal
   config that named almost no paths, and six eval files leaned on keys it
   did not set (an unset `[w9].folder`, an unset `[expenses].filing_dir`, a
   four-column workbook, a bank account spelled out as a literal). Each was
   retargeted to read the value from the tenant, or to build the unset
   condition it is about, so none of them can drift on the next re-render.

10. **`[bank_csv].statement_dir` is part of the template.** Row 7.3 added
    the key and the demo carried it; a rendered tenant needs somewhere for
    bank exports to land, so the template names
    `${data_root}/statements` and `init` creates the folder with the rest of
    the skeleton.

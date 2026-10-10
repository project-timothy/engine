# Tenant templates

`engine init <slug> [--archetype A|B|C]` renders a new tenant from this
folder (phase 7 row 7.19, `core/engine/init.py`). It is not a tenant itself:
the files carry `${placeholder}` markers and the `.tmpl` suffix so nothing
that iterates `tenants/*/tenant.toml` picks them up.

| File | Renders to | Placeholders |
|---|---|---|
| `tenant.toml.tmpl` | `tenants/<slug>/tenant.toml` | slug, legal_name, timezone, fiscal_year_start, data_root, env_prefix, and the archetype knobs |
| `vendors.toml.tmpl` | `tenants/<slug>/vendors.toml` | slug, legal_name |
| `secrets.ref.tmpl` | `tenants/<slug>/secrets.ref` | slug, env_prefix |
| `obligations.toml.tmpl` | `tenants/<slug>/obligations.toml` | slug, legal_name |
| `authority/<shape>.toml.tmpl` | `tenants/<slug>/authority.toml` | legal_name, shape (one template per shape: its safeguards, roles and agents) |
| `kit/brand.toml.tmpl` | `tenants/<slug>/kit/brand.toml` | legal_name |
| `kit/voice.toml.tmpl` | `tenants/<slug>/kit/voice.toml` | legal_name, voice_preset (from the shape's family) |
| `archetypes.toml` | the knob values per archetype | (data, not a template) |

Substitution is `string.Template` (`${name}`), so a literal dollar sign in a
template is written `$$`. The rendered files pass the bleed-through lint
(`core/evals/bleedthrough_lint.py` scans this folder in CI), so nothing here
names a real business, vendor, customer, host, or path.

The demo tenant in this repo is the rendered archetype A
(`tests/unit/test_engine_init.py` pins it byte for byte). To regenerate it
after editing a template, from the repo root:

```
uv run engine init demo --archetype A --legal-name "Demo Tenant Inc." \
    --timezone America/Chicago --fiscal-year-start 7 --root /tmp/regen --no-audit \
    --data-root demo-data
cp -R /tmp/regen/demo/. tenants/demo/
```

Adding an archetype knob: add the key to every archetype in
`archetypes.toml`, reference it in `tenant.toml.tmpl`, and extend
`ARCHETYPE_DIFF_KEYS` in the test if the rendered config gains a new
differing key. Adding a fourth archetype is a new table in
`archetypes.toml` and one entry in `ARCHETYPES`.

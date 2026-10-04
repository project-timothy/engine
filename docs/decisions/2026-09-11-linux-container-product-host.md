# Linux container is the product host; macOS wiring stays tenant-local
Date: 2026-09-11
Type: Two-way door

PRD v2 section 5.4: one container, one data volume, one secrets file, one
wizard. `docker compose up -d` on any Linux host brings up the engine with
`supercronic` inside driving the engine's own CLI, a first-run web wizard
that writes `tenant.toml`, and `tailscale serve` for HTTPS remote access.
The engine's `job_records` table is the durable job ledger and the
approval-card queue is the human step; adding Temporal or Windmill to
schedule five daily jobs on a single-tenant box doubles the surface a
consultant must understand. Hatchet Lite is the named upgrade path if
parallel workers or multi-day waits arrive.

Where it runs, in order of proof: a $5 to $20 VPS via a `cloud-init`
template (consultants), a mini PC or the owner's Mac under Docker Desktop,
then the Umbrel community store and CasaOS, then PikaPods. A Home
Assistant style appliance image comes last, if ever, because its updater
is a product of its own.

The first tenant's Mac stays the dev box. Nothing macOS-specific (launchd, TCC,
applets, the compiled launcher, the Notes and Photos lanes) enters the
product core; it stays in that tenant's own `host/` folder as one tenant's wiring.
Host plists become templates rendered at install with the paths filled in
(section 5.5).

Secrets: SOPS with age, `tenant.secrets.enc.yaml` committed next to
`tenant.toml`, the age key only on the box and in the owner's password
manager; never in `tenant.toml`, the ledger repo, or event logs (the W-9
TIN discipline extended to tokens). Backups: the ledger's nightly `git
push` as tier one, `restic` to Backblaze B2 for the whole volume as tier
two, a monthly restore test as an auditor lens.

Why two-way: the host is a packaging choice around a CLI the engine
already has; a different container runtime or a second host image changes
the templates, not the engine.

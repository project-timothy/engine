# The container's crontab is rendered from tenant.toml, and it runs scripts
Date: 2026-09-16
Type: Two-way door

Phase 7 row 7.21 needs a schedule for a host that has no launchd. The times
live in `[host.schedule]` in `tenant.toml` and `engine schedule <slug>` renders
`host/crontab.tmpl` from them. The rendered file says GENERATED at the top and
the entrypoint rewrites it at every boot, so a hand-edited crontab cannot
survive a restart and a host cannot quietly drift from its tenant file.

**One knob per entry, and the expression is the on switch.** Each entry is one
five-field cron string; empty renders no line. A time plus a separate `enabled`
boolean can disagree with itself, and then the file and the fact disagree too.

**Six entries, not five.** The row names five (engine 08:00, auditor 02:00,
ledger backup 23:00, heartbeat every 30 minutes, the build lane at 04:00 if
enabled). Row 7.23 shipped `engine jobs resume` and said supercronic runs it
every 15 minutes, leaving the line to this row, so `retries` is the sixth
entry at `*/15 * * * *`. No production job declares a retry policy yet, which
makes the line a noop until one does.

**The build lane must name its command.** The other five run scripts this repo
ships; the build lane is the host's own program (row 7.6 shipped the first tenant's as a
tenant-local script). A time with no `build_command` is a config error naming
the key, never an empty crontab line.

**Every line runs a script, never an inline `uv` command.** The contract a
scheduled job carries (which tenant, which uv, which extra, the freshness
guard, the dead-man ping, the canonical ledger root) then lives in exactly one
place, and the container and the Mac execute the same files. That is why this
row adds `scripts/host-heartbeat.sh` (the Mac's heartbeat has always been
inline zsh inside its plist, which a container cannot run) and
`scripts/engine-jobs-resume.sh`.

**The schedule's timezone is `[identity].timezone`, rendered as `CRON_TZ`.**
supercronic schedules in the container's own timezone unless the crontab says
otherwise, and "02:00" means 02:00 where the business is. No environment
variables are exported from the crontab: supercronic's documentation calls
those a compatibility feature and recommends setting the environment before it
starts, which is what the entrypoint does.

**The expressions are validated at config load.** Cron has no error channel: a
typo is a job that never fires and never says so. `[host.schedule]` refuses a
field that is not five wide or out of range, with the entry named.

**The first tenant's `tenant.toml` is deliberately NOT given this section.**
Its Mac's schedule is its twelve launchd plists. A second copy of those times in a
file nothing on that host reads is a fact waiting to drift out of date; when
that tenant runs in a container, that container's tenant file carries it.

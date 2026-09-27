# One environment for the whole schedule
Date: 2026-09-17
Type: Refines 2026-09-16

`uv run` syncs before it runs: it makes the environment be exactly the set it
was asked for, and removes everything else. So the set is a property of the
HOST, not of the job, and every scheduled call has to name the same one.

Until today they did not. The 02:00 and 08:00 scripts asked for `--extra
claude`; the installer had synced the `host` dependency group (Playwright) for
the Thursday sweep; uv dropped that group every morning. The first Thursday
after the extra went in, the sweep's browser helper died on
`ModuleNotFoundError: No module named 'playwright'` and the bank feed went
unswept (docs/lessons.md, "One environment for the whole schedule", issue #280).

**The decision (the owner's, on a coding day):** every scheduled entry script
runs `uv run --extra claude --group host`. The union is the environment; no
job narrows it. The alternative on the table was to let the sweep sync its own
set weekly, which keeps the churn and moves the failure to whichever job runs
next.

This refines 2026-09-16 "The scheduled scripts take an extra's NAME, never a
flag string" rather than replacing it. The group knob has the same shape as
the extra knob and for the same reason:

    ENGINE_UV_EXTRA   the NAME of the one optional extra   (default claude)
    ENGINE_UV_GROUP   the NAME of the one dependency group (default host)

Unset means the Mac's default; empty means this host installed none. They are
independent, so `uv_run` has four branches, all of them fixed arguments behind
an `if` (no array, no eval, identical under zsh and bash). The container sets
both empty: no SDK, no browser, and no resolve at 02:00 on a box that may have
no network.

**What this costs.** A host still names one extra and one group, not several.
This repo defines one of each that a scheduled job needs (`claude`, `host`);
`dev` is uv's own default group and is not asked for by name.

**The installer proves the union in ONE command** (`uv run --extra claude
--group host python -c "import claude_agent_sdk, playwright"`). Two separate
probes could each pass while the second one's sync removed what the first one
proved, which is the bug in miniature: the installer was doing exactly that.

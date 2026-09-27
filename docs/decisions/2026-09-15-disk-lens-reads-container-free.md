# The disk lens alarms on APFS container free, and names the purgeable gap
Date: 2026-09-15
Type: Refines 2026-09-10

On macOS the free space `df` and `shutil.disk_usage` report EXCLUDES
purgeable space, and the hourly APFS local Time Machine snapshots hold the
day's churn there. On the night of 2026-09-15 `df` said 12 GB free on the
data volume while `diskutil info -plist` put the container at 42 GB
unallocated of 228 GB, and macOS purged itself back to 39 GB inside the
hour with nothing asked of it. The host lens read the `df` number, so it
raised low-free at CRITICAL over accounting that was already reclaimed.
That is not a rare shape: the thin log shows the same swing on every dev
day (61 to 23, 45 to 15, 44 to 12 GB), and low-free fired on 11 of the
lens's first 53 nights. A floor that alarms on housekeeping is a floor the
owner learns to ignore, which is the one failure mode a checklist auditor
cannot afford.

So on Darwin the floors compare against `APFSContainerFree`, read from
`diskutil info -plist <data path>` with `plistlib` (the volume's own
`FreeSpace` key is 0 on this box, which is why the container keys are the
ones read). The finding's detail names both numbers and says which is
which, so a real shortage is still legible and the gap is explained rather
than hidden: `12 GB unpurged free (df) / 42 GB container free of 228 GB`.
The floors themselves do not move, and `tenant.toml` is untouched: 40 and
20 GB now mean container free on macOS, which is what the lens docstring
and this file say they mean.

The reader is a plain subprocess with a 15 second timeout, no network and
no privilege. Anywhere it cannot answer (Linux, the phase 7 container, a
missing or failing `diskutil`, a non-APFS volume) the lens falls back to
the `shutil` numbers and the exact detail text it printed before this
change. A failed probe is never a failed lens.

Two things this deliberately does NOT change:

1. **The 01:30 disk-thin job still reads `df`** and still thins under the
   same 40 GB floor (decision 2026-09-10). That is the point of it: it
   keeps the `df` number honest for everything else on the machine that
   reads it, and it keeps real pressure far away from the placeholder
   eviction the lens exists to prevent. The thin is housekeeping; the lens
   is the alarm, and only the alarm was wrong.
2. **The floors and their config keys.** `[auditor.host].disk_warn_free_gb`
   and `disk_critical_free_gb` keep their values and their names; only the
   quantity they measure on macOS changed, which is a lens decision, not a
   tenant one.

Refines 2026-09-10 (local Time Machine snapshots thin themselves at the
floor): that decision ended with "if the finding still recurs, the floor or
the headroom is wrong, not the job." It recurred, and the answer was a
third thing: the metric was wrong.

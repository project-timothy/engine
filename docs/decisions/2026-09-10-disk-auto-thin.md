# Local Time Machine snapshots thin themselves at the floor
Date: 2026-09-10
Type: Two-way door

The host lens raised the low-free disk finding on 11 of its first 53
nights and the fix was the same hand command every time: thin the local
snapshots (the real backups are on the SanDisk; local APFS snapshots are
scratch macOS is slow to release). This was the first automation the
recurrence lens named, and the owner's go the same day. A daily 01:30 job
runs before the 02:00 audit, only when free space is under the auditor's
warn floor, asking tmutil for floor plus 10 GB headroom minus free. It
never touches external backups or files, logs before and after, and has a
dry run. If the finding still recurs, the floor or the headroom is wrong,
not the job: that is what the lens will say next.

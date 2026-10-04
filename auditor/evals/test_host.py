"""Host lens evals: the machine's own storage and backup health.

Written from the 2026-08-18 storage review of the always-on Mac mini that
runs the back office. Every failure pinned here is one that either happened
or was one bad night away: the internal SSD at 20 GB free evicting cloud
placeholders out from under the 08:00 run (the 2026-08-10 shim incident and
the 2026-07-09 intake incident are the same disease), a machine with NO
Time Machine destination at all, and a backup disk that quietly stops
mounting. Silence is never the signal, so every probe is injected and every
branch has an eval.
"""

from __future__ import annotations

import os
import plistlib
from datetime import datetime, timedelta

from auditor.lenses import host

from .fixtures import NOW, make_context, make_ledger

GB = 1024**3
NOW_DT = datetime.fromisoformat(NOW)


def _conditions(findings):
    return sorted(f.condition for f in findings)


def _ctx(tmp_path, **overrides):
    ledger_root = tmp_path / "ledger"
    make_ledger(ledger_root)
    base = {"host_enabled": True, "host_data_path": str(tmp_path)}
    base.update(overrides)
    return make_context(ledger_root, **base)


def _usage(free_gb: float, total_gb: float = 228.0):
    def probe(path):
        return host.DiskUsage(total=int(total_gb * GB), free=int(free_gb * GB))

    return probe


def _container_plist(free_gb: float, total_gb: float = 228.0) -> bytes:
    """The shape ``diskutil info -plist /System/Volumes/Data`` returns on the
    mini (macOS 26, captured 2026-09-15): the APFS container carries the real
    numbers and the volume's own ``FreeSpace`` is 0, which is why the lens
    reads the container keys and ignores that one."""
    return plistlib.dumps(
        {
            "APFSContainerFree": int(free_gb * GB),
            "APFSContainerReference": "disk3",
            "APFSContainerSize": int(total_gb * GB),
            "CapacityInUse": int((total_gb - free_gb) * GB),
            "DeviceIdentifier": "disk3s5",
            "FreeSpace": 0,
            "Size": int(total_gb * GB),
            "VolumeName": "Data",
            "VolumeSize": 0,
        }
    )


def _container(free_gb: float, total_gb: float = 228.0):
    """The injected container reader, parsing a real-shaped plist every time."""

    def probe(path):
        return host.parse_container_usage(_container_plist(free_gb, total_gb))

    return probe


def _no_container(path):
    """Linux, the phase 7 container, or a diskutil that named no APFS keys."""
    return


def _iso(hours_ago: float) -> datetime:
    return NOW_DT - timedelta(hours=hours_ago)


# ---- scope -----------------------------------------------------------------


def test_unconfigured_tenant_is_out_of_scope(tmp_path):
    ledger_root = tmp_path / "ledger"
    make_ledger(ledger_root)
    ctx = make_context(ledger_root)  # host_enabled defaults to False
    with ctx.ledger:
        assert host.check(ctx) == []


# ---- internal disk free space ------------------------------------------------


def test_plenty_of_free_space_is_quiet(tmp_path):
    ctx = _ctx(tmp_path)
    assert host.check_disk_free(ctx, usage=_usage(free_gb=70), container=_no_container) == []


def test_free_space_under_warn_threshold_warns(tmp_path):
    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(ctx, usage=_usage(free_gb=35), container=_no_container)
    assert _conditions(findings) == ["low-free"]
    assert findings[0].severity == "WARN"
    assert "35" in findings[0].detail


def test_free_space_under_critical_threshold_is_critical(tmp_path):
    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(ctx, usage=_usage(free_gb=19), container=_no_container)
    assert _conditions(findings) == ["low-free"]
    assert findings[0].severity == "CRITICAL"


def test_worsening_keeps_the_same_fingerprint(tmp_path):
    """WARN then CRITICAL is ONE checklist item escalating, not two items."""
    ctx = _ctx(tmp_path)
    warn = host.check_disk_free(ctx, usage=_usage(free_gb=35), container=_no_container)[0]
    crit = host.check_disk_free(ctx, usage=_usage(free_gb=10), container=_no_container)[0]
    assert warn.fingerprint == crit.fingerprint


def test_unreadable_data_path_warns(tmp_path):
    def boom(path):
        raise OSError("statvfs failed")

    ctx = _ctx(tmp_path)
    findings = host.check_disk_free(ctx, usage=boom, container=_no_container)
    assert _conditions(findings) == ["unreadable"]
    assert findings[0].severity == "WARN"


# ---- 2026-09-15: the floors measure the APFS container, not df ----------------
#
# On macOS `df` free EXCLUDES purgeable space, and the hourly local Time
# Machine snapshots park the day's churn there. On 2026-09-15 df said 12 GB
# free while the container had 42 GB unallocated and macOS purged itself back
# to 39 GB within the hour: the lens fired CRITICAL on nothing. Decision:
# docs/decisions/2026-09-15-disk-lens-reads-container-free.md.


def test_purgeable_space_below_the_floor_is_not_an_alarm(tmp_path):
    """The 2026-09-15 finding: df 12 GB, container 42 GB, floors 40/20. The
    machine is fine, so the lens says nothing."""
    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(
        ctx, usage=_usage(free_gb=12), container=_container(free_gb=42), platform="darwin"
    )
    assert findings == []


def test_container_free_under_critical_names_both_numbers(tmp_path):
    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(
        ctx, usage=_usage(free_gb=10), container=_container(free_gb=15), platform="darwin"
    )
    assert _conditions(findings) == ["low-free"]
    assert findings[0].severity == "CRITICAL"
    detail = findings[0].detail
    assert "10 GB unpurged free (df)" in detail
    assert "15 GB container free" in detail
    assert "228 GB" in detail
    assert "purgeable" in detail
    assert "cloud placeholders" in detail


def test_container_free_under_warn_warns(tmp_path):
    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(
        ctx, usage=_usage(free_gb=25), container=_container(free_gb=30), platform="darwin"
    )
    assert _conditions(findings) == ["low-free"]
    assert findings[0].severity == "WARN"
    assert "25 GB unpurged free (df)" in findings[0].detail
    assert "30 GB container free" in findings[0].detail


def test_container_probe_failure_falls_back_to_df(tmp_path):
    """diskutil missing, refusing, or timing out is never a crashed lens: the
    numbers and the words are exactly what they were before this change."""

    def boom(path):
        raise OSError("diskutil not found")

    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(
        ctx, usage=_usage(free_gb=12), container=boom, platform="darwin"
    )
    fallback = host.check_disk_free(ctx, usage=_usage(free_gb=12), container=_no_container)
    assert _conditions(findings) == ["low-free"]
    assert findings[0].severity == "CRITICAL"
    assert findings[0].detail == fallback[0].detail
    assert "12 GB free of 228 GB" in findings[0].detail
    assert "container" not in findings[0].detail


def test_a_plist_without_apfs_keys_reads_as_no_container(tmp_path):
    """A non-APFS volume (or a diskutil that answered something else) has no
    container numbers to compare, so the lens keeps the df answer."""
    assert host.parse_container_usage(plistlib.dumps({"FreeSpace": 0, "Size": 1})) is None
    assert host.parse_container_usage(b"not a plist at all") is None


def test_a_real_shaped_plist_parses_to_the_container_numbers(tmp_path):
    parsed = host.parse_container_usage(_container_plist(free_gb=42, total_gb=228))
    assert parsed == host.DiskUsage(total=int(228 * GB), free=int(42 * GB))


def test_off_darwin_the_diskutil_probe_is_never_invoked(tmp_path):
    """Linux, and the phase 7 container: no diskutil exists, so the lens must
    not reach for it at all, and the df answer stands."""
    calls = []

    def spy(path):
        calls.append(path)
        return host.DiskUsage(total=int(228 * GB), free=int(42 * GB))

    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(ctx, usage=_usage(free_gb=12), container=spy, platform="linux")
    assert calls == []
    assert _conditions(findings) == ["low-free"]
    assert findings[0].severity == "CRITICAL"
    assert "12 GB free of 228 GB" in findings[0].detail


# ---- 2026-09-25: the cushion can be gone, and the detail has to say so -------
#
# Reading the container instead of df explains a shortfall as purgeable
# housekeeping macOS clears by itself. That explanation holds only while there
# IS purgeable space. When container free has converged on df free there is
# nothing left to reclaim, and the night the cushion vanishes is the night the
# reader most needs the alarm rather than the reassurance. The same night the
# two numbers converge they also round to the floor itself, so the finding
# printed "40 GB container free ... warn under 40 GB" and read like an
# off-by-one in the lens.


def test_a_vanished_purgeable_cushion_is_never_called_reclaimable(tmp_path):
    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(
        ctx, usage=_usage(free_gb=39.7), container=_container(free_gb=39.8), platform="darwin"
    )
    assert _conditions(findings) == ["low-free"]
    detail = findings[0].detail
    assert "reclaims itself" not in detail
    assert "no purgeable cushion" in detail
    # the act-now half of the sentence is untouched by any of this
    assert "cloud placeholders" in detail


def test_a_real_cushion_is_named_with_its_size(tmp_path):
    """Naming both numbers leaves the reader to subtract. The gap is the
    quantity the sentence is about, so the sentence carries it."""
    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(
        ctx, usage=_usage(free_gb=12), container=_container(free_gb=30), platform="darwin"
    )
    detail = findings[0].detail
    assert "18 GB of that is purgeable" in detail
    assert "reclaims itself" in detail


def test_a_free_figure_never_rounds_up_to_its_own_floor(tmp_path):
    """The finding fired because free space is UNDER the floor. A figure that
    rounds up to the floor makes the report contradict itself, and a checklist
    item that looks like a lens bug is a checklist item the owner mutes."""
    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(
        ctx, usage=_usage(free_gb=39.7), container=_container(free_gb=39.8), platform="darwin"
    )
    detail = findings[0].detail
    assert "39 GB unpurged free (df)" in detail
    assert "39 GB container free" in detail
    # the only "40 GB" left in the sentence is the floor itself
    assert "40 GB unpurged" not in detail
    assert "40 GB container free" not in detail


def test_the_df_only_detail_rounds_down_too(tmp_path):
    """The fallback branch carries the same floor comparison, so it carries the
    same rule: Linux and any host whose diskutil cannot answer."""
    ctx = _ctx(tmp_path, host_disk_warn_free_gb=40, host_disk_critical_free_gb=20)
    findings = host.check_disk_free(ctx, usage=_usage(free_gb=39.6), container=_no_container)
    assert _conditions(findings) == ["low-free"]
    assert "39 GB free of 228 GB" in findings[0].detail


# ---- Time Machine ----------------------------------------------------------


def _dest(mounted: bool = True, name: str = "TimeMachine"):
    dest = {"Name": name, "Kind": "Local", "ID": "ABC-123"}
    if mounted:
        dest["MountPoint"] = f"/Volumes/{name}"
    return dest


def _tm(ctx, **kwargs):
    """Every eval in this section exercises the macOS path, so the platform
    is pinned here. The lens reads ``sys.platform`` (row 7.20: Time Machine
    is a macOS service and ``tmutil`` is never spawned anywhere else), and
    CI's Linux runner would otherwise take the not-applicable branch and
    prove nothing about the probes."""
    kwargs.setdefault("platform", "darwin")
    return host.check_time_machine(ctx, **kwargs)


def test_no_time_machine_destination_is_critical(tmp_path):
    ctx = _ctx(tmp_path)
    findings = _tm(ctx, destinations=lambda: [], latest_backup=lambda: None)
    assert _conditions(findings) == ["no-destination"]
    assert findings[0].severity == "CRITICAL"


def test_destination_configured_but_unmounted_is_critical(tmp_path):
    ctx = _ctx(tmp_path)
    findings = _tm(ctx, destinations=lambda: [_dest(mounted=False)], latest_backup=lambda: None)
    assert _conditions(findings) == ["destination-unmounted"]
    assert findings[0].severity == "CRITICAL"


def test_mounted_destination_with_no_backup_yet_warns(tmp_path):
    ctx = _ctx(tmp_path)
    findings = _tm(ctx, destinations=lambda: [_dest()], latest_backup=lambda: None)
    assert _conditions(findings) == ["no-backup-yet"]
    assert findings[0].severity == "WARN"


def test_fresh_backup_is_quiet(tmp_path):
    ctx = _ctx(tmp_path, host_timemachine_max_age_hours=36)
    findings = _tm(ctx, destinations=lambda: [_dest()], latest_backup=lambda: _iso(2))
    assert findings == []


def test_stale_backup_warns_then_goes_critical_after_a_week(tmp_path):
    ctx = _ctx(tmp_path, host_timemachine_max_age_hours=36)
    stale = _tm(ctx, destinations=lambda: [_dest()], latest_backup=lambda: _iso(40))
    assert _conditions(stale) == ["stale-backup"]
    assert stale[0].severity == "WARN"
    dead = _tm(ctx, destinations=lambda: [_dest()], latest_backup=lambda: _iso(24 * 8))
    assert _conditions(dead) == ["stale-backup"]
    assert dead[0].severity == "CRITICAL"
    assert stale[0].fingerprint == dead[0].fingerprint


def test_tmutil_probe_failure_is_a_finding_not_a_crash(tmp_path):
    def boom():
        raise RuntimeError("tmutil not available")

    ctx = _ctx(tmp_path)
    findings = _tm(ctx, destinations=boom, latest_backup=lambda: None)
    assert _conditions(findings) == ["probe-failed"]
    assert findings[0].severity == "WARN"


# ---- row 7.20: off Darwin there is no tmutil to ask --------------------------


def test_off_darwin_time_machine_is_not_applicable_and_tmutil_is_never_spawned(tmp_path):
    """A Linux host that turns the host lens on wants the disk, volume and
    eviction pulses. ``tmutil`` does not exist there, and letting the probe
    fail would file a WARN every night for a service the OS does not have.
    The sub-check reports itself not applicable instead, and neither probe
    is called."""

    def never():  # pragma: no cover - the point is that it is not called
        raise AssertionError("a tmutil probe ran off Darwin")

    ctx = _ctx(tmp_path)
    findings = host.check_time_machine(
        ctx, destinations=never, latest_backup=never, platform="linux"
    )
    assert _conditions(findings) == ["not-applicable"]
    assert findings[0].severity == "INFO"
    assert "linux" in findings[0].detail


def test_not_applicable_is_a_new_item_not_a_rewritten_one(tmp_path):
    """The not-applicable line is its own checklist item, so it can never be
    confused with (or silence) a real backup failure carried forward."""
    ctx = _ctx(tmp_path)
    off = host.check_time_machine(
        ctx, destinations=lambda: [], latest_backup=lambda: None, platform="linux"
    )
    on = _tm(ctx, destinations=lambda: [], latest_backup=lambda: None)
    assert off[0].fingerprint != on[0].fingerprint


def test_time_machine_fingerprints_are_unchanged_by_this_row(tmp_path):
    """A fingerprint is lens|subject|condition, and the owner's triage.toml
    mutes and acknowledgments key on it. Row 7.20 rewrote this check, so the
    five conditions it could already report are pinned to their literal
    fingerprints: a rename here silently reopens a muted item at 02:00."""
    ctx = _ctx(tmp_path, host_timemachine_max_age_hours=36)
    fingerprints = {
        "no-destination": "c62321bd9f5561b3",
        "destination-unmounted": "7e7ebff5716cac04",
        "no-backup-yet": "673cf6a3679faaf3",
        "stale-backup": "0b459d7c22e50ed7",
        "probe-failed": "6de7777ea765f26a",
    }

    def boom():
        raise RuntimeError("tmutil not available")

    observed = {
        f.condition: f.fingerprint
        for f in [
            *_tm(ctx, destinations=lambda: [], latest_backup=lambda: None),
            *_tm(ctx, destinations=lambda: [_dest(mounted=False)], latest_backup=lambda: None),
            *_tm(ctx, destinations=lambda: [_dest()], latest_backup=lambda: None),
            *_tm(ctx, destinations=lambda: [_dest()], latest_backup=lambda: _iso(40)),
            *_tm(ctx, destinations=boom, latest_backup=lambda: None),
        ]
    }
    assert observed == fingerprints


def test_destination_plist_parsing():
    """The real probe parses ``tmutil destinationinfo -X`` output; pin the
    shape so an OS change in the plist keys is caught by the eval, not by a
    silent empty list at 02:00."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict><key>Destinations</key><array><dict>'
        "<key>Name</key><string>TimeMachine</string>"
        "<key>Kind</key><string>Local</string>"
        "<key>ID</key><string>ABC</string>"
        "<key>QuotaGB</key><integer>1500</integer>"
        "<key>LastDestination</key><integer>1</integer>"
        "<key>MountPoint</key><string>/Volumes/TimeMachine</string>"
        "</dict></array></dict></plist>"
    )
    dests = host.parse_destinationinfo(xml)
    assert dests == [
        {"Name": "TimeMachine", "Kind": "Local", "ID": "ABC", "MountPoint": "/Volumes/TimeMachine"}
    ]
    assert host.mount_point(dests[0]) == "/Volumes/TimeMachine"
    empty = '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict></dict></plist>'
    assert host.parse_destinationinfo(empty) == []


def test_mount_point_accepts_real_and_spaced_keys():
    """Incident 2026-08-18: the live plist key is ``MountPoint`` (no space); the
    lens looked up ``Mount Point`` and reported a mounted, three-backups-deep
    destination as unmounted CRITICAL on its first live run. Both spellings
    resolve; neither present means unmounted."""
    assert host.mount_point({"Name": "TM", "MountPoint": "/Volumes/TM"}) == "/Volumes/TM"
    assert host.mount_point({"Name": "TM", "Mount Point": "/Volumes/TM"}) == "/Volumes/TM"
    assert host.mount_point({"Name": "TM"}) is None


def test_mounted_destination_with_fresh_backup_is_clean_on_real_key(tmp_path):
    ctx = _ctx(tmp_path)
    dest = {
        "Name": "TimeMachine",
        "Kind": "Local",
        "ID": "ABC",
        "MountPoint": "/Volumes/TimeMachine",
    }
    fresh = ctx.now.astimezone(host._local_tz()).replace(tzinfo=None) - timedelta(hours=1)
    findings = _tm(ctx, destinations=lambda: [dest], latest_backup=lambda: fresh)
    assert findings == []


def test_latest_backup_timestamp_parsing():
    assert host.parse_latest_backup("2026-08-18-121530\n") == datetime(2026, 8, 18, 12, 15, 30)
    # Path form (older tmutil): the basename carries the same stamp.
    assert host.parse_latest_backup(
        "/Volumes/TimeMachine/Backups.backupdb/mini/2026-08-17-020000\n"
    ) == datetime(2026, 8, 17, 2, 0, 0)
    assert host.parse_latest_backup("") is None
    assert host.parse_latest_backup("Failed to mount backup destination") is None


# ---- expected volumes --------------------------------------------------------


def test_expected_volume_present_is_quiet(tmp_path):
    ctx = _ctx(tmp_path, host_expected_volumes=["/Volumes/Archive"])
    assert host.check_expected_volumes(ctx, is_mounted=lambda p: True) == []


def test_expected_volume_missing_warns(tmp_path):
    ctx = _ctx(tmp_path, host_expected_volumes=["/Volumes/Archive"])
    findings = host.check_expected_volumes(ctx, is_mounted=lambda p: False)
    assert _conditions(findings) == ["not-mounted"]
    assert findings[0].severity == "WARN"
    assert "/Volumes/Archive" in findings[0].subject


# ---- cloud eviction inside engine-read trees ---------------------------------


class _Stat:
    def __init__(self, flags: int = 0):
        self.st_flags = flags


def test_fully_local_tree_is_quiet(tmp_path):
    tree = tmp_path / "Financials"
    (tree / "02_Invoices").mkdir(parents=True)
    (tree / "02_Invoices" / "a.pdf").write_bytes(b"x")
    ctx = _ctx(tmp_path, host_eviction_watch_paths=[str(tree)])
    assert host.check_eviction(ctx) == []


def test_missing_watch_path_warns(tmp_path):
    ctx = _ctx(tmp_path, host_eviction_watch_paths=[str(tmp_path / "gone")])
    findings = host.check_eviction(ctx)
    assert _conditions(findings) == ["missing"]
    assert findings[0].severity == "WARN"


def test_icloud_placeholder_is_critical(tmp_path):
    """The iCloud eviction shape: the real file is replaced by a dot-prefixed
    ``.name.icloud`` stub the engine cannot read."""
    tree = tmp_path / "Financials"
    (tree / "06_Invoice_Originals").mkdir(parents=True)
    (tree / "06_Invoice_Originals" / ".invoice-9001.pdf.icloud").write_bytes(b"")
    ctx = _ctx(tmp_path, host_eviction_watch_paths=[str(tree)])
    findings = host.check_eviction(ctx)
    assert _conditions(findings) == ["evicted-files"]
    assert findings[0].severity == "CRITICAL"
    assert "1 iCloud placeholder" in findings[0].detail
    assert "invoice-9001.pdf" in findings[0].detail


def test_dataless_file_provider_item_is_critical(tmp_path):
    """The File Provider eviction shape (the 2026-08-10 shim
    incident): the entry keeps its name but carries SF_DATALESS and any read
    blocks or fails headless."""
    tree = tmp_path / "Secure"
    tree.mkdir()
    victim = tree / "CLAUDE.md"
    victim.write_bytes(b"x")

    def lstat(path):
        return _Stat(host.SF_DATALESS if os.fspath(path) == str(victim) else 0)

    ctx = _ctx(tmp_path, host_eviction_watch_paths=[str(tree)])
    findings = host.check_eviction(ctx, lstat=lstat)
    assert _conditions(findings) == ["evicted-files"]
    assert findings[0].severity == "CRITICAL"
    assert "1 dataless" in findings[0].detail
    assert "CLAUDE.md" in findings[0].detail


def test_eviction_never_opens_files(tmp_path):
    """The lens must not materialize what it inspects: a read of an evicted
    item is exactly the crash the 2026-08-10 incident logged. Pin that the
    walk uses lstat only (the injected lstat sees every entry, nothing else
    touches the file)."""
    tree = tmp_path / "Secure"
    tree.mkdir()
    (tree / "doc.md").write_bytes(b"x")
    seen: list[str] = []

    def lstat(path):
        seen.append(os.fspath(path))
        return _Stat(0)

    ctx = _ctx(tmp_path, host_eviction_watch_paths=[str(tree)])
    assert host.check_eviction(ctx, lstat=lstat) == []
    assert str(tree / "doc.md") in seen


def test_walk_cap_is_reported(tmp_path):
    tree = tmp_path / "big"
    tree.mkdir()
    for i in range(12):
        (tree / f"f{i}").write_bytes(b"x")
    ctx = _ctx(tmp_path, host_eviction_watch_paths=[str(tree)])
    findings = host.check_eviction(ctx, max_entries=10)
    assert _conditions(findings) == ["walk-capped"]
    assert findings[0].severity == "WARN"


# ---- the whole lens ----------------------------------------------------------


def test_check_composes_every_probe(tmp_path, monkeypatch):
    tree = tmp_path / "Financials"
    tree.mkdir()
    (tree / ".x.pdf.icloud").write_bytes(b"")
    ctx = _ctx(
        tmp_path,
        host_expected_volumes=["/Volumes/Archive"],
        host_eviction_watch_paths=[str(tree)],
    )
    monkeypatch.setattr(host, "_disk_usage", _usage(free_gb=10))
    monkeypatch.setattr(host, "_container_usage", _no_container)
    monkeypatch.setattr(host, "_tm_destinations", lambda: [])
    monkeypatch.setattr(host, "_tm_latest_backup", lambda: None)
    monkeypatch.setattr(host, "_is_mounted", lambda p: False)
    with ctx.ledger:
        findings = host.check(ctx, platform="darwin")
    assert _conditions(findings) == [
        "evicted-files",
        "low-free",
        "no-destination",
        "not-mounted",
    ]


def test_check_on_a_linux_host_keeps_three_pulses_and_files_the_fourth_as_n_a(
    tmp_path, monkeypatch
):
    """Row 7.20: the same lens on the phase 7 container. Disk, volumes and
    eviction answer exactly as before (the container probe already fell back
    to df off Darwin, PR #264); the backup pulse says not applicable and no
    macOS binary is spawned."""
    tree = tmp_path / "Financials"
    tree.mkdir()
    (tree / ".x.pdf.icloud").write_bytes(b"")
    ctx = _ctx(
        tmp_path,
        host_expected_volumes=["/srv/archive"],
        host_eviction_watch_paths=[str(tree)],
    )

    def never(*args, **kwargs):  # pragma: no cover - the point is that it is not called
        raise AssertionError("a macOS-only probe ran on a Linux host")

    monkeypatch.setattr(host, "_disk_usage", _usage(free_gb=10))
    monkeypatch.setattr(host, "_container_usage", never)
    monkeypatch.setattr(host, "_tm_destinations", never)
    monkeypatch.setattr(host, "_tm_latest_backup", never)
    monkeypatch.setattr(host, "_is_mounted", lambda p: False)
    with ctx.ledger:
        findings = host.check(ctx, platform="linux")
    assert _conditions(findings) == [
        "evicted-files",
        "low-free",
        "not-applicable",
        "not-mounted",
    ]


# ---- honesty audit 2026-09-03: 04-F5 (walk gaps) and 04-F6 (latestbackup probe)


def test_unreadable_subtree_is_reported_as_incomplete_walk(tmp_path):
    """04-F5: os.walk dropped an unlistable directory without a trace and the
    lens then said the tree was clean. A skipped subtree is a WARN naming
    the count and the first path."""
    tree = tmp_path / "Financials"
    (tree / "locked").mkdir(parents=True)
    ctx = _ctx(tmp_path, host_eviction_watch_paths=[str(tree)])

    def walk(top, onerror=None):
        err = PermissionError(13, "Permission denied", str(tree / "locked"))
        if onerror is not None:
            onerror(err)
        yield top, ["locked"], []

    findings = host.check_eviction(ctx, walk=walk)
    assert _conditions(findings) == ["walk-incomplete"]
    assert findings[0].severity == "WARN"
    assert "1 " in findings[0].detail
    assert str(tree / "locked") in findings[0].detail


def test_unstatable_entry_is_reported_as_incomplete_walk(tmp_path):
    tree = tmp_path / "Secure"
    tree.mkdir()
    (tree / "a.md").write_bytes(b"x")
    (tree / "b.md").write_bytes(b"x")

    def lstat(path):
        if os.fspath(path).endswith("b.md"):
            raise OSError(35, "Resource deadlock avoided", os.fspath(path))
        return _Stat(0)

    ctx = _ctx(tmp_path, host_eviction_watch_paths=[str(tree)])
    findings = host.check_eviction(ctx, lstat=lstat)
    assert _conditions(findings) == ["walk-incomplete"]
    assert "1 " in findings[0].detail
    assert str(tree / "b.md") in findings[0].detail


def test_failing_latest_backup_probe_is_probe_failed_not_no_backup_yet(tmp_path):
    """04-F6: any non-zero `tmutil latestbackup` exit was read as 'mounted but
    no completed backup exists yet', a fact never observed."""

    def boom():
        raise RuntimeError("tmutil latestbackup failed: operation not permitted")

    ctx = _ctx(tmp_path)
    findings = _tm(ctx, destinations=lambda: [_dest()], latest_backup=boom)
    assert _conditions(findings) == ["probe-failed"]
    assert findings[0].severity == "WARN"
    assert "operation not permitted" in findings[0].detail


def test_latest_backup_probe_distinguishes_no_backups_from_a_failure(monkeypatch):
    import subprocess

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=fake_run.stderr)

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    fake_run.stderr = "No backups found for host\n"
    assert host._tm_latest_backup() is None  # the honest empty case
    fake_run.stderr = "tmutil: operation not permitted\n"
    try:
        host._tm_latest_backup()
    except RuntimeError as exc:
        assert "operation not permitted" in str(exc)
    else:
        raise AssertionError("a failing probe must raise, not read as empty")

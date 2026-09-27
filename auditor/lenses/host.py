"""Lens 10 — host: is the machine under the back office healthy?

The engine's outcome lenses catch what the engine did wrong; nothing else
catches the machine itself running out of disk, evicting cloud placeholders
out from under a headless run (docs/lessons.md, "A cloud placeholder is not
here yet"), or sitting with no local backup at all. Four
pulses, each recomputed from the OS every night, never from anything the
engine writes:

- free space on the data volume against WARN/CRITICAL floors (one checklist
  item that escalates, not two). On macOS the floors measure the APFS
  CONTAINER's free space, read from ``diskutil info -plist``, because ``df``
  free excludes purgeable local-snapshot space and reads as an emergency on
  any busy day; off macOS, and whenever diskutil cannot name a
  container, the ``shutil`` numbers stand. The detail calls the gap between
  the two a reclaimable cushion only when there is one, and every free-space
  figure rounds down, so a finding never prints a number its own floor says
  could not have raised it;
- Time Machine: a destination exists, it is mounted, and the newest completed
  backup is younger than the window (``tmutil``, the OS's own answer). macOS
  only: ``tmutil`` is never spawned off Darwin and the pulse reports itself
  not applicable instead of failing (row 7.20);
- every expected volume (the archive tier) is mounted;
- no evicted item inside the trees the engine reads: an iCloud ``.name.icloud``
  placeholder or a File Provider dataless entry (``SF_DATALESS``). The walk
  uses ``lstat`` only and never opens a file — opening an evicted item is the
  exact crash the incidents logged.

Every probe is injectable so evals pin every branch without a real disk,
``tmutil``, or cloud provider. Local only, runs under ``--local-only``.
Config: ``[auditor.host]`` in tenant.toml; an absent section disables the lens.
"""

from __future__ import annotations

import math
import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..findings import Finding
from . import AuditContext

LENS = "host"

# <sys/stat.h>: SF_DATALESS, set on File Provider items whose bytes are not
# on disk (File Provider / iCloud on-demand evictions). lstat exposes it in st_flags.
SF_DATALESS = 0x40000000
ICLOUD_PLACEHOLDER_SUFFIX = ".icloud"
# A stale backup is WARN inside a week and CRITICAL after: a week of no
# backups on an always-on machine means the destination is effectively gone.
STALE_BACKUP_CRITICAL_HOURS = 24 * 7
DEFAULT_WALK_CAP = 250_000
# Under a gigabyte the difference between df free and container free is
# measurement noise, not a cushion: nothing meaningful is waiting to be
# reclaimed, so the detail must stop calling the shortfall housekeeping.
PURGEABLE_CUSHION_FLOOR_GB = 1.0
_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})")


def _floor_gb(value: float) -> int:
    """A free-space figure rounds DOWN, never to nearest. Nearest-rounding
    prints a number at the floor that raised the finding ("40 GB free; warn
    under 40 GB"), the item reads as an off-by-one in the lens, and an item
    that looks like a lens bug is an item the owner learns to mute."""
    return math.floor(value)


@dataclass(frozen=True)
class DiskUsage:
    total: int
    free: int


# ---- probes (the only code that touches the OS; evals inject stand-ins) ------


def _disk_usage(path: str) -> DiskUsage:
    usage = shutil.disk_usage(path)
    return DiskUsage(total=usage.total, free=usage.free)


def parse_container_usage(raw: bytes) -> DiskUsage | None:
    """``diskutil info -plist <path>`` for an APFS volume carries the whole
    container's numbers in ``APFSContainerFree`` and ``APFSContainerSize``
    (the volume's own ``FreeSpace`` is 0 there, so it is ignored). None when
    the plist is unreadable or names no container: a non-APFS volume, or a
    ``diskutil`` that answered something else."""
    try:
        data = plistlib.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    free = data.get("APFSContainerFree")
    total = data.get("APFSContainerSize")
    if not isinstance(free, int) or not isinstance(total, int) or total <= 0:
        return None
    return DiskUsage(total=total, free=free)


def _container_usage(path: str) -> DiskUsage | None:
    """The APFS container behind ``path``, or None when diskutil is missing,
    fails, or names no container. Never a network or privileged call."""
    result = subprocess.run(["diskutil", "info", "-plist", path], capture_output=True, timeout=15)
    if result.returncode != 0:
        return None
    return parse_container_usage(result.stdout)


def parse_destinationinfo(xml: str) -> list[dict]:
    """``tmutil destinationinfo -X`` is a plist: ``{Destinations: [{Name, Kind,
    ID, MountPoint?, QuotaGB?, LastDestination?}]}``; an empty dict when nothing
    is configured. Only the string-valued keys are kept. The live key is
    ``MountPoint`` (no space; verified macOS 26, 2026-08-18) — the spaced
    ``Mount Point`` is the human-readable form and is only accepted for
    tolerance, see :func:`mount_point`."""
    data = plistlib.loads(xml.encode())
    out: list[dict] = []
    for dest in data.get("Destinations", []) or []:
        out.append({k: v for k, v in dest.items() if isinstance(v, str)})
    return out


def mount_point(dest: dict) -> str | None:
    """The destination's mount path, or None when it is configured but not
    mounted. Accepts the real plist key ``MountPoint`` and the spaced form
    ``Mount Point`` (incident 2026-08-18: the lens keyed on the spaced form
    and reported a mounted destination as unmounted on its first live run)."""
    return dest.get("MountPoint") or dest.get("Mount Point") or None


def _tm_destinations() -> list[dict]:
    result = subprocess.run(
        ["tmutil", "destinationinfo", "-X"], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"tmutil destinationinfo failed: {result.stderr.strip()}")
    return parse_destinationinfo(result.stdout)


def parse_latest_backup(text: str) -> datetime | None:
    """``tmutil latestbackup -t`` prints ``YYYY-MM-DD-HHMMSS``; older forms print
    a path whose basename carries the same stamp. Anything else (an error
    line, nothing) means no completed backup could be named."""
    match = _STAMP.search(text.strip().rsplit("/", 1)[-1]) or _STAMP.search(text)
    if not match:
        return None
    y, mo, d, h, mi, s = (int(g) for g in match.groups())
    return datetime(y, mo, d, h, mi, s)


def _tm_latest_backup() -> datetime | None:
    """None means tmutil itself said there is no completed backup; any other
    failure raises so the lens reports ``probe-failed`` instead of asserting
    an empty backup set it never observed (honesty audit 2026-09-03, 04-F6)."""
    result = subprocess.run(
        ["tmutil", "latestbackup", "-t"], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        if "no backups" in f"{result.stdout}\n{result.stderr}".lower():
            return None
        raise RuntimeError(f"tmutil latestbackup failed: {result.stderr.strip()}")
    return parse_latest_backup(result.stdout)


def _is_mounted(path: str) -> bool:
    return os.path.ismount(path)


# ---- checks ------------------------------------------------------------------


def check_disk_free(
    ctx: AuditContext,
    *,
    usage: Callable[[str], DiskUsage] = _disk_usage,
    container: Callable[[str], DiskUsage | None] = _container_usage,
    platform: str | None = None,
) -> list[Finding]:
    """Free space against the WARN/CRITICAL floors, ONE item that escalates.

    On macOS the floors measure the APFS **container**, not ``df``: ``df``
    free excludes purgeable space, and the hourly local Time Machine
    snapshots park the day's churn there, so a dev day reads as a disk
    emergency that macOS clears by itself within the hour (2026-09-15, see
    docs/decisions/2026-09-15-disk-lens-reads-container-free.md). Both
    numbers go in the detail so the gap is legible. Anywhere the container
    cannot be named (Linux, the phase 7 container, a diskutil that failed)
    the ``shutil`` numbers stand exactly as they always did.
    """
    subject = "internal disk"
    try:
        du = usage(ctx.tenant.host_data_path)
    except Exception as exc:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="unreadable",
                severity="WARN",
                detail=f"could not read free space at {ctx.tenant.host_data_path}: {exc}",
            )
        ]
    cu: DiskUsage | None = None
    if (sys.platform if platform is None else platform) == "darwin":
        try:
            cu = container(ctx.tenant.host_data_path)
        except Exception:
            cu = None
    gb = 1024**3
    df_free_gb = du.free / gb
    free_gb = (cu.free if cu else du.free) / gb
    total_gb = (cu.total if cu else du.total) / gb
    warn = ctx.tenant.host_disk_warn_free_gb
    critical = ctx.tenant.host_disk_critical_free_gb
    if free_gb < critical:
        severity = "CRITICAL"
    elif free_gb < warn:
        severity = "WARN"
    else:
        return []
    pct = (free_gb / total_gb * 100) if total_gb else 0.0
    floors = f"floors: warn under {warn} GB, critical under {critical} GB"
    tail = (
        "Under pressure macOS evicts cloud placeholders the 08:00 run reads; reclaim before it does"
    )
    if cu:
        cushion_gb = max(0.0, free_gb - df_free_gb)
        if cushion_gb >= PURGEABLE_CUSHION_FLOOR_GB:
            cushion = (
                f"{_floor_gb(cushion_gb)} GB of that is purgeable local-snapshot space "
                "macOS reclaims itself"
            )
        else:
            cushion = (
                "there is no purgeable cushion left: container free has converged on df "
                "free, so none of this shortfall comes back on its own"
            )
        detail = (
            f"{_floor_gb(df_free_gb)} GB unpurged free (df) / {_floor_gb(free_gb)} GB container "
            f"free of {_floor_gb(total_gb)} GB ({pct:.0f}%); {floors}; {cushion}. {tail}"
        )
    else:
        detail = (
            f"{_floor_gb(free_gb)} GB free of {_floor_gb(total_gb)} GB ({pct:.0f}%); "
            f"{floors}. {tail}"
        )
    return [
        Finding(
            lens=LENS,
            subject=subject,
            condition="low-free",
            severity=severity,
            detail=detail,
        )
    ]


def check_time_machine(
    ctx: AuditContext,
    *,
    destinations: Callable[[], list[dict]] = _tm_destinations,
    latest_backup: Callable[[], datetime | None] = _tm_latest_backup,
    platform: str | None = None,
) -> list[Finding]:
    """The local backup pulse. Time Machine is a macOS service and ``tmutil``
    exists nowhere else, so off Darwin the probes are never spawned and the
    sub-check files itself as not applicable (row 7.20). Letting the probe
    fail instead would park a WARN every night of a container's life for a
    service the OS does not have, and a lens that cries wolf gets muted.
    Saying nothing was the other option and is worse: it would read as "the
    backup is fine", a fact this lens never observed (honesty audit
    2026-09-03, 04-F6). What backs the host up off Darwin is the host's own
    concern, and the INFO line is where the owner is told so."""
    subject = "time machine"
    host_platform = sys.platform if platform is None else platform
    if host_platform != "darwin":
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="not-applicable",
                severity="INFO",
                detail=f"Time Machine is a macOS service and this host is {host_platform}; "
                "the local-backup pulse is not measured here. Whatever backs this host up "
                "(volume snapshots, the hypervisor, the ledger's own git remote) is outside "
                "what this lens can see",
            )
        ]
    try:
        dests = destinations()
    except Exception as exc:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="probe-failed",
                severity="WARN",
                detail=f"could not ask tmutil for the backup destination: {exc}",
            )
        ]
    if not dests:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="no-destination",
                severity="CRITICAL",
                detail="no Time Machine destination is configured; nothing on this machine "
                "is backed up locally (keychain, tokens, ledger working copy, launchd wiring)",
            )
        ]
    mounted = [d for d in dests if mount_point(d)]
    if not mounted:
        names = ", ".join(d.get("Name", "?") for d in dests)
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="destination-unmounted",
                severity="CRITICAL",
                detail=f"destination {names} is configured but not mounted; backups have "
                "stopped (unplugged, not unlocked at login, or the volume failed)",
            )
        ]
    try:
        latest = latest_backup()
    except Exception as exc:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="probe-failed",
                severity="WARN",
                detail=f"could not ask tmutil for the newest completed backup: {exc}",
            )
        ]
    if latest is None:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="no-backup-yet",
                severity="WARN",
                detail=f"destination {mounted[0].get('Name', '?')} is mounted but no completed "
                "backup exists yet",
            )
        ]
    if latest.tzinfo is None:
        # tmutil stamps are local wall-clock; compare in the machine's zone.
        latest = latest.replace(tzinfo=_local_tz())
    age = (ctx.now - latest.astimezone(UTC)).total_seconds() / 3600
    window = ctx.tenant.host_timemachine_max_age_hours
    if age <= window:
        return []
    severity = "CRITICAL" if age > STALE_BACKUP_CRITICAL_HOURS else "WARN"
    return [
        Finding(
            lens=LENS,
            subject=subject,
            condition="stale-backup",
            severity=severity,
            detail=f"newest completed backup is {age:.0f}h old (window {window}h); "
            "Time Machine is mounted but not finishing backups",
        )
    ]


def _local_tz():
    return datetime.now().astimezone().tzinfo


def check_expected_volumes(
    ctx: AuditContext, *, is_mounted: Callable[[str], bool] = _is_mounted
) -> list[Finding]:
    findings: list[Finding] = []
    for path in ctx.tenant.host_expected_volumes:
        if is_mounted(path):
            continue
        findings.append(
            Finding(
                lens=LENS,
                subject=f"volume {path}",
                condition="not-mounted",
                severity="WARN",
                detail="an expected volume is not mounted (unplugged, not unlocked at "
                "login, or failed); the archive tier is unreachable",
            )
        )
    return findings


def _walk(
    top: str, onerror: Callable[[OSError], None] | None = None
) -> Iterable[tuple[str, list[str], list[str]]]:
    return os.walk(top, onerror=onerror, followlinks=False)


def check_eviction(
    ctx: AuditContext,
    *,
    lstat: Callable[[str], object] = os.lstat,
    walk: Callable[..., Iterable[tuple[str, list[str], list[str]]]] = _walk,
    max_entries: int = DEFAULT_WALK_CAP,
) -> list[Finding]:
    """Every entry the walk could not list or ``lstat`` is counted and
    reported as ``walk-incomplete`` (honesty audit 2026-09-03, 04-F5): a
    tree the lens could only partly see is never reported clean."""
    findings: list[Finding] = []
    for top in ctx.tenant.host_eviction_watch_paths:
        subject = f"watch path {top}"
        if not Path(top).is_dir():
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="missing",
                    severity="WARN",
                    detail="a configured engine-read tree does not exist or is not a "
                    "directory; the engine cannot read what is not there",
                )
            )
            continue
        placeholders: list[str] = []
        dataless: list[str] = []
        skipped: list[str] = []  # paths the walk could not list or stat

        def unlistable(err: OSError, _into: list[str] = skipped) -> None:
            _into.append(str(getattr(err, "filename", None) or "?"))

        entries = 0
        capped = False
        for dirpath, dirnames, filenames in walk(top, unlistable):
            for name in [*dirnames, *filenames]:
                entries += 1
                if entries > max_entries:
                    capped = True
                    break
                full = os.path.join(dirpath, name)
                if name.startswith(".") and name.endswith(ICLOUD_PLACEHOLDER_SUFFIX):
                    placeholders.append(name[1 : -len(ICLOUD_PLACEHOLDER_SUFFIX)])
                    continue
                try:
                    flags = getattr(lstat(full), "st_flags", 0)
                except OSError:
                    skipped.append(full)
                    continue
                if flags & SF_DATALESS:
                    dataless.append(name)
            if capped:
                break
        if placeholders or dataless:
            examples = ", ".join((placeholders + dataless)[:3])
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="evicted-files",
                    severity="CRITICAL",
                    detail=f"{len(placeholders)} iCloud placeholder(s) and {len(dataless)} "
                    f"dataless File Provider item(s) inside an engine-read tree (e.g. "
                    f"{examples}); a headless read of these blocks or crashes. Free disk "
                    "space or pin the folder (Keep Downloaded / Always Keep on This Device)",
                )
            )
        if skipped:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="walk-incomplete",
                    severity="WARN",
                    detail=f"{len(skipped)} entr{'y' if len(skipped) == 1 else 'ies'} could "
                    f"not be listed or stat'ed (first: {skipped[0]}); that part of the tree "
                    "was not inspected for evicted items (permissions, a dataless folder, "
                    "or a File Provider deadlock)",
                )
            )
        if capped:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="walk-capped",
                    severity="WARN",
                    detail=f"the eviction walk stopped after {max_entries} entries; the "
                    "tree is larger than this lens expects and was not fully inspected",
                )
            )
    return findings


def check(ctx: AuditContext, *, platform: str | None = None) -> list[Finding]:
    """``[auditor.host]`` gates the whole lens: an absent section disables it.
    ``platform`` is the one seam the two macOS-only probes read (``diskutil``
    for the APFS container, ``tmutil`` for the backup); it defaults to this
    host's own and is injectable so the evals can pin both platforms."""
    if not ctx.tenant.host_enabled:
        return []
    return [
        *check_disk_free(ctx, usage=_disk_usage, container=_container_usage, platform=platform),
        *check_time_machine(
            ctx,
            destinations=_tm_destinations,
            latest_backup=_tm_latest_backup,
            platform=platform,
        ),
        *check_expected_volumes(ctx, is_mounted=_is_mounted),
        *check_eviction(ctx),
    ]

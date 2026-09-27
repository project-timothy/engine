"""Command-line entry point: ``auditor run <tenant>``.

Exit codes mirror the engine CLI: 0 for a completed audit (findings are the
owner's checklist, not a process failure), 1 for an audit error, 2 for usage
or configuration errors.
"""

from __future__ import annotations

import argparse
import sys

from .config import AuditorConfigError, load_auditor_tenant
from .lenses import LENSES
from .runner import AuditorReportDirError, resolve_store_root, run_audit
from .store import AuditorStore


def _add_dirs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenants-dir", default=None, help="tenants root (default: ./tenants)")
    parser.add_argument(
        "--ledger-dir",
        default=None,
        help="engine ledger base directory (default: $ENGINE_LEDGER_ROOT or ./.ledger)",
    )
    parser.add_argument(
        "--store-dir",
        default=None,
        help="auditor store base directory (default: $AUDITOR_STORE_ROOT or ./.auditor)",
    )


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        result = run_audit(
            args.tenant,
            tenants_dir=args.tenants_dir,
            ledger_dir=args.ledger_dir,
            store_dir=args.store_dir,
            report_dir=args.report_dir,
            local_only=args.local_only,
            write=not args.no_report,
        )
    except (AuditorConfigError, AuditorReportDirError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"audit error: {exc}", file=sys.stderr)
        return 1

    print(
        f"audit @ {result.tenant}: {result.new_count} new, "
        f"{result.open_count} open, {result.resolved_count} resolved"
    )
    if result.lens_errors:
        for err in result.lens_errors:
            print(f"  lens error: {err}", file=sys.stderr)
    if result.report_path:
        print(f"  report: {result.report_path}")
    else:
        print(result.report_text)
    return 0


def _cmd_checklist(args: argparse.Namespace) -> int:
    try:
        load_auditor_tenant(args.tenant, tenants_dir=args.tenants_dir)
    except AuditorConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with AuditorStore.open(resolve_store_root(args.tenant, args.store_dir)) as store:
        rows = store.open_findings(args.tenant)
    if not rows:
        print(f"checklist for {args.tenant}: empty")
        return 0
    for row in rows:
        print(
            f"[ ] {row['severity']} since {row['first_seen'][:10]} "
            f"({row['lens']}) {row['subject']}: {row['detail']}"
        )
    return 0


def _cmd_lenses() -> int:
    if not LENSES:
        print("no lenses registered yet")
        return 0
    for lens in LENSES:
        kind = "external" if lens.external else "local"
        print(f"{lens.name} ({kind})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auditor",
        description=(
            "Independent auditor for the back-office engine: recomputes truth "
            "from ground sources and keeps a running checklist for the owner."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run the nightly audit: auditor run <tenant>")
    run_p.add_argument("tenant", help="tenant slug (a directory under tenants/)")
    _add_dirs(run_p)
    run_p.add_argument(
        "--report-dir", default=None, help="override the tenant's [auditor].report_dir"
    )
    run_p.add_argument(
        "--local-only",
        action="store_true",
        help="skip lenses that reach external services (mailbox, accounting system)",
    )
    run_p.add_argument(
        "--no-report",
        action="store_true",
        help="dry run: print the report instead of writing it; the checklist store is "
        "NOT updated, so NEW announcements are kept for the next written report",
    )

    check_p = sub.add_parser("checklist", help="print the open checklist")
    check_p.add_argument("tenant")
    _add_dirs(check_p)

    sub.add_parser("lenses", help="list registered lenses")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "checklist":
        return _cmd_checklist(args)
    if args.command == "lenses":
        return _cmd_lenses()
    return 2  # unreachable; argparse enforces the subcommand


if __name__ == "__main__":
    raise SystemExit(main())

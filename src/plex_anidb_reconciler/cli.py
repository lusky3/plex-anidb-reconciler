"""pxar command line: plan (dry-run), apply, serve."""

import argparse
import logging
import os
import sys
from datetime import UTC, datetime

from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.models import Rule, SnapshotState, Status
from plex_anidb_reconciler.plex import PlexError
from plex_anidb_reconciler.runner import EXECUTABLE_RULES, run_once
from plex_anidb_reconciler.serve import healthcheck, serve
from plex_anidb_reconciler.shoko import ShokoError

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_RUN_FAILED = 2  # a write failed, the run failed, or bad command line (argparse)
EXIT_SNAPSHOT_UNAVAILABLE = 3  # mylist.json missing, unreadable or malformed: nothing was planned


def _rules(raw: str) -> frozenset[Rule]:
    names = {r.strip() for r in raw.split(",") if r.strip()}
    if not names or not names <= set(EXECUTABLE_RULES):
        raise argparse.ArgumentTypeError(f"rules must be a comma list of {','.join(EXECUTABLE_RULES)}")
    return frozenset(Rule(r) for r in names)


def _limit(raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer >= 0") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be an integer >= 0")
    return value


def _add_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--rules", type=_rules, default=None, help="only run these rules (comma list of R1,R2,R2b)")
    p.add_argument("--file-id", type=int, default=None, help="only act on this Shoko file id")
    p.add_argument(
        "--rating-key",
        action="append",
        default=None,
        help="only act on this Plex item (repeatable); everything is still planned as in a full run",
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pxar")
    sub = p.add_subparsers(dest="command", required=True)
    pp = sub.add_parser("plan", help="dry run: report what would change (uncapped)")
    _add_filters(pp)
    ap = sub.add_parser("apply", help="apply changes (capped by MAX_WRITES)")
    ap.add_argument("--limit", type=_limit, default=None, help="maximum writes this run")
    _add_filters(ap)
    sub.add_parser("healthcheck", help="container probe: exit 0 iff the local /healthz answers 200")
    sub.add_parser("serve", help="run the scheduled service and /trigger endpoint")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # no line per HTTP request; runs log their own summary
    if args.command == "healthcheck":  # needs no backend settings
        return healthcheck(os.environ)
    try:
        settings = Settings.from_env(os.environ)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.command == "serve":
        serve(settings)
        return EXIT_OK
    apply = args.command == "apply"
    try:
        result = run_once(
            settings,
            apply=apply,
            limit=getattr(args, "limit", None),
            now=datetime.now(UTC),
            rules=args.rules,
            file_id=args.file_id,
            rating_keys=frozenset(args.rating_key) if args.rating_key else None,
        )
    except (ShokoError, PlexError) as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return EXIT_RUN_FAILED
    except Exception as exc:  # noqa: BLE001 - messages may carry URLs/tokens: print the type only
        print(f"run failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_RUN_FAILED
    print(f"report: {result.report_path}")
    if result.snapshot == SnapshotState.UNAVAILABLE:
        print(f"snapshot unavailable: {result.detail}", file=sys.stderr)
        return EXIT_SNAPSHOT_UNAVAILABLE
    return EXIT_RUN_FAILED if result.counts.get(Status.FAILED) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

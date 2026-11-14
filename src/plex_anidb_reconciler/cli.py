"""pxar command line: plan (dry-run), apply, serve."""

import argparse
import os
import sys
from datetime import UTC, datetime

from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.plex import PlexError
from plex_anidb_reconciler.runner import run_once
from plex_anidb_reconciler.serve import serve
from plex_anidb_reconciler.shoko import ShokoError


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pxar")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="dry run: report what would change")
    ap = sub.add_parser("apply", help="apply changes (capped by MAX_WRITES)")
    ap.add_argument("--limit", type=int, default=None, help="maximum writes this run")
    sub.add_parser("serve", help="run the scheduled service and /trigger endpoint")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env(os.environ)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    if args.command == "serve":
        serve(settings)
        return 0
    apply = args.command == "apply"
    try:
        result = run_once(settings, apply=apply, limit=getattr(args, "limit", None), now=datetime.now(UTC))
    except (ShokoError, PlexError) as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return 2
    print(f"report: {result.report_path}")
    return 2 if result.counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())

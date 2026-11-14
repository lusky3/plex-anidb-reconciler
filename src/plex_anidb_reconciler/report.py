"""CSV report and stdout summary."""

import csv
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from plex_anidb_reconciler.ledger import iso_utc
from plex_anidb_reconciler.models import ActionKind, MyListFileState, Outcome

# New columns are appended so existing CSV consumers keep working.
COLUMNS = [
    "rule",
    "kind",
    "status",
    "rating_key",
    "file_id",
    "fid",
    "eids",
    "when",
    "reason",
    "detail",
    "aid",
    "release_group",
    "anidb_state",
    "anidb_viewdate",
]

FileStateLookup = Callable[[int | None], MyListFileState | None]


def _blank(value: object) -> object:
    return "" if value is None else value


def report_row(outcome: Outcome, file_state: FileStateLookup) -> list[object]:
    """One report row in COLUMNS order. State and view date come from the MyList snapshot for the action's
    fid; aid from Shoko, else from the snapshot."""
    action = outcome.action
    anidb = file_state(action.fid)
    aid = action.aid if action.aid is not None else (anidb.aid if anidb else None)
    return [
        action.rule,
        action.kind.value,
        outcome.status,
        action.rating_key or "",
        _blank(action.file_id),
        _blank(action.fid),
        ";".join(str(e) for e in action.eids),
        iso_utc(action.when),
        action.reason,
        outcome.detail,
        _blank(aid),
        action.release_group or "",
        _blank(anidb.state if anidb else None),
        iso_utc(anidb.viewed_at if anidb else None),
    ]


def _no_file_state(fid: int | None) -> MyListFileState | None:
    return None


def write_report(outcomes: list[Outcome], path: Path, file_state: FileStateLookup = _no_file_state) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        writer.writerows(report_row(outcome, file_state) for outcome in outcomes)


def prune_reports(directory: Path, keep: int = 200) -> None:
    """Keep the newest ``keep`` run reports. Run ids start with a UTC timestamp, so names sort by age."""
    try:
        reports = sorted(p for p in directory.glob("*.csv") if p.is_file())
    except OSError:
        return
    for old in reports[: max(len(reports) - keep, 0)]:
        try:
            old.unlink()
        except OSError:
            pass


def _summary_key(outcome: Outcome) -> str:
    """Writes group by rule + status; reports also by reason (e.g. ``R4 reported no_shoko_match``)."""
    action, status = outcome.action, outcome.status
    return f"{action.rule} {status} {action.reason}" if action.kind is ActionKind.REPORT else f"{action.rule} {status}"


def summarize(outcomes: list[Outcome]) -> str:
    counts = Counter(_summary_key(outcome) for outcome in outcomes)
    if not counts:
        return "no actions"
    return "\n".join(f"{key}: {n}" for key, n in sorted(counts.items()))

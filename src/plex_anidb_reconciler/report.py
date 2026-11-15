"""Per-run CSV and HTML reports, the run meta file and the stdout summary."""

import csv
import html
import json
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from plex_anidb_reconciler.ledger import iso_utc
from plex_anidb_reconciler.models import ActionKind, MyListFileState, Outcome

log = logging.getLogger(__name__)

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


def no_file_state(fid: int | None) -> MyListFileState | None:
    """FileStateLookup for when there is no snapshot: every snapshot column stays empty."""
    return None


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> object:
    """Spreadsheet apps run cells starting with = + - @ (or tab/CR) as formulas: prefix them with '."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def write_report(outcomes: list[Outcome], path: Path, file_state: FileStateLookup = no_file_state) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        writer.writerows([_csv_safe(v) for v in report_row(outcome, file_state)] for outcome in outcomes)


_HTML_STYLE = (
    "body{font-family:system-ui,sans-serif;margin:1rem;color:#1a1a1a;background:#fff}"
    "table{border-collapse:collapse;font-size:.85rem}"
    "th,td{border:1px solid #ccc;padding:.2rem .4rem;text-align:left;white-space:nowrap}"
    "th{background:#f0f0f0;position:sticky;top:0}"
    "@media (prefers-color-scheme:dark){body{color:#e6e6e6;background:#161616}"
    "th{background:#2a2a2a}th,td{border-color:#444}}"
)


def _cell(value: object, tag: str = "td") -> str:
    return f"<{tag}>{html.escape(str(value), quote=True)}</{tag}>"


def write_html_report(outcomes: list[Outcome], path: Path, file_state: FileStateLookup = no_file_state) -> None:
    """Self-contained HTML version of the CSV (no scripts, no external resources): summary counts, then
    one table row per action. Every cell, including the summary, is HTML-escaped."""
    title = html.escape(path.stem)
    summary = "".join(f"<li>{html.escape(line)}</li>" for line in summarize(outcomes).splitlines())
    header = "".join(_cell(column, "th") for column in COLUMNS)
    rows = "".join("<tr>" + "".join(_cell(v) for v in report_row(o, file_state)) + "</tr>" for o in outcomes)
    page = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>pxar run {title}</title><style>{_HTML_STYLE}</style></head><body>"
        f"<h1>pxar run {title}</h1><ul>{summary}</ul>"
        f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></body></html>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")


def write_run_meta(path: Path, meta: dict[str, object]) -> None:
    """``<run_id>.json`` next to the CSV: how the run was started (plan or apply, full or scoped) and its
    snapshot state, which the CSV rows alone can't tell. The web UI reads it; nothing else depends on it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")


def prune_reports(directory: Path, keep: int = 200) -> None:
    """Keep the newest ``keep`` run reports of each format (CSV, HTML, meta JSON). Run ids start with a UTC
    timestamp, so names sort by age."""
    for pattern in ("*.csv", "*.html", "*.json"):
        _prune(directory, pattern, keep)


def _prune(directory: Path, pattern: str, keep: int) -> None:
    try:
        reports = sorted(p for p in directory.glob(pattern) if p.is_file())
    except OSError as exc:
        log.debug("reports not pruned: cannot list them (%s)", type(exc).__name__)
        return
    for old in reports[: max(len(reports) - keep, 0)]:
        try:
            old.unlink()
        except OSError as exc:
            log.debug("old report not removed (%s)", type(exc).__name__)


def _summary_key(outcome: Outcome) -> str:
    """Writes group by rule + status; reports also by reason (e.g. ``R4 reported no_shoko_match``)."""
    action, status = outcome.action, outcome.status
    return f"{action.rule} {status} {action.reason}" if action.kind is ActionKind.REPORT else f"{action.rule} {status}"


def summarize(outcomes: list[Outcome]) -> str:
    counts = Counter(_summary_key(outcome) for outcome in outcomes)
    if not counts:
        return "no actions"
    return "\n".join(f"{key}: {n}" for key, n in sorted(counts.items()))

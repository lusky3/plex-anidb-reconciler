"""CSV report and stdout summary."""

import csv
from collections import Counter
from pathlib import Path

from plex_anidb_reconciler.ledger import iso_utc
from plex_anidb_reconciler.models import Action

COLUMNS = ["rule", "kind", "status", "rating_key", "file_id", "fid", "eids", "when", "reason", "detail"]


def write_report(rows: list[tuple[Action, str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        for a, status, detail in rows:
            writer.writerow(
                [
                    a.rule,
                    a.kind.value,
                    status,
                    a.rating_key or "",
                    "" if a.file_id is None else a.file_id,
                    "" if a.fid is None else a.fid,
                    ";".join(str(e) for e in a.eids),
                    iso_utc(a.when),
                    a.reason,
                    detail,
                ]
            )


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


def summarize(rows: list[tuple[Action, str, str]]) -> str:
    counts = Counter((a.rule, status) for a, status, _ in rows)
    if not counts:
        return "no actions"
    return "\n".join(f"{rule} {status}: {n}" for (rule, status), n in sorted(counts.items()))

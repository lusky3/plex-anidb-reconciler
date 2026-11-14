"""SQLite ledger: idempotency record of every planned/applied action."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from plex_anidb_reconciler.models import Action, ActionKind

STATUSES = ("applied", "failed", "skipped_ledger", "dry_run", "reported", "capped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    rule TEXT,
    kind TEXT,
    rating_key TEXT,
    file_id INT,
    fid INT,
    eids TEXT,
    when_ts TEXT,
    status TEXT,
    detail TEXT,
    created_at TEXT
)
"""


def iso_utc(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat() if value else ""


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def seen_success(self, kind: ActionKind, key: str | int | None) -> bool:
        """True when an action of this kind was already applied for the key
        (rating_key for MARK_PLEX_PLAYED, file_id for SET_FILE_WATCHED)."""
        if kind is ActionKind.MARK_PLEX_PLAYED:
            column = "rating_key"
        elif kind is ActionKind.SET_FILE_WATCHED:
            column = "file_id"
        else:
            return False
        row = self._db.execute(
            f"SELECT 1 FROM actions WHERE kind = ? AND {column} = ? AND status = 'applied' LIMIT 1",
            (kind.value, key),
        ).fetchone()
        return row is not None

    def record(self, run_id: str, action: Action, status: str, detail: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown ledger status: {status}")
        self._db.execute(
            "INSERT INTO actions (run_id, rule, kind, rating_key, file_id, fid, eids, when_ts, status, detail,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                action.rule,
                action.kind.value,
                action.rating_key,
                action.file_id,
                action.fid,
                ",".join(str(e) for e in action.eids),
                iso_utc(action.when) or None,
                status,
                detail,
                datetime.now(UTC).isoformat(),
            ),
        )
        self._db.commit()

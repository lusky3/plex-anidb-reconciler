"""SQLite ledger: audit and idempotency record of writes the reconciler attempted.

Only ``applied`` and ``failed`` rows are stored; planned/skipped/capped/reported actions go to
the per-run CSV report only, so the ledger grows with real writes, not with every poll."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from plex_anidb_reconciler.models import Action, ActionKind, PendingWatch, Status

STATUSES = (Status.APPLIED, Status.FAILED)

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
    created_at TEXT,
    before TEXT,
    after TEXT
);
CREATE INDEX IF NOT EXISTS ix_actions_kind_status_rk ON actions (kind, status, rating_key);
CREATE INDEX IF NOT EXISTS ix_actions_kind_status_file ON actions (kind, status, file_id);
"""


# Static queries only (no SQL built from strings): the key column depends on the action kind.
_SEEN_SUCCESS = {
    ActionKind.MARK_PLEX_PLAYED: "SELECT 1 FROM actions WHERE kind = ? AND rating_key = ? AND status = ? LIMIT 1",
    ActionKind.SET_FILE_WATCHED: "SELECT 1 FROM actions WHERE kind = ? AND file_id = ? AND status = ? LIMIT 1",
}


# Static statements only (no SQL built from strings).
_AUDIT_COLUMNS = ("before", "after")
_ADD_COLUMN = {
    "before": "ALTER TABLE actions ADD COLUMN before TEXT",
    "after": "ALTER TABLE actions ADD COLUMN after TEXT",
}


def _json_or_null(value: dict[str, object] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def iso_utc(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat() if value else ""


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
        self._add_missing_columns()
        # rows from versions that also stored planned/skipped actions: the ledger only audits writes
        self._db.execute("DELETE FROM actions WHERE status NOT IN (?, ?)", STATUSES)
        self._db.commit()

    def _add_missing_columns(self) -> None:
        """Ledgers created before the before/after audit columns existed get them added (old rows: NULL)."""
        present = {row[1] for row in self._db.execute("PRAGMA table_info(actions)")}
        for column in _AUDIT_COLUMNS:
            if column not in present:
                self._db.execute(_ADD_COLUMN[column])

    def close(self) -> None:
        self._db.close()

    def seen_success(self, kind: ActionKind, key: str | int | None) -> bool:
        """True when an action of this kind was already applied for the key
        (rating_key for MARK_PLEX_PLAYED, file_id for SET_FILE_WATCHED)."""
        query = _SEEN_SUCCESS.get(kind)
        if query is None:
            return False
        return self._db.execute(query, (kind.value, key, Status.APPLIED)).fetchone() is not None

    def applied_r1_keys(self) -> frozenset[str]:
        """Rating keys the reconciler itself marked played in Plex (R1)."""
        rows = self._db.execute(
            "SELECT DISTINCT rating_key FROM actions WHERE kind = ? AND status = ? AND rating_key IS NOT NULL",
            (ActionKind.MARK_PLEX_PLAYED.value, Status.APPLIED),
        )
        return frozenset(r[0] for r in rows)

    def applied_sets(self) -> list[PendingWatch]:
        """Applied file watches, in write order (the snapshot may not show them yet)."""
        rows = self._db.execute(
            "SELECT file_id, fid, eids, when_ts FROM actions WHERE kind = ? AND status = ?"
            " AND file_id IS NOT NULL AND when_ts IS NOT NULL ORDER BY id",
            (ActionKind.SET_FILE_WATCHED.value, Status.APPLIED),
        )
        return [
            PendingWatch(
                file_id=file_id,
                fid=fid,
                eids=tuple(int(e) for e in (eids or "").split(",") if e),
                when=datetime.fromisoformat(when_ts),
            )
            for file_id, fid, eids, when_ts in rows
        ]

    def record(
        self,
        run_id: str,
        action: Action,
        status: Status | str,
        detail: str = "",
        *,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        """Store one applied or failed write; before/after (JSON) are the audited state around it."""
        if status not in STATUSES:
            raise ValueError(f"unknown ledger status: {status}")
        self._db.execute(
            "INSERT INTO actions (run_id, rule, kind, rating_key, file_id, fid, eids, when_ts, status, detail,"
            " created_at, before, after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                action.rule,
                action.kind.value,
                action.rating_key,
                action.file_id,
                action.fid,
                ",".join(str(e) for e in action.eids),
                iso_utc(action.when) or None,
                str(status),
                detail,
                datetime.now(UTC).isoformat(),
                _json_or_null(before),
                _json_or_null(after),
            ),
        )
        self._db.commit()

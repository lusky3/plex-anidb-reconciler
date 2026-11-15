import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from plex_anidb_reconciler.ledger import Ledger
from plex_anidb_reconciler.models import Action, ActionKind, PendingWatch


def act(kind, rule, rating_key, *, file_id=None, fid=None, eids=(), when=None):
    return Action(
        kind=kind, rule=rule, rating_key=rating_key, file_id=file_id, fid=fid, eids=eids, when=when, reason="r"
    )


R1 = act(ActionKind.MARK_PLEX_PLAYED, "R1", "rk1", eids=(50,))
SET = act(
    ActionKind.SET_FILE_WATCHED, "R2", "rk2", file_id=7, fid=12345, eids=(50,), when=datetime(2026, 1, 1, tzinfo=UTC)
)


@pytest.fixture
def open_ledger():
    """Open ledgers through this so every connection is closed at teardown."""
    opened = []

    def _open(path):
        led = Ledger(path)
        opened.append(led)
        return led

    yield _open
    for led in opened:
        led.close()


def test_only_applied_counts_as_seen(tmp_path, open_ledger):
    led = open_ledger(tmp_path / "l.sqlite")
    led.record("run", R1, "failed")
    assert not led.seen_success(ActionKind.MARK_PLEX_PLAYED, "rk1")
    led.record("run", R1, "applied")
    led.record("run", SET, "applied")
    assert led.seen_success(ActionKind.MARK_PLEX_PLAYED, "rk1")
    assert led.seen_success(ActionKind.SET_FILE_WATCHED, 7)
    assert not led.seen_success(ActionKind.SET_FILE_WATCHED, 8)
    assert not led.seen_success(ActionKind.MARK_PLEX_PLAYED, "rk2")


def test_persists_across_instances_and_rejects_bad_status(tmp_path, open_ledger):
    p = tmp_path / "sub" / "l.sqlite"
    open_ledger(p).record("run", SET, "applied")
    led = open_ledger(p)
    assert led.seen_success(ActionKind.SET_FILE_WATCHED, 7)
    with pytest.raises(ValueError):
        led.record("run", SET, "bogus")


def test_ledger_stores_only_applied_and_failed(tmp_path, open_ledger):
    led = open_ledger(tmp_path / "l.sqlite")
    for status in ("dry_run", "capped", "skipped_ledger", "reported"):
        with pytest.raises(ValueError):
            led.record("run", R1, status)
    led.record("run", R1, "failed", "x")
    led.record("run", SET, "applied")
    assert led._db.execute("SELECT status FROM actions ORDER BY id").fetchall() == [("failed",), ("applied",)]
    idx = {r[0] for r in led._db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"ix_actions_kind_status_rk", "ix_actions_kind_status_file"} <= idx


def test_old_non_audit_rows_are_dropped_on_open(tmp_path, open_ledger):
    p = tmp_path / "l.sqlite"
    led = open_ledger(p)
    led._db.execute("INSERT INTO actions (status, kind) VALUES ('reported', 'report'), ('dry_run', 'report')")
    led._db.commit()
    led.close()
    assert open_ledger(p)._db.execute("SELECT COUNT(*) FROM actions").fetchone() == (0,)


def test_applied_r1_keys_and_pending_sets(tmp_path, open_ledger):
    led = open_ledger(tmp_path / "l.sqlite")
    led.record("run", R1, "applied")
    led.record("run", act(ActionKind.MARK_PLEX_PLAYED, "R1", "rk9"), "failed")
    led.record("run", SET, "applied")
    led.record(
        "run", act(ActionKind.SET_FILE_WATCHED, "R2", "rk3", file_id=8, fid=99, eids=(60, 61), when=SET.when), "failed"
    )
    assert led.r1_marked_keys() == frozenset({"rk1", "rk9"})  # a failed scrobble may still have landed in Plex
    assert led.applied_sets() == [PendingWatch(7, 12345, (50,), SET.when)]


def test_record_stores_before_and_after_as_json(tmp_path, open_ledger):
    led = open_ledger(tmp_path / "l.sqlite")
    led.record("run", SET, "applied", before={"b": 1, "when": None}, after={"a": "x"})
    led.record("run", R1, "failed", "boom")
    rows = led._db.execute("SELECT before, after FROM actions ORDER BY id").fetchall()
    assert [(json.loads(b) if b else None, json.loads(a) if a else None) for b, a in rows] == [
        ({"b": 1, "when": None}, {"a": "x"}),
        (None, None),
    ]


OLD_SCHEMA = """
CREATE TABLE actions (
    id INTEGER PRIMARY KEY, run_id TEXT, rule TEXT, kind TEXT, rating_key TEXT, file_id INT, fid INT,
    eids TEXT, when_ts TEXT, status TEXT, detail TEXT, created_at TEXT
);
INSERT INTO actions (run_id, rule, kind, rating_key, file_id, fid, eids, when_ts, status, detail, created_at)
VALUES ('old', 'R2', 'set_file_watched', 'rk2', 7, 12345, '50', '2026-01-01T00:00:00+00:00', 'applied', '', 'x');
"""


def test_existing_ledger_without_before_after_is_migrated(tmp_path, open_ledger):
    p = tmp_path / "l.sqlite"
    with closing(sqlite3.connect(p)) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.commit()
    led = open_ledger(p)
    cols = [r[1] for r in led._db.execute("PRAGMA table_info(actions)")]
    assert cols[-2:] == ["before", "after"]
    assert led.applied_sets() == [PendingWatch(7, 12345, (50,), datetime(2026, 1, 1, tzinfo=UTC))]
    assert led.seen_success(ActionKind.SET_FILE_WATCHED, 7)
    led.record("run", R1, "applied", before={"plex_view_count": 0}, after={"plex_played": True})
    led.close()
    again = open_ledger(p)  # opening a migrated ledger again is a no-op
    assert again._db.execute("SELECT COUNT(*), COUNT(before) FROM actions").fetchone() == (2, 1)


# --- pending R1 rows: written before the scrobble, finished after it ---


def test_pending_r1_counts_as_marked_but_not_as_seen(tmp_path, open_ledger):
    led = open_ledger(tmp_path / "l.sqlite")
    row = led.start("run", R1)
    assert led.r1_marked_keys() == frozenset({"rk1"})
    assert not led.seen_success(ActionKind.MARK_PLEX_PLAYED, "rk1")  # a crash before the scrobble: R1 may run again
    led.finish(row, "applied", before={"plex_view_count": 0}, after={"plex_played": True})
    assert led.seen_success(ActionKind.MARK_PLEX_PLAYED, "rk1")
    assert led._db.execute("SELECT status, before, after FROM actions").fetchall() == [
        ("applied", '{"plex_view_count": 0}', '{"plex_played": true}')
    ]


def test_failed_r1_still_counts_as_marked(tmp_path, open_ledger):
    # A scrobble can time out after Plex applied it: its lastViewedAt must never become an AniDB date.
    led = open_ledger(tmp_path / "l.sqlite")
    led.finish(led.start("run", R1), "failed", "Plex GET /:/scrobble returned HTTP 401")
    assert led.r1_marked_keys() == frozenset({"rk1"})
    assert not led.seen_success(ActionKind.MARK_PLEX_PLAYED, "rk1")  # a really failed scrobble may be retried
    assert led._db.execute("SELECT status, detail FROM actions").fetchall() == [
        ("failed", "Plex GET /:/scrobble returned HTTP 401")
    ]


def test_pending_rows_survive_reopen(tmp_path, open_ledger):
    p = tmp_path / "l.sqlite"
    led = open_ledger(p)
    led.start("run", R1)
    led.close()
    again = open_ledger(p)
    assert again.r1_marked_keys() == frozenset({"rk1"})
    assert again._db.execute("SELECT status FROM actions").fetchall() == [("pending",)]


def test_pending_is_only_for_r1_and_finish_rejects_non_final_status(tmp_path, open_ledger):
    led = open_ledger(tmp_path / "l.sqlite")
    with pytest.raises(ValueError):
        led.start("run", SET)
    with pytest.raises(ValueError):
        led.record("run", R1, "pending")
    row = led.start("run", R1)
    with pytest.raises(ValueError):
        led.finish(row, "pending")


def test_old_ledger_gets_pending_support_without_losing_rows(tmp_path, open_ledger):
    p = tmp_path / "l.sqlite"
    with closing(sqlite3.connect(p)) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.commit()
    led = open_ledger(p)
    led.start("run", R1)
    assert led.applied_sets() == [PendingWatch(7, 12345, (50,), datetime(2026, 1, 1, tzinfo=UTC))]
    assert led.r1_marked_keys() == frozenset({"rk1"})

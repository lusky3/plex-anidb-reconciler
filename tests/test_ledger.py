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
    assert led.applied_r1_keys() == frozenset({"rk1"})
    assert led.applied_sets() == [PendingWatch(7, 12345, (50,), SET.when)]

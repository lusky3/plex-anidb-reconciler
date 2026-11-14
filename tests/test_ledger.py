from datetime import UTC, datetime

import pytest

from plex_anidb_reconciler.ledger import Ledger
from plex_anidb_reconciler.models import Action, ActionKind

R1 = Action(ActionKind.MARK_PLEX_PLAYED, "R1", "rk1", None, None, (50,), None, "r")
SET = Action(ActionKind.SET_FILE_WATCHED, "R2", "rk2", 7, 12345, (50,), datetime(2026, 1, 1, tzinfo=UTC), "r")


def test_only_applied_counts_as_seen(tmp_path):
    led = Ledger(tmp_path / "l.sqlite")
    for status in ("failed", "dry_run", "capped", "skipped_ledger"):
        led.record("run", R1, status)
    assert not led.seen_success(ActionKind.MARK_PLEX_PLAYED, "rk1")
    led.record("run", R1, "applied")
    led.record("run", SET, "applied")
    assert led.seen_success(ActionKind.MARK_PLEX_PLAYED, "rk1")
    assert led.seen_success(ActionKind.SET_FILE_WATCHED, 7)
    assert not led.seen_success(ActionKind.SET_FILE_WATCHED, 8)
    assert not led.seen_success(ActionKind.MARK_PLEX_PLAYED, "rk2")


def test_persists_across_instances_and_rejects_bad_status(tmp_path):
    p = tmp_path / "sub" / "l.sqlite"
    Ledger(p).record("run", SET, "applied")
    led = Ledger(p)
    assert led.seen_success(ActionKind.SET_FILE_WATCHED, 7)
    with pytest.raises(ValueError):
        led.record("run", SET, "bogus")

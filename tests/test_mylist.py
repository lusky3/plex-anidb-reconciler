import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from plex_anidb_reconciler.mylist import MyListIndex, SnapshotError, load_mylist

FIXTURE = Path(__file__).parent / "fixtures" / "mylist_sample.json"
EST = timezone(timedelta(hours=-5))


def test_parses_offsets_to_aware_datetimes():
    entries = load_mylist(FIXTURE)
    assert len(entries) == 5
    first = entries[0]
    assert first.viewed_at == datetime(2023, 3, 1, 21, 0, tzinfo=EST)
    assert first.viewed_at.utcoffset() == timedelta(hours=-5)
    assert (first.fid, first.eid, first.aid, first.state) == (999, 50, 10, 3)
    assert entries[1].viewed_at is None
    assert entries[2].fid is None  # FileID 0 -> None


def test_episode_watched_via_other_release():
    index = MyListIndex(load_mylist(FIXTURE))
    assert index.episode_watched_at(50) == [datetime(2023, 3, 1, 21, 0, tzinfo=EST)]
    assert index.fid_watched(12345) is False
    assert index.fid_watched(999) is True
    assert index.fid_watched(None) is False
    assert index.fid_watched(0) is False


def test_generic_entry_counts_for_episode():
    index = MyListIndex(load_mylist(FIXTURE))
    assert 51 in index.watched_eids()
    assert 52 not in index.watched_eids()


def test_null_episode_ignored():
    index = MyListIndex(load_mylist(FIXTURE))
    assert index.fid_watched(888) is True
    assert index.watched_eids() == {50, 51}
    assert index.episode_watched_at(52) == []


def test_max_watch():
    index = MyListIndex(load_mylist(FIXTURE))
    assert index.max_watch([50, 51]) == datetime(2023, 3, 2, 9, 0, tzinfo=EST)
    assert index.max_watch([52, 999]) is None


def test_invalid_json_raises_snapshot_error(tmp_path):
    p = tmp_path / "mylist.json"
    p.write_text(FIXTURE.read_text()[:100])
    with pytest.raises(SnapshotError) as exc:
        load_mylist(p)
    assert str(p) in str(exc.value)


def test_missing_file_raises_snapshot_error(tmp_path):
    with pytest.raises(SnapshotError, match="nope.json"):
        load_mylist(tmp_path / "nope.json")


def test_non_list_raises_snapshot_error(tmp_path):
    p = tmp_path / "m.json"
    p.write_text('{"a": 1}')
    with pytest.raises(SnapshotError):
        load_mylist(p)


def test_naive_timestamp_raises_snapshot_error(tmp_path):
    p = tmp_path / "m.json"
    p.write_text('[{"AnimeID": 1, "EpisodeID": 2, "FileID": 3, "ViewedAt": "2023-03-01T21:00:00", "State": 3}]')
    with pytest.raises(SnapshotError, match="m.json"):
        load_mylist(p)


def test_snapshot_age(tmp_path):
    from plex_anidb_reconciler.mylist import snapshot_age

    p = tmp_path / "m.json"
    p.write_text("[]")
    mtime = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    os.utime(p, (mtime.timestamp(), mtime.timestamp()))
    assert snapshot_age(p, mtime + timedelta(hours=5)) == timedelta(hours=5)


def test_non_utf8_raises_snapshot_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_bytes(b"\xff\xfe[\x80")
    with pytest.raises(SnapshotError, match="bad.json"):
        load_mylist(p)


def test_has_fid_counts_unwatched_entries():
    from plex_anidb_reconciler.models import MyListEntry

    ml = MyListIndex(
        [
            MyListEntry(fid=5, eid=1, aid=1, viewed_at=None, state=1),
            MyListEntry(fid=6, eid=2, aid=1, viewed_at=datetime(2023, 1, 1, tzinfo=UTC), state=1),
        ]
    )
    assert ml.has_fid(5) and ml.has_fid(6)
    assert not ml.fid_watched(5) and ml.fid_watched(6)
    assert not ml.has_fid(7) and not ml.has_fid(None) and not ml.has_fid(0)


def test_eids_for_fid_collects_non_null_eids():
    from plex_anidb_reconciler.models import MyListEntry

    ml = MyListIndex(
        [
            MyListEntry(fid=5, eid=1, aid=1, viewed_at=None, state=1),
            MyListEntry(fid=5, eid=2, aid=1, viewed_at=None, state=1),
            MyListEntry(fid=5, eid=None, aid=1, viewed_at=None, state=1),
            MyListEntry(fid=6, eid=None, aid=1, viewed_at=None, state=1),
        ]
    )
    assert ml.eids_for_fid(5) == frozenset({1, 2})
    assert ml.eids_for_fid(6) == frozenset()
    assert ml.eids_for_fid(7) == frozenset()
    assert ml.eids_for_fid(None) == frozenset()


# --- snapshot_age hardening ---


def test_snapshot_age_missing_file_raises_snapshot_error(tmp_path):
    from plex_anidb_reconciler.mylist import snapshot_age

    with pytest.raises(SnapshotError, match="gone.json"):
        snapshot_age(tmp_path / "gone.json", datetime(2024, 1, 1, tzinfo=UTC))


def test_snapshot_age_future_mtime_clamped_to_zero(tmp_path):
    from plex_anidb_reconciler.mylist import snapshot_age

    p = tmp_path / "m.json"
    p.write_text("[]")
    mtime = datetime(2024, 1, 1, 12, tzinfo=UTC)
    os.utime(p, (mtime.timestamp(), mtime.timestamp()))
    assert snapshot_age(p, mtime - timedelta(hours=3)) == timedelta(0)


# --- entry types: the whole snapshot is rejected on any malformed entry (fail closed) ---


def write_entries(tmp_path, entries):
    import json

    p = tmp_path / "m.json"
    p.write_text(json.dumps(entries))
    return p


def good(**over):
    return {"AnimeID": 1, "EpisodeID": 2, "FileID": 3, "ViewedAt": None, "State": 1, **over}


def test_numeric_strings_are_coerced_to_int(tmp_path):
    (e,) = load_mylist(write_entries(tmp_path, [good(AnimeID="1", EpisodeID="2", FileID="3", State="3")]))
    assert (e.aid, e.eid, e.fid, e.state) == (1, 2, 3, 3)
    assert MyListIndex([e]).fid_watched(3) is False and MyListIndex([e]).has_fid(3)


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-object",
        ["list"],
        good(FileID="abc"),
        good(EpisodeID=1.5),
        good(AnimeID={"x": 1}),
        good(FileID=True),
        good(ViewedAt="yesterday"),
        good(ViewedAt=12345),
        {k: v for k, v in good().items() if k != "State"},
        good(State=None),
        good(State="x"),
    ],
    ids=[
        "string",
        "list",
        "fid_str",
        "eid_float",
        "aid_obj",
        "fid_bool",
        "viewed_text",
        "viewed_int",
        "no_state",
        "null_state",
        "state_str",
    ],
)
def test_malformed_entry_rejects_snapshot_with_count(tmp_path, bad):
    p = write_entries(tmp_path, [good(), bad, good(FileID=4)])
    with pytest.raises(SnapshotError) as exc:
        load_mylist(p)
    assert "m.json" in str(exc.value)
    assert "1 malformed" in str(exc.value)

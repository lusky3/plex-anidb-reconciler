from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from plex_anidb_reconciler.models import ActionKind, MyListEntry, PlexItem, ShokoFile
from plex_anidb_reconciler.mylist import MyListIndex
from plex_anidb_reconciler.paths import PathMatcher
from plex_anidb_reconciler.rules import plan

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 2, 1, tzinfo=UTC)
T3 = datetime(2026, 3, 1, tzinfo=UTC)
T2025 = datetime(2025, 6, 1, tzinfo=UTC)


def entry(fid, eid, when):
    return MyListEntry(fid=fid, eid=eid, aid=1, viewed_at=when, state=1)


def shoko(file_id, fid, eids, path, manual=False):
    return ShokoFile(file_id, fid, tuple(eids), manual, (path,))


def item(rk, *paths, views=1, last=None, section=4):
    return PlexItem(rk, section, tuple(paths), views, last)


@dataclass
class Scenario:
    items: list
    files: list
    entries: list = field(default_factory=list)
    history: dict = field(default_factory=dict)
    policy: str = "record"
    fresh: bool = True

    def run(self):
        return plan(
            self.items,
            self.history,
            PathMatcher(self.files, []),
            MyListIndex(self.entries),
            self.policy,
            self.fresh,
        )

    def mylist(self):
        return MyListIndex(self.entries)


def simple(**kw):
    """One Plex item (rk 'a') backed by file 1 (fid 12345, eid 50)."""
    base = {"items": [item("a", "/p/x.mkv", last=T2)], "files": [shoko(1, 12345, [50], "/p/x.mkv")]}
    base.update(kw)
    return Scenario(**base)


def s_wiped_manual_remark():
    return simple(entries=[entry(12345, 50, T2025)])


def s_reupload():
    return simple(items=[item("a", "/p/x.mkv", views=0)], entries=[entry(999, 50, T1)])


def s_new_watch():
    return simple(history={"a": [T2, T1]})


def s_manual_mark():
    return simple(items=[item("a", "/p/x.mkv", last=T3)])


def s_r2b_after():
    return simple(entries=[entry(999, 50, T1)], history={"a": [T3, T2]})


def s_r2b_before():
    return simple(entries=[entry(999, 50, T2)], history={"a": [T1]})


def s_r2b_manual():
    return simple(entries=[entry(999, 50, T1)])


def s_r2b_ignore():
    return simple(entries=[entry(999, 50, T1)], history={"a": [T3]}, policy="ignore")


def s_stale():
    return simple(
        items=[item("a", "/p/x.mkv", last=T2), item("b", "/p/y.mkv", views=0)],
        files=[shoko(1, 12345, [50], "/p/x.mkv"), shoko(2, 777, [60], "/p/y.mkv")],
        entries=[entry(888, 60, T1)],
        history={"a": [T2]},
        fresh=False,
    )


def s_stale_r2b():
    return simple(entries=[entry(999, 50, T1)], history={"a": [T3]}, fresh=False)


def s_no_date():
    return simple(items=[item("a", "/p/x.mkv", last=None)])


def s_manual_link():
    return simple(files=[shoko(1, 12345, [50], "/p/x.mkv", manual=True)])


def s_unrecognised():
    return simple(files=[shoko(1, None, [50], "/p/x.mkv")])


def s_no_match():
    return simple(items=[item("a", "/nowhere/z.mkv")])


def s_ambiguous():
    return simple(
        items=[item("a", "/q/Season 01/x.mkv")],
        files=[shoko(1, 11, [50], "/r1/Season 01/x.mkv"), shoko(2, 22, [51], "/r2/Season 01/x.mkv")],
    )


def s_multi_partial_watched():
    return simple(
        files=[shoko(1, 12345, [50, 51], "/p/x.mkv")],
        entries=[entry(999, 50, T1)],
        history={"a": [T3]},
    )


def s_multi_partial_unwatched():
    return simple(
        items=[item("a", "/p/x.mkv", views=0)],
        files=[shoko(1, 12345, [50, 51], "/p/x.mkv")],
        entries=[entry(999, 50, T1)],
    )


def s_two_libraries():
    return Scenario(
        items=[item("b", "/p2/x.mkv", section=5, last=T3), item("a", "/p/x.mkv", last=T2)],
        files=[ShokoFile(1, 12345, (50,), False, ("/p/x.mkv", "/p2/x.mkv"))],
        history={"a": [T2], "b": [T1]},
    )


def s_r1_already_fid_watched():
    return simple(items=[item("a", "/p/x.mkv", views=0)], entries=[entry(12345, 50, T1)])


def s_two_paths_one_item():
    return simple(
        items=[item("a", "/p/x.mkv", "/p/y.mkv")],
        files=[shoko(1, 12345, [50], "/p/x.mkv"), shoko(2, 777, [60], "/p/y.mkv")],
        history={"a": [T1]},
    )


SCENARIOS = [
    s_wiped_manual_remark,
    s_reupload,
    s_new_watch,
    s_manual_mark,
    s_r2b_after,
    s_r2b_before,
    s_r2b_manual,
    s_r2b_ignore,
    s_stale,
    s_stale_r2b,
    s_no_date,
    s_manual_link,
    s_unrecognised,
    s_no_match,
    s_ambiguous,
    s_multi_partial_watched,
    s_multi_partial_unwatched,
    s_two_libraries,
    s_r1_already_fid_watched,
    s_two_paths_one_item,
]


def sets(actions):
    return [a for a in actions if a.kind is ActionKind.SET_FILE_WATCHED]


def reports(actions):
    return [(a.rule, a.reason) for a in actions if a.kind is ActionKind.REPORT]


def test_plex_wiped_manual_remark_same_file_no_write():
    assert s_wiped_manual_remark().run() == []


def test_reupload_different_release_marks_plex_only():
    acts = s_reupload().run()
    assert [(a.kind, a.rule) for a in acts] == [(ActionKind.MARK_PLEX_PLAYED, "R1")]
    a = acts[0]
    assert (a.rating_key, a.file_id, a.fid, a.eids, a.when, a.reason) == (
        "a",
        None,
        None,
        (50,),
        None,
        "anidb_episode_watched",
    )


def test_new_watch_records_exact_fid_with_first_playback():
    (a,) = s_new_watch().run()
    assert (a.kind, a.rule, a.fid, a.file_id, a.when, a.rating_key) == (
        ActionKind.SET_FILE_WATCHED,
        "R2",
        12345,
        1,
        T1,
        "a",
    )
    assert (a.eids, a.reason) == ((50,), "new_watch")


def test_manual_mark_new_episode_uses_last_viewed_at():
    (a,) = s_manual_mark().run()
    assert (a.rule, a.when, a.fid) == ("R2", T3, 12345)


def test_r2b_record_requires_playback_after_anidb_watch():
    (a,) = s_r2b_after().run()
    assert (a.kind, a.rule, a.when, a.fid, a.reason) == (
        ActionKind.SET_FILE_WATCHED,
        "R2b",
        T2,
        12345,
        "rewatch_new_release",
    )
    acts = s_r2b_before().run()
    assert sets(acts) == []
    assert reports(acts) == [("R2b", "no_playback")]


def test_r2b_manual_mark_never_records():
    acts = s_r2b_manual().run()
    assert sets(acts) == []
    assert reports(acts) == [("R2b", "no_playback")]


def test_r2b_ignore_policy():
    acts = s_r2b_ignore().run()
    assert sets(acts) == []
    assert reports(acts) == [("R2b", "ignore_policy")]


def test_stale_snapshot_defers_r2_but_not_r1():
    acts = s_stale().run()
    assert sets(acts) == []
    assert [(a.kind, a.rule, a.rating_key) for a in acts] == [
        (ActionKind.REPORT, "R4", "a"),
        (ActionKind.MARK_PLEX_PLAYED, "R1", "b"),
    ]
    assert acts[0].reason == "stale_snapshot"
    assert (acts[0].file_id, acts[0].fid, acts[0].when) == (1, 12345, T2)


def test_stale_snapshot_defers_r2b():
    acts = s_stale_r2b().run()
    assert sets(acts) == []
    assert reports(acts) == [("R2b", "stale_snapshot")]


def test_no_date_reports():
    acts = s_no_date().run()
    assert sets(acts) == []
    assert reports(acts) == [("R4", "no_date")]


def test_manual_link_and_unrecognised_report_only():
    assert [(a.kind, a.rule, a.reason) for a in s_manual_link().run()] == [(ActionKind.REPORT, "R4", "manual_link")]
    assert [(a.kind, a.rule, a.reason) for a in s_unrecognised().run()] == [(ActionKind.REPORT, "R4", "unrecognised")]


def test_unmatched_and_ambiguous_paths_report_only():
    assert reports(s_no_match().run()) == [("R4", "no_shoko_match")]
    assert reports(s_ambiguous().run()) == [("R4", "ambiguous_path")]


def test_multi_episode_partial_reports():
    assert reports(s_multi_partial_watched().run()) == [("R4", "partial_episode")]
    assert reports(s_multi_partial_unwatched().run()) == [("R4", "partial_episode")]


def test_file_with_no_episodes_is_never_marked_played():
    sc = simple(items=[item("a", "/p/x.mkv", views=0)], files=[shoko(1, 12345, [], "/p/x.mkv")])
    acts = sc.run()
    assert [a.kind for a in acts] == [ActionKind.REPORT]


def test_same_file_two_libraries_one_write_earliest_date():
    acts = s_two_libraries().run()
    (a,) = sets(acts)
    assert (a.file_id, a.when, a.rating_key) == (1, T1, "b")
    assert len(acts) == 1


def test_fid_already_watched_skips_and_r1_not_needed_when_plex_unwatched_but_fid_watched():
    acts = s_r1_already_fid_watched().run()
    assert [(a.kind, a.rule) for a in acts] == [(ActionKind.MARK_PLEX_PLAYED, "R1")]


def test_output_sorted_by_rating_key_then_file_id():
    acts = s_two_paths_one_item().run()
    assert [(a.rating_key, a.file_id) for a in acts] == [("a", 1), ("a", 2)]
    mixed = Scenario(
        items=[item("b", "/p/y.mkv", views=0), item("a", "/p/x.mkv", last=T2)],
        files=[shoko(1, 12345, [50], "/p/x.mkv"), shoko(2, 777, [60], "/p/y.mkv")],
        entries=[entry(1, 60, T1)],
    )
    assert [a.rating_key for a in mixed.run()] == ["a", "b"]


@pytest.mark.parametrize("builder", SCENARIOS, ids=lambda b: b.__name__)
def test_never_emits_unwatch(builder):
    sc = builder()
    ml = sc.mylist()
    for a in sc.run():
        assert a.kind in {ActionKind.MARK_PLEX_PLAYED, ActionKind.SET_FILE_WATCHED, ActionKind.REPORT}
        if a.kind is ActionKind.SET_FILE_WATCHED:
            assert not ml.fid_watched(a.fid)


def test_scenarios_cover_all_action_kinds():
    kinds = {a.kind for b in SCENARIOS for a in b().run()}
    assert kinds == set(ActionKind)

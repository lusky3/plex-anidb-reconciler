from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from plex_anidb_reconciler.models import ActionKind, MyListEntry, PendingWatch, PlexItem, ShokoFile
from plex_anidb_reconciler.mylist import MyListIndex
from plex_anidb_reconciler.paths import PathMatcher
from plex_anidb_reconciler.rules import plan

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 2, 1, tzinfo=UTC)
T3 = datetime(2026, 3, 1, tzinfo=UTC)
T2025 = datetime(2025, 6, 1, tzinfo=UTC)
T0 = datetime(2025, 1, 1, tzinfo=UTC)  # default Shoko import time (before every playback)


def entry(fid, eid, when):
    return MyListEntry(fid=fid, eid=eid, aid=1, viewed_at=when, state=1)


def shoko(file_id, fid, eids, path, manual=False, created=T0):
    return ShokoFile(file_id, fid, tuple(eids), manual, (path,), created)


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
    if not kw.get("no_snapshot_row"):
        base["entries"] = [entry(12345, 50, None), *base.get("entries", [])]
    base.pop("no_snapshot_row", None)
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
        items=[item("a", "/p/x.mkv", last=T2), item("b", "/p2/x.mkv", section=5, last=T3)],
        files=[ShokoFile(1, 12345, (50,), False, ("/p/x.mkv", "/p2/x.mkv"), T0)],
        entries=[entry(12345, 50, None)],
        history={"a": [T3], "b": [T1]},
    )


def s_r1_already_fid_watched():
    return simple(items=[item("a", "/p/x.mkv", views=0)], entries=[entry(12345, 50, T1)])


def s_two_paths_one_item():
    return simple(
        items=[item("a", "/p/x.mkv", "/p/y.mkv")],
        files=[shoko(1, 12345, [50], "/p/x.mkv"), shoko(2, 777, [60], "/p/y.mkv")],
        entries=[entry(12345, 50, None), entry(777, 60, None)],
        history={"a": [T1]},
    )


def s_same_fid_replayed():
    return simple(entries=[entry(12345, 50, T1)], history={"a": [T3]})


def s_multi_version():
    return simple(
        items=[item("a", "/p/x.mkv", "/p/x2.mkv")],
        files=[shoko(1, 12345, [50], "/p/x.mkv"), shoko(2, 777, [50], "/p/x2.mkv")],
        entries=[entry(12345, 50, None), entry(777, 50, None)],
        history={"a": [T2]},
    )


def s_multi_version_unwatched():
    return simple(
        items=[item("a", "/p/x.mkv", "/p/x2.mkv", views=0)],
        files=[shoko(1, 12345, [50], "/p/x.mkv"), shoko(2, 777, [50], "/p/x2.mkv")],
        entries=[entry(999, 50, T1)],
    )


def s_not_in_snapshot():
    return simple(no_snapshot_row=True, entries=[entry(999, 50, T1)], history={"a": [T3]})


def s_unknown_file_age():
    return simple(
        files=[shoko(1, 12345, [50], "/p/x.mkv", created=None)], entries=[entry(999, 50, T1)], history={"a": [T3]}
    )


def s_r2b_play_before_created():
    return simple(
        files=[shoko(1, 12345, [50], "/p/x.mkv", created=T3)], entries=[entry(999, 50, T1)], history={"a": [T2]}
    )


def s_r2b_play_equals_cutoff():
    return simple(entries=[entry(999, 50, T2)], history={"a": [T2]})


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
    s_same_fid_replayed,
    s_multi_version,
    s_multi_version_unwatched,
    s_not_in_snapshot,
    s_unknown_file_age,
    s_r2b_play_before_created,
    s_r2b_play_equals_cutoff,
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
    assert reports(sc.run()) == [("R4", "no_episodes")]


def test_same_file_two_libraries_one_write_earliest_date():
    acts = s_two_libraries().run()
    (a,) = sets(acts)
    assert (a.file_id, a.when, a.rating_key) == (1, T1, "b")  # earliest item is not first in input
    assert len(acts) == 1


def test_plex_unwatched_with_fid_watched_on_anidb_marks_plex_played():
    acts = s_r1_already_fid_watched().run()
    assert [(a.kind, a.rule) for a in acts] == [(ActionKind.MARK_PLEX_PLAYED, "R1")]


def test_output_sorted_by_rating_key_then_file_id():
    acts = s_two_paths_one_item().run()
    assert [(a.rating_key, a.file_id) for a in acts] == [("a", 1), ("a", 2)]
    mixed = Scenario(
        items=[item("b", "/p/y.mkv", views=0), item("a", "/p/x.mkv", last=T2)],
        files=[shoko(1, 12345, [50], "/p/x.mkv"), shoko(2, 777, [60], "/p/y.mkv")],
        entries=[entry(12345, 50, None), entry(1, 60, T1)],
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


def test_same_fid_replayed_no_write():
    assert s_same_fid_replayed().run() == []


def test_multi_version_reports_each_file_and_never_sets():
    acts = s_multi_version().run()
    assert sets(acts) == []
    assert [(a.rule, a.reason, a.file_id) for a in acts] == [("R4", "multi_version", 1), ("R4", "multi_version", 2)]


def test_multi_version_unwatched_still_allows_r1():
    assert [(a.kind, a.rule) for a in s_multi_version_unwatched().run()] == [(ActionKind.MARK_PLEX_PLAYED, "R1")]


def test_not_in_snapshot_reports_no_write():
    acts = s_not_in_snapshot().run()
    assert sets(acts) == []
    assert reports(acts) == [("R4", "not_in_snapshot")]


def test_unknown_file_age_blocks_r2b():
    acts = s_unknown_file_age().run()
    assert sets(acts) == []
    assert reports(acts) == [("R2b", "unknown_file_age")]


def test_r2b_play_before_file_created_excluded():
    acts = s_r2b_play_before_created().run()
    assert sets(acts) == []
    assert reports(acts) == [("R2b", "no_playback")]


def test_r2b_play_equal_to_cutoff_excluded():
    acts = s_r2b_play_equals_cutoff().run()
    assert sets(acts) == []
    assert reports(acts) == [("R2b", "no_playback")]


def test_scenarios_cover_all_action_kinds():
    kinds = {a.kind for b in SCENARIOS for a in b().run()}
    assert kinds == set(ActionKind)


# --- C1 (ruling 15): R1-marked items never use lastViewedAt as an AniDB date ---


def test_r1_marked_item_without_history_reports_not_sets():
    sc = simple(items=[item("a", "/p/x.mkv", last=T3)])
    acts = plan(sc.items, {}, PathMatcher(sc.files, []), sc.mylist(), "record", True, r1_marked=frozenset({"a"}))
    assert sets(acts) == []
    assert reports(acts) == [("R4", "plex_marked_by_reconciler")]


def test_r1_marked_item_with_history_uses_history_only():
    sc = simple(items=[item("a", "/p/x.mkv", last=T3)], history={"a": [T2]})
    acts = plan(
        sc.items, sc.history, PathMatcher(sc.files, []), sc.mylist(), "record", True, r1_marked=frozenset({"a"})
    )
    (a,) = sets(acts)
    assert (a.rule, a.when) == ("R2", T2)


# --- I1 (ruling 16): applied SETs not yet in the snapshot count as watched ---


def test_pending_fid_is_treated_as_watched():
    sc = s_new_watch()
    pending = [PendingWatch(file_id=1, fid=12345, eids=(50,), when=T1)]
    assert plan(sc.items, sc.history, PathMatcher(sc.files, []), sc.mylist(), "record", True, pending=pending) == []


def test_pending_eid_gates_new_file_through_r2b():
    # file 2 (fid 777) replaced file 1 for the same episode; file 1's SET (T1) is applied but not in the snapshot yet
    sc = simple(
        items=[item("a", "/p/x2.mkv", last=T2)],
        files=[shoko(2, 777, [50], "/p/x2.mkv")],
        entries=[entry(777, 50, None)],
        history={"a": [T1]},
    )
    pending = [PendingWatch(file_id=1, fid=12345, eids=(50,), when=T1)]
    acts = plan(sc.items, sc.history, PathMatcher(sc.files, []), sc.mylist(), "record", True, pending=pending)
    assert sets(acts) == []
    assert reports(acts) == [("R2b", "no_playback")]
    # a real play after the pending date is a legitimate R2b
    sc.history = {"a": [T1, T3]}
    acts = plan(sc.items, sc.history, PathMatcher(sc.files, []), sc.mylist(), "record", True, pending=pending)
    (a,) = sets(acts)
    assert (a.rule, a.file_id, a.when) == ("R2b", 2, T3)


def test_two_fids_same_episode_one_run_only_one_r2():
    sc = Scenario(
        items=[item("a", "/p/x.mkv", last=T2), item("b", "/p2/y.mkv", section=5, last=T1)],
        files=[shoko(1, 11, [50], "/p/x.mkv"), shoko(2, 22, [50], "/p2/y.mkv")],
        entries=[entry(11, 50, None), entry(22, 50, None)],
        history={"a": [T2], "b": [T1]},
    )
    acts = sc.run()
    assert [(a.rule, a.file_id, a.when) for a in sets(acts)] == [("R2b", 1, T2), ("R2", 2, T1)]
    # without a real play after the first file's date, the second file is not written
    sc.history = {"b": [T1]}
    acts = sc.run()
    assert [(a.rule, a.file_id) for a in sets(acts)] == [("R2", 2)]
    assert reports(acts) == [("R2b", "no_playback")]


# --- R4 eid_mismatch: AniDB (snapshot) and Shoko disagree about a file's episodes ---


def test_eid_mismatch_blocks_r2():
    sc = simple(no_snapshot_row=True, entries=[entry(12345, 51, None)], history={"a": [T1]})
    acts = sc.run()
    assert sets(acts) == []
    assert reports(acts) == [("R4", "eid_mismatch")]
    assert (acts[0].file_id, acts[0].fid) == (1, 12345)


def test_eid_mismatch_blocks_r2b():
    sc = simple(no_snapshot_row=True, entries=[entry(12345, 51, None), entry(999, 50, T1)], history={"a": [T3]})
    acts = sc.run()
    assert sets(acts) == []
    assert reports(acts) == [("R4", "eid_mismatch")]


def test_eid_superset_in_snapshot_is_mismatch():
    sc = simple(entries=[entry(12345, 51, None)], history={"a": [T1]})  # snapshot {50, 51}, Shoko {50}
    assert reports(sc.run()) == [("R4", "eid_mismatch")]


def test_snapshot_without_eid_for_fid_is_not_a_mismatch():
    sc = simple(no_snapshot_row=True, entries=[entry(12345, None, None)], history={"a": [T1]})
    (a,) = sc.run()
    assert (a.kind, a.rule, a.file_id) == (ActionKind.SET_FILE_WATCHED, "R2", 1)


def test_multi_episode_file_with_matching_eids_sets():
    sc = simple(files=[shoko(1, 12345, [50, 51], "/p/x.mkv")], entries=[entry(12345, 51, None)], history={"a": [T1]})
    (a,) = sc.run()
    assert (a.rule, a.eids) == ("R2", (50, 51))


def test_set_dedupe_tie_breaks_on_smallest_rating_key_regardless_of_order():
    files = [ShokoFile(1, 12345, (50,), False, ("/p/x.mkv", "/p2/x.mkv"), T0)]
    a = item("a", "/p/x.mkv", last=T2)
    b = item("b", "/p2/x.mkv", section=5, last=T2)
    for items in ([a, b], [b, a]):
        sc = Scenario(items=items, files=files, entries=[entry(12345, 50, None)], history={"a": [T1], "b": [T1]})
        (s,) = sets(sc.run())
        assert (s.rating_key, s.when) == ("a", T1)


def test_plan_rejects_unknown_r2b_policy():
    with pytest.raises(ValueError, match="r2b_policy"):
        Scenario(**{**s_new_watch().__dict__, "policy": "recrod"}).run()


def test_many_conflicts_plan_quickly():
    """Replanning settles every independent same-episode conflict per pass (not one per pass)."""
    import time

    n = 1500
    items, files, entries, history = [], [], [], {}
    for i in range(n):
        eid = 10_000 + i
        for k, when in ((0, T1), (1, T2)):
            fid, rk, path = 2 * i + k + 1, f"r{i}-{k}", f"/p/{i}/{k}.mkv"
            files.append(shoko(fid, fid, [eid], path))
            entries.append(entry(fid, eid, None))
            items.append(item(rk, path, last=when))
            history[rk] = [when]
    sc = Scenario(items=items, files=files, entries=entries, history=history)
    start = time.perf_counter()
    acts = sc.run()
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"plan took {elapsed:.2f}s"
    # per episode: the earlier file gets R2, the later one an R2b gated on the earlier date
    assert [(a.rule, a.when) for a in sets(acts)] == [("R2", T1), ("R2b", T2)] * n
    assert reports(acts) == []


# --- R2b that relies on an R2 planned in the same run carries that dependency ---


def test_r2b_depends_on_r2_planned_in_same_run():
    sc = Scenario(
        items=[item("a", "/p/x.mkv", last=T2), item("b", "/p2/y.mkv", section=5, last=T1)],
        files=[shoko(1, 11, [50], "/p/x.mkv"), shoko(2, 22, [50], "/p2/y.mkv")],
        entries=[entry(11, 50, None), entry(22, 50, None)],
        history={"a": [T2], "b": [T1]},
    )
    deps = {(a.rule, a.file_id): a.depends_on_file_id for a in sets(sc.run())}
    assert deps == {("R2b", 1): 2, ("R2", 2): None}


def test_r2b_with_snapshot_or_ledger_cutoff_has_no_dependency():
    (a,) = s_r2b_after().run()
    assert (a.rule, a.depends_on_file_id) == ("R2b", None)
    sc = simple(
        items=[item("a", "/p/x2.mkv", last=T2)],
        files=[shoko(2, 777, [50], "/p/x2.mkv")],
        entries=[entry(777, 50, None)],
        history={"a": [T3]},
    )
    pending = [PendingWatch(file_id=1, fid=12345, eids=(50,), when=T1)]
    (a,) = plan(sc.items, sc.history, PathMatcher(sc.files, []), sc.mylist(), "record", True, pending=pending)
    assert (a.rule, a.depends_on_file_id) == ("R2b", None)


def test_r2b_needing_two_planned_r2s_is_reported():
    # A{50} and C{60} both get R2 this run; X{50,60} would need both to land first
    sc = Scenario(
        items=[
            item("a", "/p/a.mkv", last=T1),
            item("c", "/p/c.mkv", last=T1),
            item("d", "/p/d.mkv", last=T3),
            item("x", "/p/x.mkv", last=T2),
        ],
        files=[
            shoko(1, 1, [50], "/p/a.mkv"),
            shoko(3, 3, [60], "/p/c.mkv"),
            shoko(4, 4, [60], "/p/d.mkv"),
            shoko(9, 9, [50, 60], "/p/x.mkv"),
        ],
        entries=[entry(1, 50, None), entry(3, 60, None), entry(4, 60, None), entry(9, 50, None), entry(9, 60, None)],
        history={"a": [T1], "c": [T1], "d": [T3], "x": [T2]},
    )
    acts = sc.run()
    assert [(a.rule, a.file_id, a.depends_on_file_id) for a in sets(acts)] == [
        ("R2", 1, None),
        ("R2", 3, None),
        ("R2b", 4, 3),
    ]
    assert [(a.rule, a.reason, a.file_id) for a in acts if a.kind is ActionKind.REPORT] == [
        ("R2b", "multiple_prerequisites", 9)
    ]


# --- G1: a parent-dir+filename fallback never drives a write ---


def _fallback(views=1, entries=(), history=None):
    """Plex 'Show B' episode whose only Shoko hit is 'Show A' with the same parent dir and filename."""
    return Scenario(
        items=[item("a", "/plex/Show B/Season 01/01.mkv", views=views, last=T2)],
        files=[shoko(1, 12345, [50], "/shoko/Show A/Season 01/01.mkv")],
        entries=list(entries),
        history=history or {},
    )


def test_fallback_match_never_sets_only_reports_candidate():
    acts = _fallback(entries=[entry(12345, 50, None)], history={"a": [T1]}).run()
    assert [(a.kind, a.rule, a.reason, a.file_id) for a in acts] == [(ActionKind.REPORT, "R4", "fallback_match", 1)]


def test_fallback_match_never_marks_plex_played():
    acts = _fallback(views=0, entries=[entry(999, 50, T1)]).run()
    assert [(a.kind, a.rule, a.reason, a.file_id) for a in acts] == [(ActionKind.REPORT, "R4", "fallback_match", 1)]


def test_exact_match_unaffected():
    (a,) = s_new_watch().run()
    assert (a.kind, a.rule, a.file_id) == (ActionKind.SET_FILE_WATCHED, "R2", 1)
    (r1,) = s_reupload().run()
    assert r1.kind is ActionKind.MARK_PLEX_PLAYED

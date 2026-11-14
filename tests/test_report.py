from plex_anidb_reconciler.models import Action, ActionKind, Outcome
from plex_anidb_reconciler.report import prune_reports, summarize


def test_prune_keeps_newest_by_run_id(tmp_path):
    names = [f"2026010{d}T000000Z-abc{d}.csv" for d in range(1, 6)]
    for n in names:
        (tmp_path / n).write_text("x")
    (tmp_path / "notes.txt").write_text("keep")
    prune_reports(tmp_path, keep=2)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*names[-2:], "notes.txt"])


def test_prune_missing_dir_is_noop(tmp_path):
    prune_reports(tmp_path / "nope", keep=2)


def _act(kind, rule, reason):
    return Action(kind=kind, rule=rule, rating_key="rk", file_id=None, fid=None, eids=(), when=None, reason=reason)


def test_summary_splits_reports_by_reason_and_writes_by_status():
    rep = ActionKind.REPORT
    rows = [
        Outcome(_act(rep, "R4", "no_shoko_match"), "reported"),
        Outcome(_act(rep, "R4", "no_shoko_match"), "reported"),
        Outcome(_act(rep, "R4", "fallback_match"), "reported"),
        Outcome(_act(rep, "R2b", "no_playback"), "reported"),
        Outcome(_act(ActionKind.SET_FILE_WATCHED, "R2", "new_watch"), "dry_run"),
        Outcome(_act(ActionKind.SET_FILE_WATCHED, "R2", "new_watch"), "capped"),
        Outcome(_act(ActionKind.MARK_PLEX_PLAYED, "R1", "anidb_episode_watched"), "dry_run"),
    ]
    assert summarize(rows).splitlines() == [
        "R1 dry_run: 1",
        "R2 capped: 1",
        "R2 dry_run: 1",
        "R2b reported no_playback: 1",
        "R4 reported fallback_match: 1",
        "R4 reported no_shoko_match: 2",
    ]


def test_summary_empty():
    assert summarize([]) == "no actions"

from plex_anidb_reconciler.report import prune_reports


def test_prune_keeps_newest_by_run_id(tmp_path):
    names = [f"2026010{d}T000000Z-abc{d}.csv" for d in range(1, 6)]
    for n in names:
        (tmp_path / n).write_text("x")
    (tmp_path / "notes.txt").write_text("keep")
    prune_reports(tmp_path, keep=2)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*names[-2:], "notes.txt"])


def test_prune_missing_dir_is_noop(tmp_path):
    prune_reports(tmp_path / "nope", keep=2)

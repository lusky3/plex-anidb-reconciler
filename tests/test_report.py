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


def test_prune_keeps_newest_csv_and_html_separately(tmp_path):
    stems = [f"2026010{d}T000000Z-abc{d}" for d in range(1, 5)]
    for stem in stems:
        (tmp_path / f"{stem}.csv").write_text("x")
        (tmp_path / f"{stem}.html").write_text("x")
    prune_reports(tmp_path, keep=2)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        f"{stem}.{ext}" for stem in stems[-2:] for ext in ("csv", "html")
    )


def test_html_report_escapes_every_cell_and_shows_summary(tmp_path):
    from datetime import UTC, datetime

    from plex_anidb_reconciler.models import MyListFileState
    from plex_anidb_reconciler.report import write_html_report

    evil = Action(
        kind=ActionKind.SET_FILE_WATCHED,
        rule="R2",
        rating_key="<rk>&",
        file_id=3,
        fid=33,
        eids=(52,),
        when=datetime(2026, 1, 1, tzinfo=UTC),
        reason="new_watch",
        release_group='<script>alert("g")</script>',
    )
    outcomes = [
        Outcome(evil, "failed", "<img src=x onerror=alert(1)>"),
        Outcome(_act(ActionKind.REPORT, "R4", "no_shoko_match"), "reported"),
    ]
    path = tmp_path / "reports" / "run.html"
    write_html_report(outcomes, path, lambda fid: MyListFileState(4, None, 9) if fid == 33 else None)
    page = path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "<script" not in page and "<img" not in page and "<rk>" not in page
    assert "&lt;script&gt;alert(&quot;g&quot;)&lt;/script&gt;" in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "&lt;rk&gt;&amp;" in page
    assert page.index("R2 failed: 1") < page.index("<table")  # summary counts above the table
    assert "R4 reported no_shoko_match: 1" in page
    assert "http" not in page.replace("http-equiv", "")  # no external resources
    header = page[page.index("<thead>") : page.index("</thead>")]
    for column in ("rule", "status", "aid", "release_group", "anidb_state", "anidb_viewdate"):
        assert f"<th>{column}</th>" in header
    assert page.count("<tr>") == 3  # header + 2 rows


def test_html_report_empty(tmp_path):
    from plex_anidb_reconciler.report import write_html_report

    path = tmp_path / "run.html"
    write_html_report([], path)
    assert "no actions" in path.read_text(encoding="utf-8")


def test_prune_failures_logged_at_debug(tmp_path, monkeypatch, caplog):
    from pathlib import Path

    for d in range(1, 4):
        (tmp_path / f"2026010{d}T000000Z-a.csv").write_text("x")

    def refuse(self, missing_ok=False):
        raise PermissionError(f"secret path {self}")

    monkeypatch.setattr(Path, "unlink", refuse)
    with caplog.at_level("DEBUG", logger="plex_anidb_reconciler.report"):
        prune_reports(tmp_path, keep=1)
    assert caplog.text.count("old report not removed (PermissionError)") == 2
    assert "secret path" not in caplog.text
    assert len(list(tmp_path.iterdir())) == 3

import csv
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from plex_anidb_reconciler import cli
from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.models import Action, ActionKind
from plex_anidb_reconciler.runner import build_clients, run_once


def _csv_dicts(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def _csv_rows(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def _query(db, sql):
    """Run a read-only query against the ledger and close the connection."""
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql).fetchall()


SHOKO = "http://shoko:8111"
NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)  # fixed run clock; the snapshot mtime is set relative to it


def set_mtime(path, when):
    os.utime(path, (when.timestamp(), when.timestamp()))


def make_stale(env_):
    set_mtime(env_["MYLIST_PATH"], NOW - timedelta(hours=48))


PLEX = "http://plex:32400"


def shoko_file(i, fid, eid):
    return {
        "ID": i,
        "AniDB": {"ID": fid, "ReleaseGroup": {"Name": f"Group {i}", "ShortName": f"G{i}"}},
        "Created": "2025-01-01T00:00:00+00:00",
        "Locations": [{"AbsolutePath": f"/p/{i}.mkv"}],
        "SeriesIDs": [{"SeriesID": {"ID": 1, "AniDB": 700 + i}, "EpisodeIDs": [{"AniDB": eid, "Source": "AniDB"}]}],
    }


def plex_item(rk, i, views, last=None):
    m = {"ratingKey": rk, "viewCount": views, "Media": [{"Part": [{"file": f"/p/{i}.mkv"}]}]}
    if last:
        m["lastViewedAt"] = last
    return m


def snap(fid, eid, viewed):
    return {"FileID": fid, "EpisodeID": eid, "AnimeID": 1, "State": 1, "ViewedAt": viewed}


@pytest.fixture
def env(tmp_path):
    mylist = tmp_path / "mylist.json"
    # eids 50, 51 watched elsewhere (R1 for plex rk a, b); fid 3 in snapshot unwatched (R2 for rk c)
    mylist.write_text(
        json.dumps(
            [
                snap(999, 50, "2023-03-01T21:00:00-05:00"),
                snap(998, 51, "2023-03-01T21:00:00-05:00"),
                snap(3, 52, None),
            ]
        )
    )
    set_mtime(mylist, NOW)
    return {
        "SHOKO_URL": SHOKO,
        "SHOKO_API_KEY": "SHOKOSECRET",
        "PLEX_URL": PLEX,
        "PLEX_TOKEN": "PLEXSECRET",
        "PLEX_SECTIONS": "4",
        "MYLIST_PATH": str(mylist),
        "DATA_DIR": str(tmp_path / "data"),
    }


def mock_servers(*, shoko_status=200):
    index = respx.get(f"{SHOKO}/api/v3/File").mock(
        return_value=httpx.Response(
            200, json={"List": [shoko_file(1, 1, 50), shoko_file(2, 2, 51), shoko_file(3, 3, 52)]}
        )
    )
    respx.get(f"{PLEX}/library/sections/4/all").mock(
        return_value=httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [plex_item("a", 1, 0), plex_item("b", 2, 0), plex_item("c", 3, 1, 1682971200)]
                }
            },
        )
    )
    respx.get(f"{PLEX}/status/sessions/history/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": []}})
    )
    scrobble = respx.get(f"{PLEX}/:/scrobble").mock(return_value=httpx.Response(200))
    put = respx.put(url__regex=rf"{SHOKO}/api/v3/File/\d+/UserStats").mock(return_value=httpx.Response(shoko_status))
    mock_file_detail([shoko_file(1, 1, 50), shoko_file(2, 2, 51), shoko_file(3, 3, 52)])
    mock_user_stats({})
    mock_servers.index = index
    return scrobble, put


def mock_file_detail(files):
    """GET /api/v3/File/{id}: the pre-write re-check reads the current Shoko state of one file."""
    by_id = {f["ID"]: f for f in files}

    def detail(request, file_id):
        f = by_id.get(int(file_id))
        return httpx.Response(200, json=f) if f else httpx.Response(404)

    return respx.get(url__regex=rf"{SHOKO}/api/v3/File/(?P<file_id>\d+)(\?.*)?$").mock(side_effect=detail)


def mock_user_stats(stats_by_id):
    """GET /api/v3/File/{id}/UserStats: Shoko's current watched state of one file."""

    def handler(request, file_id):
        body = stats_by_id.get(int(file_id), {"WatchedCount": 0, "LastWatchedAt": None, "ResumePosition": None})
        return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)

    return respx.get(url__regex=rf"{SHOKO}/api/v3/File/(?P<file_id>\d+)/UserStats$").mock(side_effect=handler)


def mock_world(files, items, history=()):
    """Shoko index + per-file detail, one Plex section, owner history rows (rk, epoch)."""
    respx.get(f"{SHOKO}/api/v3/File").mock(return_value=httpx.Response(200, json={"List": files}))
    detail = mock_file_detail(files)
    mock_user_stats({})
    respx.get(f"{PLEX}/library/sections/4/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": items}})
    )
    rows = [{"ratingKey": rk, "viewedAt": t} for rk, t in history]
    respx.get(f"{PLEX}/status/sessions/history/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": rows}})
    )
    scrobble = respx.get(f"{PLEX}/:/scrobble").mock(return_value=httpx.Response(200))
    put = respx.put(url__regex=rf"{SHOKO}/api/v3/File/\d+/UserStats").mock(return_value=httpx.Response(200))
    return scrobble, put, detail


def clients(settings):
    return build_clients(settings, httpx.Client())


def go(env_, *, apply, limit=None, now=None, **kw):
    s = Settings.from_env(env_)
    return run_once(s, apply=apply, limit=limit, now=now or NOW, clients=clients(s), **kw)


@respx.mock
def test_dry_run_makes_no_write_calls(env):
    scrobble, put = mock_servers()
    res = go(env, apply=False)
    assert scrobble.call_count == 0 and put.call_count == 0
    assert res.counts == {"dry_run": 3}
    assert res.report_path.exists()


@respx.mock
def test_ledger_r1_once_per_rating_key(env):
    scrobble, put = mock_servers()
    first = go(env, apply=True)
    assert first.counts == {"applied": 3}
    assert scrobble.call_count == 2 and put.call_count == 1
    # same items still unwatched in Plex on the next run: ledger blocks every R1; the applied SET's
    # fid counts as watched (pending) so no SET is even planned
    second = go(env, apply=True)
    assert second.counts == {"skipped_ledger": 2}
    assert scrobble.call_count == 2 and put.call_count == 1


@respx.mock
def test_apply_respects_limit_and_cap(env):
    scrobble, put = mock_servers()
    res = go(env, apply=True, limit=2)
    assert res.counts == {"applied": 2, "capped": 1}
    assert scrobble.call_count == 2 and put.call_count == 0  # R1 first, SET capped
    env["MAX_WRITES"] = "1"
    res = go(env, apply=True, limit=10)  # cap = min(limit, MAX_WRITES); rk a,b already applied
    assert res.counts == {"skipped_ledger": 2, "applied": 1}
    assert put.call_count == 1


@respx.mock
def test_failed_write_recorded_and_exit_code_2(env, monkeypatch, capsys):
    _, put = mock_servers(shoko_status=500)
    res = go(env, apply=True)
    assert res.counts == {"applied": 2, "failed": 1}
    rows = list(_csv_dicts(res.report_path))
    assert [r["status"] for r in rows if r["kind"] == "set_file_watched"] == ["failed"]
    # failed does not count as seen: it is retried
    put.mock(return_value=httpx.Response(200))
    assert go(env, apply=True).counts == {"skipped_ledger": 2, "applied": 1}

    mock_servers(shoko_status=500)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATA_DIR", env["DATA_DIR"] + "2")
    # the CLI passes the wall clock; pin it so the snapshot (mtime NOW) is fresh
    monkeypatch.setattr(cli, "run_once", lambda s, **kw: run_once(s, clients=clients(s), **{**kw, "now": NOW}))
    assert cli.main(["apply"]) == 2
    assert "SHOKOSECRET" not in capsys.readouterr().out


@respx.mock
def test_snapshot_unavailable_no_actions(env, capsys):
    scrobble, put = mock_servers()
    os.remove(env["MYLIST_PATH"])
    res = go(env, apply=True)
    assert res.counts == {"snapshot_unavailable": 1}
    assert "snapshot_unavailable" in capsys.readouterr().out
    assert scrobble.call_count == 0 and put.call_count == 0
    assert len(_csv_rows(res.report_path)) == 1  # header only


@respx.mock
def test_stale_snapshot_defers_r2_but_not_r1(env):
    scrobble, put = mock_servers()
    make_stale(env)
    res = go(env, apply=True)
    assert scrobble.call_count == 2 and put.call_count == 0
    assert res.counts == {"applied": 2, "reported": 1}


@respx.mock
def test_report_csv_columns(env):
    mock_servers()
    res = go(env, apply=False)
    rows = list(_csv_dicts(res.report_path))
    assert list(rows[0]) == [
        "rule",
        "kind",
        "status",
        "rating_key",
        "file_id",
        "fid",
        "eids",
        "when",
        "reason",
        "detail",
        "aid",
        "release_group",
        "anidb_state",
        "anidb_viewdate",
    ]
    assert res.report_path == __import__("pathlib").Path(env["DATA_DIR"]) / "reports" / f"{res.run_id}.csv"
    r2 = next(r for r in rows if r["rule"] == "R2")
    assert (r2["file_id"], r2["fid"], r2["eids"], r2["status"]) == ("3", "3", "52", "dry_run")
    assert r2["when"] == "2023-05-01T20:00:00+00:00"
    assert (r2["aid"], r2["release_group"], r2["anidb_state"], r2["anidb_viewdate"]) == ("703", "G3", "1", "")
    r1 = next(r for r in rows if r["rule"] == "R1")
    assert (r1["aid"], r1["release_group"], r1["anidb_state"], r1["anidb_viewdate"]) == ("", "", "", "")


@respx.mock
def test_report_shows_anidb_state_and_viewdate_of_the_actions_fid(env):
    """eid_mismatch report for fid 3: the snapshot has it watched, with a different episode."""
    write_mylist(env, [snap(3, 99, "2023-03-01T21:00:00-05:00")])
    mock_world([shoko_file(3, 3, 52)], [plex_item("c", 3, 1, 1682971200)])
    rows = _csv_dicts(go(env, apply=False).report_path)
    assert [(r["reason"], r["aid"], r["release_group"], r["anidb_state"], r["anidb_viewdate"]) for r in rows] == [
        ("eid_mismatch", "703", "G3", "1", "2023-03-02T02:00:00+00:00")
    ]


@respx.mock
def test_shoko_index_cached_and_ttl(env):
    mock_servers()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    route = mock_servers.index
    go(env, apply=False, now=now)
    go(env, apply=False, now=now.replace(minute=30))
    assert route.call_count == 1
    cache = json.loads((__import__("pathlib").Path(env["DATA_DIR"]) / "shoko_index.json").read_text())
    assert cache["files"][0]["created_at"] == "2025-01-01T00:00:00+00:00"
    go(env, apply=False, now=now.replace(hour=14))
    assert route.call_count == 2


def test_config_error_exit_1(monkeypatch, capsys):
    for k in ("SHOKO_URL", "SHOKO_API_KEY", "PLEX_URL", "PLEX_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["plan"]) == 1
    assert "SHOKO_URL" in capsys.readouterr().err


def write_mylist(env_, rows):
    with open(env_["MYLIST_PATH"], "w") as fh:
        json.dump(rows, fh)
    set_mtime(env_["MYLIST_PATH"], NOW)


R1_TIME = datetime(2026, 3, 1, 12, tzinfo=UTC)


def test_reconciler_scrobble_never_becomes_anidb_date(env):
    """Run 1 marks the Plex item played via R1 (the episode was watched through another release); later
    that release's MyList row disappears. The item's Plex lastViewedAt is now the reconciler's own scrobble
    time, not a real watch of this file: it must not be written to AniDB."""
    env["SHOKO_INDEX_TTL_MIN"] = "0"
    files = [shoko_file(1, 1, 50)]
    write_mylist(env, [snap(999, 50, "2023-03-01T21:00:00-05:00"), snap(1, 50, None)])
    with respx.mock:
        scrobble, put, _ = mock_world(files, [plex_item("a", 1, 0)])
        assert go(env, apply=True, now=R1_TIME).counts == {"applied": 1}
        assert scrobble.call_count == 1
    write_mylist(env, [snap(1, 50, None)])
    with respx.mock:
        _, put, _ = mock_world(files, [plex_item("a", 1, 1, int(R1_TIME.timestamp()))])
        res = go(env, apply=True, now=R1_TIME + timedelta(days=30))
    assert put.call_count == 0
    rows = list(_csv_dicts(res.report_path))
    assert [(r["rule"], r["reason"], r["status"]) for r in rows] == [("R4", "plex_marked_by_reconciler", "reported")]


def test_applied_set_gates_upgraded_file_until_snapshot_catches_up(env):
    """Run 1 writes fid 1 (eid 50) via R2. The file is then upgraded to fid 2 before the
    snapshot shows the write; the old play date must not be copied to the new file."""
    env["SHOKO_INDEX_TTL_MIN"] = "0"
    play = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
    write_mylist(env, [snap(1, 50, None), snap(2, 50, None)])
    with respx.mock:
        _, put, _ = mock_world([shoko_file(1, 1, 50)], [plex_item("a", 1, 1, play)], [("a", play)])
        assert go(env, apply=True).counts == {"applied": 1}
        assert put.calls.last.request.url.path == "/api/v3/File/1/UserStats"
    with respx.mock:
        _, put, _ = mock_world([shoko_file(2, 2, 50)], [plex_item("a", 2, 1, play)], [("a", play)])
        res = go(env, apply=True)
    assert put.call_count == 0
    rows = list(_csv_dicts(res.report_path))
    assert [(r["rule"], r["reason"], r["file_id"]) for r in rows] == [("R2b", "no_playback", "2")]


@pytest.mark.parametrize(
    "current",
    [
        {**shoko_file(3, 4, 52)},  # fid changed
        {**shoko_file(3, 3, 53)},  # eids changed
        {**shoko_file(3, 3, 52), "SeriesIDs": [{"EpisodeIDs": [{"AniDB": 52, "Source": "User"}]}]},  # manual link
        {**shoko_file(3, 3, 52), "AniDB": None},  # no longer recognised
        {**shoko_file(3, 3, 52), "Locations": [{"AbsolutePath": "/p/other.mkv"}]},  # path now another file's
        {**shoko_file(3, 3, 52), "Locations": []},  # no paths returned
    ],
    ids=["fid", "eids", "manual_link", "no_fid", "path_moved", "no_paths"],
)
@respx.mock
def test_shoko_changed_since_index_blocks_write(env, current):
    _, put = mock_servers()
    mock_file_detail([shoko_file(1, 1, 50), shoko_file(2, 2, 51), current])
    res = go(env, apply=True)
    assert put.call_count == 0
    assert res.counts["failed"] == 1 and res.counts["shoko_changed"] == 1
    rows = list(_csv_dicts(res.report_path))
    assert [r["status"] for r in rows if r["kind"] == "set_file_watched"] == ["failed"]


@respx.mock
def test_recheck_reads_current_file_before_write(env):
    _, put = mock_servers()
    res = go(env, apply=True)
    assert put.call_count == 1 and res.counts == {"applied": 3}
    get = next(c.request for c in respx.calls if c.request.url.path == "/api/v3/File/3")
    assert get.url.params["includeDataFrom"] == "AniDB"


# --- --rules/--file-id filters: a canary can reach R2 directly ---


@respx.mock
def test_rules_filter_lets_canary_reach_r2(env):
    scrobble, put = mock_servers()
    res = go(env, apply=True, limit=1, rules=frozenset({"R2"}))
    assert scrobble.call_count == 0 and put.call_count == 1
    assert res.counts == {"applied": 1}


@respx.mock
def test_file_id_filter_and_reports_still_listed(env):
    scrobble, put = mock_servers()
    make_stale(env)  # stale: the R2 becomes a report
    res = go(env, apply=True, file_id=3)
    assert scrobble.call_count == 0 and put.call_count == 0
    assert res.counts == {"reported": 1}
    assert res.snapshot == "stale"


@respx.mock
def test_cli_rules_and_file_id_flags(env, monkeypatch):
    seen = {}

    def fake(s, **kw):
        seen.update(kw)
        return __import__("plex_anidb_reconciler.runner", fromlist=["RunResult"]).RunResult("x", {}, Path("r"))

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli, "run_once", fake)
    assert cli.main(["apply", "--rules", "R2,R2b", "--limit", "1", "--file-id", "7"]) == 0
    assert (seen["rules"], seen["file_id"], seen["limit"], seen["apply"]) == (frozenset({"R2", "R2b"}), 7, 1, True)
    assert cli.main(["plan", "--rules", "R1"]) == 0
    assert (seen["rules"], seen["file_id"], seen["apply"]) == (frozenset({"R1"}), None, False)
    with pytest.raises(SystemExit):
        cli.main(["apply", "--rules", "R4"])


# --- the ledger only holds real writes; dry runs are uncapped; old reports are pruned ---


@respx.mock
def test_ledger_holds_only_writes_and_dry_run_is_uncapped(env):
    mock_servers()
    env["MAX_WRITES"] = "1"
    make_stale(env)  # one REPORT row
    res = go(env, apply=False)
    assert res.counts == {"dry_run": 2, "reported": 1}
    db = Path(env["DATA_DIR"]) / "ledger.sqlite"
    assert _query(db, "SELECT COUNT(*) FROM actions")[0] == (0,)
    go(env, apply=True)
    assert _query(db, "SELECT status FROM actions") == [("applied",)]


@respx.mock
def test_reports_pruned_to_newest(env, monkeypatch):
    from plex_anidb_reconciler import runner

    mock_servers()
    monkeypatch.setattr(runner, "KEEP_REPORTS", 2)
    paths = [go(env, apply=False, now=datetime(2026, 1, d, tzinfo=UTC)).report_path for d in (1, 2, 3)]
    assert sorted(p.name for p in paths[0].parent.glob("*.csv")) == sorted(p.name for p in paths[1:])


# --- CLI hardening ---


def test_cli_unexpected_exception_exit_2_type_only(env, monkeypatch, capsys):
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def boom(s, **kw):
        raise RuntimeError("http://plex:32400?X-Plex-Token=PLEXSECRET")

    monkeypatch.setattr(cli, "run_once", boom)
    assert cli.main(["plan"]) == 2
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "PLEXSECRET" not in err


# --- R2b that depends on an R2 planned in the same run ---

T_EARLY = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
T_LATE = int(datetime(2026, 2, 2, tzinfo=UTC).timestamp())


def dep_world(env_, *, detail_files=None):
    """file 2 (rk b) gets R2 at T_EARLY; file 1 (rk a, same episode) an R2b at T_LATE that depends on it."""
    write_mylist(env_, [snap(11, 50, None), snap(22, 50, None)])
    files = [shoko_file(1, 11, 50), shoko_file(2, 22, 50)]
    _, put, _ = mock_world(
        files, [plex_item("a", 1, 1, T_LATE), plex_item("b", 2, 1, T_EARLY)], [("a", T_LATE), ("b", T_EARLY)]
    )
    if detail_files is not None:
        mock_file_detail(detail_files)
    return put


def set_rows(res):
    return [
        (r["rule"], r["file_id"], r["status"], r["detail"])
        for r in _csv_dicts(res.report_path)
        if r["kind"] == "set_file_watched"
    ]


def put_paths(put):
    return [c.request.url.path for c in put.calls]


@respx.mock
def test_r2_applied_before_dependent_r2b(env):
    put = dep_world(env)
    res = go(env, apply=True)
    assert put_paths(put) == ["/api/v3/File/2/UserStats", "/api/v3/File/1/UserStats"]
    assert res.counts == {"applied": 2}


@respx.mock
def test_cap_never_lets_dependent_r2b_run_first(env):
    put = dep_world(env)
    res = go(env, apply=True, limit=1)
    assert put_paths(put) == ["/api/v3/File/2/UserStats"]
    assert set_rows(res) == [("R2", "2", "applied", ""), ("R2b", "1", "capped", "")]


@respx.mock
def test_dependent_r2b_skipped_when_prerequisite_capped(env):
    env["MAX_WRITES"] = "1"
    put = dep_world(env)
    respx.get(f"{PLEX}/library/sections/4/all").mock(
        return_value=httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [plex_item("a", 1, 1, T_LATE), plex_item("b", 2, 1, T_EARLY), plex_item("c", 3, 0)]
                }
            },
        )
    )
    # rk c (file 3, eid 51) is an R1: it runs first and takes the only write
    respx.get(f"{SHOKO}/api/v3/File").mock(
        return_value=httpx.Response(
            200, json={"List": [shoko_file(1, 11, 50), shoko_file(2, 22, 50), shoko_file(3, 33, 51)]}
        )
    )
    write_mylist(env, [snap(11, 50, None), snap(22, 50, None), snap(999, 51, "2023-03-01T21:00:00-05:00")])
    res = go(env, apply=True)
    assert put.call_count == 0
    assert set_rows(res) == [("R2", "2", "capped", ""), ("R2b", "1", "skipped", "prerequisite_not_applied")]


@respx.mock
def test_rules_r2b_alone_skips_dependent(env):
    put = dep_world(env)
    res = go(env, apply=True, rules=frozenset({"R2b"}))
    assert put.call_count == 0
    assert set_rows(res) == [("R2b", "1", "skipped", "prerequisite_not_applied")]
    db = Path(env["DATA_DIR"]) / "ledger.sqlite"
    assert _query(db, "SELECT COUNT(*) FROM actions")[0] == (0,)


@respx.mock
def test_file_id_filter_skips_dependent_r2b(env):
    put = dep_world(env)
    res = go(env, apply=True, file_id=1)
    assert put.call_count == 0
    assert set_rows(res) == [("R2b", "1", "skipped", "prerequisite_not_applied")]


@respx.mock
def test_dependent_r2b_skipped_when_prerequisite_shoko_changed(env):
    put = dep_world(env, detail_files=[shoko_file(1, 11, 50), shoko_file(2, 23, 50)])
    res = go(env, apply=True)
    assert put.call_count == 0
    assert set_rows(res) == [
        ("R2", "2", "failed", "shoko_changed"),
        ("R2b", "1", "skipped", "prerequisite_not_applied"),
    ]


@respx.mock
def test_dependent_r2b_dry_run_follows_prerequisite(env):
    dep_world(env)
    assert set_rows(go(env, apply=False)) == [("R2", "2", "dry_run", ""), ("R2b", "1", "dry_run", "")]
    assert set_rows(go(env, apply=False, rules=frozenset({"R2b"}))) == [
        ("R2b", "1", "skipped", "prerequisite_not_applied")
    ]


@respx.mock
def test_shoko_index_cache_tied_to_shoko_url(env):
    mock_servers()
    route = mock_servers.index
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    cache_path = Path(env["DATA_DIR"]) / "shoko_index.json"
    go(env, apply=False, now=now)
    cache = json.loads(cache_path.read_text())
    assert cache["shoko_url"] == SHOKO
    cache["shoko_url"] = "http://other-shoko:8111"
    cache_path.write_text(json.dumps(cache))
    go(env, apply=False, now=now.replace(minute=10))
    assert route.call_count == 2  # different server: refetched
    cache = json.loads(cache_path.read_text())
    del cache["shoko_url"]  # cache from an older version
    cache_path.write_text(json.dumps(cache))
    go(env, apply=False, now=now.replace(minute=20))
    assert route.call_count == 3
    go(env, apply=False, now=now.replace(minute=30))
    assert route.call_count == 3  # same server, within TTL: cached


@pytest.mark.parametrize("raw", ["-1", "x"])
def test_cli_rejects_negative_limit(env, monkeypatch, raw, capsys):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit) as ei:
        cli.main(["apply", "--limit", raw])
    assert ei.value.code == 2
    assert "--limit" in capsys.readouterr().err


def test_cli_accepts_zero_limit(env, monkeypatch):
    seen = {}

    def fake(s, **kw):
        seen.update(kw)
        return __import__("plex_anidb_reconciler.runner", fromlist=["RunResult"]).RunResult("x", {}, Path("r"))

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli, "run_once", fake)
    assert cli.main(["apply", "--limit", "0"]) == 0
    assert seen["limit"] == 0


# --- secrets never reach stdout or stderr ---

SECRETS = ("SHOKOSECRET", "PLEXSECRET")


def assert_no_secrets(captured):
    for secret in SECRETS:
        assert secret not in captured.out and secret not in captured.err


def test_config_error_output_has_no_secrets(env, monkeypatch, capsys):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MAX_WRITES", "PLEXSECRET")
    assert cli.main(["plan"]) == 1
    captured = capsys.readouterr()
    assert "MAX_WRITES" in captured.err
    assert_no_secrets(captured)


@pytest.mark.parametrize(
    "exc",
    [
        lambda: __import__("plex_anidb_reconciler.shoko", fromlist=["x"]).ShokoError(
            "Shoko GET /api/v3/File returned HTTP 500"
        ),
        lambda: __import__("plex_anidb_reconciler.plex", fromlist=["x"]).PlexError(
            "Plex GET /:/scrobble failed: ConnectError"
        ),
        lambda: RuntimeError("apikey=SHOKOSECRET X-Plex-Token=PLEXSECRET"),
    ],
    ids=["shoko", "plex", "unexpected"],
)
def test_run_failure_output_has_no_secrets(env, monkeypatch, capsys, exc):
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def boom(s, **kw):
        raise exc()

    monkeypatch.setattr(cli, "run_once", boom)
    assert cli.main(["apply"]) == 2
    captured = capsys.readouterr()
    assert "run failed" in captured.err
    assert_no_secrets(captured)


@respx.mock
def test_real_client_failures_have_no_secrets(env, monkeypatch, capsys):
    """End to end through the real clients: transport error carrying the secrets in its text."""
    respx.get(f"{SHOKO}/api/v3/File").mock(side_effect=httpx.ConnectError("SHOKOSECRET PLEXSECRET"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli, "run_once", lambda s, **kw: run_once(s, clients=clients(s), **kw))
    assert cli.main(["plan"]) == 2
    captured = capsys.readouterr()
    assert "ConnectError" in captured.err
    assert_no_secrets(captured)


@respx.mock
def test_stop_event_stops_at_write_boundary_and_records_rest_as_skipped(env):
    import threading

    scrobble, put = mock_servers()
    stop = threading.Event()

    def first_write_sets_stop(request):
        stop.set()  # SIGTERM arrives while the first write is in flight
        return httpx.Response(200)

    scrobble.mock(side_effect=first_write_sets_stop)
    res = go(env, apply=True, stop=stop)
    assert res.counts == {"applied": 1, "skipped": 2}
    assert scrobble.call_count == 1 and put.call_count == 0  # in-flight write completed, nothing after it
    rows = list(_csv_dicts(res.report_path))
    assert [(r["status"], r["detail"]) for r in rows] == [
        ("applied", ""),
        ("skipped", "shutdown"),
        ("skipped", "shutdown"),
    ]
    # next run (no stop) picks up the remainder
    res2 = go(env, apply=True)
    assert res2.counts == {"skipped_ledger": 1, "applied": 2}


@respx.mock
def test_stop_event_unset_changes_nothing(env):
    import threading

    mock_servers()
    assert go(env, apply=True, stop=threading.Event()).counts == {"applied": 3}


@respx.mock
def test_future_snapshot_mtime_is_stale(env):
    mock_servers()
    set_mtime(env["MYLIST_PATH"], NOW + timedelta(hours=2))
    res = go(env, apply=True)
    assert res.snapshot == "stale"


# --- programming errors are raised, not recorded as failed writes ---


@pytest.mark.parametrize(
    "action",
    [
        {"kind": ActionKind.MARK_PLEX_PLAYED, "rule": "R1", "rating_key": None},
        {"kind": ActionKind.SET_FILE_WATCHED, "rule": "R2", "rating_key": "a", "file_id": None, "when": R1_TIME},
        {"kind": ActionKind.SET_FILE_WATCHED, "rule": "R2", "rating_key": "a", "file_id": 1, "when": None},
        {"kind": ActionKind.REPORT, "rule": "R4", "rating_key": "a"},
    ],
    ids=["r1_no_rating_key", "set_no_file_id", "set_no_date", "report"],
)
def test_execute_rejects_invalid_action(action):
    from plex_anidb_reconciler.runner import _execute

    full = {"file_id": None, "fid": None, "eids": (), "when": None, "reason": "x", **action}
    with pytest.raises(ValueError):
        _execute(Action(**full), clients=None)


@respx.mock
def test_corrupt_shoko_index_cache_logged_at_debug(env, caplog):
    mock_servers()
    cache = Path(env["DATA_DIR"]) / "shoko_index.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json SHOKOSECRET")
    with caplog.at_level("DEBUG", logger="plex_anidb_reconciler.runner"):
        go(env, apply=False)
    assert "shoko index cache not used (JSONDecodeError)" in caplog.text
    assert "SHOKOSECRET" not in caplog.text
    assert mock_servers.index.call_count == 1


# --- never overwrite a date Shoko already has (GET UserStats before every SET) ---


@pytest.mark.parametrize(
    "stats",
    [
        {"WatchedCount": 1, "LastWatchedAt": "2024-01-01T10:00:00-05:00", "ResumePosition": None},
        {"WatchedCount": 0, "LastWatchedAt": "2024-01-01T10:00:00-05:00", "ResumePosition": None},
        {"WatchedCount": 3, "LastWatchedAt": None, "ResumePosition": None},
    ],
    ids=["count_and_date", "date_only", "count_only"],
)
@respx.mock
def test_shoko_already_watched_is_never_overwritten(env, stats):
    _, put = mock_servers()
    mock_user_stats({3: stats})
    res = go(env, apply=True)
    assert put.call_count == 0
    assert res.counts == {"applied": 2, "skipped": 1}
    assert set_rows(res) == [("R2", "3", "skipped", "shoko_already_watched")]
    db = Path(env["DATA_DIR"]) / "ledger.sqlite"
    assert _query(db, "SELECT kind FROM actions ORDER BY id") == [("mark_plex_played",), ("mark_plex_played",)]


@respx.mock
def test_shoko_unwatched_is_written_after_reading_user_stats(env):
    _, put = mock_servers()
    stats = mock_user_stats({})
    res = go(env, apply=True)
    assert res.counts == {"applied": 3}
    assert put.call_count == 1 and stats.call_count == 1
    paths = [c.request.url.path for c in respx.calls if "/api/v3/File/3" in c.request.url.path]
    assert paths[-2:] == ["/api/v3/File/3/UserStats", "/api/v3/File/3/UserStats"]
    assert [c.request.method for c in respx.calls if c.request.url.path == "/api/v3/File/3/UserStats"] == [
        "GET",
        "PUT",
    ]


@respx.mock
def test_no_user_stats_yet_404_is_written(env):
    """Shoko 5.3.3: a never-watched file has no UserStats entry and the GET answers 404."""
    _, put = mock_servers()
    mock_user_stats({3: httpx.Response(404, text="No FileUserStats entry for the given fileID for the current user")})
    res = go(env, apply=True)
    assert res.counts == {"applied": 3}
    assert put.call_count == 1


@respx.mock
def test_user_stats_file_missing_404_fails_without_write(env):
    _, put = mock_servers()
    mock_user_stats({3: httpx.Response(404, text="No File entry for the given fileID")})
    res = go(env, apply=True)
    assert put.call_count == 0
    assert res.counts == {"applied": 2, "failed": 1}
    assert set_rows(res) == [("R2", "3", "failed", "Shoko GET /api/v3/File/3/UserStats returned HTTP 404")]


@respx.mock
def test_malformed_user_stats_fails_without_write(env):
    _, put = mock_servers()
    mock_user_stats({3: httpx.Response(200, json={"WatchedCount": "lots"})})
    res = go(env, apply=True)
    assert put.call_count == 0
    assert res.counts == {"applied": 2, "failed": 1}
    rows = set_rows(res)
    assert rows[0][:3] == ("R2", "3", "failed") and "unexpected body" in rows[0][3]


@respx.mock
def test_dry_run_does_not_read_user_stats(env):
    mock_servers()
    stats = mock_user_stats({})
    go(env, apply=False)
    assert stats.call_count == 0


@respx.mock
def test_cached_index_keeps_aid_and_group_and_reads_older_caches(env):
    mock_servers()
    go(env, apply=False, now=NOW)
    rows = _csv_dicts(go(env, apply=False, now=NOW + timedelta(minutes=1)).report_path)  # from the cache
    assert mock_servers.index.call_count == 1
    assert {(r["aid"], r["release_group"]) for r in rows if r["rule"] == "R2"} == {("703", "G3")}
    cache_path = Path(env["DATA_DIR"]) / "shoko_index.json"
    cache = json.loads(cache_path.read_text())
    for f in cache["files"]:
        del f["aid"], f["release_group"]  # cache written by an older version
    cache_path.write_text(json.dumps(cache))
    rows = _csv_dicts(go(env, apply=False, now=NOW + timedelta(minutes=2)).report_path)
    assert mock_servers.index.call_count == 1
    assert {(r["aid"], r["release_group"]) for r in rows if r["rule"] == "R2"} == {("1", "")}  # aid from snapshot


# --- ledger before/after and HTML report ---


def ledger_audit(env_):
    rows = _query(
        Path(env_["DATA_DIR"]) / "ledger.sqlite", "SELECT rule, status, before, after FROM actions ORDER BY id"
    )
    return [(rule, status, json.loads(b) if b else None, json.loads(a) if a else None) for rule, status, b, a in rows]


@respx.mock
def test_ledger_records_before_and_after(env):
    mock_servers()
    mock_user_stats({3: {"WatchedCount": 0, "LastWatchedAt": None, "ResumePosition": None}})
    go(env, apply=True)
    r1_before = {
        "anidb_state": None,
        "anidb_viewdate": None,
        "plex_view_count": 0,
        "shoko_last_watched_at": None,
        "shoko_watched_count": None,
    }
    assert ledger_audit(env) == [
        ("R1", "applied", r1_before, {"plex_played": True}),
        ("R1", "applied", r1_before, {"plex_played": True}),
        (
            "R2",
            "applied",
            {
                "anidb_state": 1,
                "anidb_viewdate": None,
                "plex_view_count": 1,
                "shoko_last_watched_at": None,
                "shoko_watched_count": 0,
            },
            {"shoko_last_watched_at": "2023-05-01T20:00:00+00:00"},
        ),
    ]


@respx.mock
def test_failed_write_has_before_but_no_after(env):
    mock_servers(shoko_status=500)
    go(env, apply=True, rules=frozenset({"R2"}))
    ((rule, status, before, after),) = ledger_audit(env)
    assert (rule, status, after) == ("R2", "failed", None)
    assert before["shoko_watched_count"] == 0 and before["plex_view_count"] == 1


@respx.mock
def test_html_report_written_next_to_csv(env):
    mock_servers()
    res = go(env, apply=False)
    html_path = res.report_path.with_suffix(".html")
    page = html_path.read_text(encoding="utf-8")
    assert "R2 dry_run: 1" in page and "<td>703</td>" in page


@respx.mock
def test_html_reports_pruned_with_csvs(env, monkeypatch):
    from plex_anidb_reconciler import runner

    mock_servers()
    monkeypatch.setattr(runner, "KEEP_REPORTS", 2)
    paths = [go(env, apply=False, now=datetime(2026, 1, d, tzinfo=UTC)).report_path for d in (1, 2, 3)]
    kept = sorted(p.name for p in paths[0].parent.iterdir())
    assert kept == sorted(f"{p.stem}.{ext}" for p in paths[1:] for ext in ("csv", "html"))


@respx.mock
def test_snapshot_unavailable_still_writes_html(env):
    mock_servers()
    os.remove(env["MYLIST_PATH"])
    res = go(env, apply=True)
    assert "no actions" in res.report_path.with_suffix(".html").read_text(encoding="utf-8")


# --- triggered runs (/trigger with rating keys): plan everything, act only on those items ---


def decisions(res, rating_key=None):
    """(rule, kind, file_id, when, reason, status) of the CSV rows, optionally of one Plex item only."""
    return [
        (r["rule"], r["kind"], r["file_id"], r["when"], r["reason"], r["status"], r["detail"])
        for r in _csv_dicts(res.report_path)
        if rating_key is None or r["rating_key"] == rating_key
    ]


@respx.mock
def test_triggered_run_acts_only_on_those_rating_keys(env):
    scrobble, put = mock_servers()
    res = go(env, apply=True, rating_keys=frozenset({"c"}))
    assert res.counts == {"applied": 1}
    assert scrobble.call_count == 0 and put.call_count == 1
    assert {r["rating_key"] for r in _csv_dicts(res.report_path)} == {"c"}  # reports listed for c only


@respx.mock
def test_triggered_run_unknown_key_does_nothing_and_none_is_full(env):
    mock_servers()
    assert go(env, apply=False, rating_keys=frozenset({"zzz"})).counts == {}
    assert go(env, apply=False, rating_keys=None).counts == {"dry_run": 3}


@respx.mock
def test_triggered_run_keeps_every_gate(env):
    """The snapshot freshness gate applies to a triggered run too."""
    _, put = mock_servers()
    make_stale(env)
    res = go(env, apply=True, rating_keys=frozenset({"c"}))
    assert put.call_count == 0 and res.counts == {"reported": 1}


def two_items_one_file(env_):
    """Plex items a (played T_LATE) and b (played T_EARLY) are both the Shoko file 1."""
    write_mylist(env_, [snap(11, 50, None)])
    return mock_world(
        [shoko_file(1, 11, 50)],
        [plex_item("a", 1, 1, T_LATE), plex_item("b", 1, 1, T_EARLY)],
        [("a", T_LATE), ("b", T_EARLY)],
    )


@respx.mock
def test_triggered_run_uses_the_full_runs_earliest_date_for_a_shared_file(env):
    _, put, _ = two_items_one_file(env)
    full = [r for r in decisions(go(env, apply=False)) if r[1] == "set_file_watched"]
    assert full == [("R2", "set_file_watched", "1", "2026-02-01T00:00:00+00:00", "new_watch", "dry_run", "")]
    # the later-played item a is triggered: the one merged SET for file 1 is executed with b's earlier date
    res = go(env, apply=True, rating_keys=frozenset({"a"}))
    assert res.counts == {"applied": 1}
    assert json.loads(put.calls.last.request.content)["LastWatchedAt"] == "2026-02-01T00:00:00+00:00"
    rows = [r for r in decisions(res) if r[1] == "set_file_watched"]
    assert rows == [("R2", "set_file_watched", "1", "2026-02-01T00:00:00+00:00", "new_watch", "applied", "")]


@respx.mock
def test_triggered_run_decides_exactly_like_the_full_run(env):
    """Two files of one episode: file 2 (item b, earlier play) gets R2, file 1 (item a) an R2b that depends
    on it. A run triggered for one item makes the full run's decisions for that item."""
    dep_world(env)
    full = go(env, apply=False)
    for key in ("a", "b"):
        triggered = go(env, apply=False, rating_keys=frozenset({key}))
        expected = [
            # an R2b whose prerequisite R2 belongs to an item outside the trigger cannot run this time
            (*row[:5], "skipped", "prerequisite_not_applied") if row[0] == "R2b" and key == "a" else row
            for row in decisions(full, key)
        ]
        assert decisions(triggered) == expected
    assert decisions(full, "b")[0][:2] == ("R2", "set_file_watched")
    assert decisions(full, "a")[0][:2] == ("R2b", "set_file_watched")


@respx.mock
def test_triggered_dependent_r2b_skipped_until_its_prerequisite_is_applied(env):
    put = dep_world(env)
    res = go(env, apply=True, rating_keys=frozenset({"a"}))
    assert put.call_count == 0
    assert set_rows(res) == [("R2b", "1", "skipped", "prerequisite_not_applied")]
    res = go(env, apply=True, rating_keys=frozenset({"a", "b"}))
    assert put_paths(put) == ["/api/v3/File/2/UserStats", "/api/v3/File/1/UserStats"]
    assert set_rows(res) == [("R2", "2", "applied", ""), ("R2b", "1", "applied", "")]


# --- snapshot unavailable: reported with its reason, CLI exit 3 ---


@pytest.mark.parametrize(
    "breakage,problem",
    [
        ("missing", "cannot read snapshot"),
        ("not_json", "invalid JSON"),
        ("bad_entry", "malformed entr"),
        ("directory", "cannot read snapshot"),
    ],
)
@respx.mock
def test_snapshot_unavailable_names_path_and_problem_not_content(env, capsys, caplog, breakage, problem):
    scrobble, put = mock_servers()
    path = Path(env["MYLIST_PATH"])
    if breakage == "missing":
        path.unlink()
    elif breakage == "not_json":
        path.write_text("CONTENTCANARY {", encoding="utf-8")
    elif breakage == "bad_entry":
        path.write_text(json.dumps([snap(1, 50, "CONTENTCANARY")]), encoding="utf-8")
    else:
        path.unlink()
        path.mkdir()
    with caplog.at_level("WARNING"):
        res = go(env, apply=True)
    assert res.snapshot == "unavailable"
    assert str(path) in res.detail and problem in res.detail
    out = capsys.readouterr().out
    for text in (out, caplog.text):
        assert str(path) in text and problem in text
        assert "CONTENTCANARY" not in text
    assert scrobble.call_count == 0 and put.call_count == 0


@pytest.mark.parametrize("command", ["plan", "apply"])
def test_cli_snapshot_unavailable_exits_3_with_reason(env, monkeypatch, capsys, command):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    os.remove(env["MYLIST_PATH"])
    monkeypatch.setattr(cli, "run_once", lambda s, **kw: run_once(s, clients=clients(s), **kw))
    assert cli.main([command]) == 3
    err = capsys.readouterr().err
    assert "snapshot unavailable" in err and env["MYLIST_PATH"] in err and "cannot read snapshot" in err


# --- HTTP timeouts and shutdown: an in-flight write fits inside the default shutdown grace ---


def _timeout(request):
    return request.extensions["timeout"]


def test_write_path_timeout_fits_inside_default_shutdown_grace():
    from plex_anidb_reconciler.config import WRITE_PATH_TIMEOUT

    t = WRITE_PATH_TIMEOUT
    worst = t.connect + t.write + t.read  # one request: connect, send the body, wait for the reply
    assert worst <= Settings.from_env({**ENV_MIN}).shutdown_grace_s
    assert t.pool is not None and t.pool <= t.connect


ENV_MIN = {"SHOKO_URL": SHOKO, "SHOKO_API_KEY": "k", "PLEX_URL": PLEX, "PLEX_TOKEN": "t", "PLEX_SECTIONS": "4"}


@respx.mock
def test_write_path_requests_use_the_short_timeout(env):
    from plex_anidb_reconciler.config import PLAN_TIMEOUT, WRITE_PATH_TIMEOUT

    scrobble, put = mock_servers()
    stats = mock_user_stats({})
    detail = mock_file_detail([shoko_file(1, 1, 50), shoko_file(2, 2, 51), shoko_file(3, 3, 52)])
    s = Settings.from_env(env)
    run_once(s, apply=True, limit=None, now=NOW)  # real clients from _open_clients
    write_path = [scrobble.calls[0].request, put.calls[0].request, stats.calls[0].request, detail.calls[0].request]
    assert all(_timeout(r) == WRITE_PATH_TIMEOUT.as_dict() for r in write_path)
    assert _timeout(mock_servers.index.calls[0].request) == PLAN_TIMEOUT.as_dict()


@respx.mock
def test_stop_during_pre_write_reads_skips_the_put(env):
    import threading

    from plex_anidb_reconciler.models import Rule

    _, put = mock_servers()
    stop = threading.Event()
    files = {f["ID"]: f for f in (shoko_file(1, 1, 50), shoko_file(2, 2, 51), shoko_file(3, 3, 52))}

    def detail(request, file_id):
        stop.set()  # SIGTERM arrives while the re-read before the write is in flight
        return httpx.Response(200, json=files[int(file_id)])

    respx.get(url__regex=rf"{SHOKO}/api/v3/File/(?P<file_id>\d+)(\?.*)?$").mock(side_effect=detail)
    stats = mock_user_stats({})
    res = go(env, apply=True, stop=stop, rules=frozenset({Rule.R2}))
    assert put.call_count == 0 and stats.call_count == 0
    assert set_rows(res) == [("R2", "3", "skipped", "shutdown")]
    assert _query(Path(env["DATA_DIR"]) / "ledger.sqlite", "SELECT COUNT(*) FROM actions") == [(0,)]

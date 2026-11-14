import csv
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from plex_anidb_reconciler import cli
from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.runner import build_clients, run_once

SHOKO = "http://shoko:8111"
PLEX = "http://plex:32400"


def shoko_file(i, fid, eid):
    return {
        "ID": i,
        "AniDB": {"ID": fid},
        "Created": "2025-01-01T00:00:00+00:00",
        "Locations": [{"AbsolutePath": f"/p/{i}.mkv"}],
        "SeriesIDs": [{"EpisodeIDs": [{"AniDB": eid, "Source": "AniDB"}]}],
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
    mock_servers.index = index
    return scrobble, put


def mock_file_detail(files):
    """GET /api/v3/File/{id}: the pre-write re-check reads the current Shoko state of one file."""
    by_id = {f["ID"]: f for f in files}

    def detail(request, file_id):
        f = by_id.get(int(file_id))
        return httpx.Response(200, json=f) if f else httpx.Response(404)

    return respx.get(url__regex=rf"{SHOKO}/api/v3/File/(?P<file_id>\d+)(\?.*)?$").mock(side_effect=detail)


def mock_world(files, items, history=()):
    """Shoko index + per-file detail, one Plex section, owner history rows (rk, epoch)."""
    respx.get(f"{SHOKO}/api/v3/File").mock(return_value=httpx.Response(200, json={"List": files}))
    detail = mock_file_detail(files)
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
    return run_once(s, apply=apply, limit=limit, now=now or datetime.now(UTC), clients=clients(s), **kw)


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
    rows = list(csv.DictReader(res.report_path.open()))
    assert [r["status"] for r in rows if r["kind"] == "set_file_watched"] == ["failed"]
    # failed does not count as seen: it is retried
    put.mock(return_value=httpx.Response(200))
    assert go(env, apply=True).counts == {"skipped_ledger": 2, "applied": 1}

    mock_servers(shoko_status=500)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATA_DIR", env["DATA_DIR"] + "2")
    monkeypatch.setattr(cli, "run_once", lambda s, **kw: run_once(s, clients=clients(s), **kw))
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
    assert len(list(csv.reader(res.report_path.open()))) == 1  # header only


@respx.mock
def test_stale_snapshot_defers_r2_but_not_r1(env):
    scrobble, put = mock_servers()
    old = time.time() - 48 * 3600
    os.utime(env["MYLIST_PATH"], (old, old))
    res = go(env, apply=True)
    assert scrobble.call_count == 2 and put.call_count == 0
    assert res.counts == {"applied": 2, "reported": 1}


@respx.mock
def test_report_csv_columns(env):
    mock_servers()
    res = go(env, apply=False)
    rows = list(csv.DictReader(res.report_path.open()))
    assert list(rows[0]) == ["rule", "kind", "status", "rating_key", "file_id", "fid", "eids", "when", "reason"]
    assert res.report_path == __import__("pathlib").Path(env["DATA_DIR"]) / "reports" / f"{res.run_id}.csv"
    r2 = next(r for r in rows if r["rule"] == "R2")
    assert (r2["file_id"], r2["fid"], r2["eids"], r2["status"]) == ("3", "3", "52", "dry_run")
    assert r2["when"] == "2023-05-01T20:00:00+00:00"


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


R1_TIME = datetime(2026, 3, 1, 12, tzinfo=UTC)


def test_c1_reconciler_scrobble_never_becomes_anidb_date(env):
    """Ruling 15: run 1 marks Plex played via R1; later the other release's MyList row disappears.
    Plex lastViewedAt is the reconciler's own scrobble time: it must not be written to AniDB."""
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
    rows = list(csv.DictReader(res.report_path.open()))
    assert [(r["rule"], r["reason"], r["status"]) for r in rows] == [("R4", "plex_marked_by_reconciler", "reported")]


def test_i1_applied_set_gates_upgraded_file_until_snapshot_catches_up(env):
    """Ruling 16: run 1 writes fid 1 (eid 50) via R2. The file is then upgraded to fid 2 before the
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
    rows = list(csv.DictReader(res.report_path.open()))
    assert [(r["rule"], r["reason"], r["file_id"]) for r in rows] == [("R2b", "no_playback", "2")]


@pytest.mark.parametrize(
    "current",
    [
        {**shoko_file(3, 4, 52)},  # fid changed
        {**shoko_file(3, 3, 53)},  # eids changed
        {**shoko_file(3, 3, 52), "SeriesIDs": [{"EpisodeIDs": [{"AniDB": 52, "Source": "User"}]}]},  # manual link
        {**shoko_file(3, 3, 52), "AniDB": None},  # no longer recognised
    ],
    ids=["fid", "eids", "manual_link", "no_fid"],
)
@respx.mock
def test_m2_shoko_changed_since_index_blocks_write(env, current):
    _, put = mock_servers()
    mock_file_detail([shoko_file(1, 1, 50), shoko_file(2, 2, 51), current])
    res = go(env, apply=True)
    assert put.call_count == 0
    assert res.counts["failed"] == 1 and res.counts["shoko_changed"] == 1
    rows = list(csv.DictReader(res.report_path.open()))
    assert [r["status"] for r in rows if r["kind"] == "set_file_watched"] == ["failed"]


@respx.mock
def test_m2_recheck_reads_current_file_before_write(env):
    _, put = mock_servers()
    res = go(env, apply=True)
    assert put.call_count == 1 and res.counts == {"applied": 3}
    get = next(c.request for c in respx.calls if c.request.url.path == "/api/v3/File/3")
    assert get.url.params["includeDataFrom"] == "AniDB"


# --- I2 (ruling 18): canary filters ---


@respx.mock
def test_rules_filter_lets_canary_reach_r2(env):
    scrobble, put = mock_servers()
    res = go(env, apply=True, limit=1, rules=frozenset({"R2"}))
    assert scrobble.call_count == 0 and put.call_count == 1
    assert res.counts == {"applied": 1}


@respx.mock
def test_file_id_filter_and_reports_still_listed(env):
    scrobble, put = mock_servers()
    old = time.time() - 48 * 3600
    os.utime(env["MYLIST_PATH"], (old, old))  # stale: the R2 becomes a report
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


# --- I3 (ruling 17): ledger only holds writes; dry run uncapped; reports pruned ---


@respx.mock
def test_ledger_holds_only_writes_and_dry_run_is_uncapped(env):
    mock_servers()
    env["MAX_WRITES"] = "1"
    old = time.time() - 48 * 3600
    os.utime(env["MYLIST_PATH"], (old, old))  # one REPORT row
    res = go(env, apply=False)
    assert res.counts == {"dry_run": 2, "reported": 1}
    db = Path(env["DATA_DIR"]) / "ledger.sqlite"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM actions").fetchone() == (0,)
    go(env, apply=True)
    assert sqlite3.connect(db).execute("SELECT status FROM actions").fetchall() == [("applied",)]


@respx.mock
def test_reports_pruned_to_newest(env, monkeypatch):
    from plex_anidb_reconciler import runner

    mock_servers()
    monkeypatch.setattr(runner, "KEEP_REPORTS", 2)
    paths = [go(env, apply=False, now=datetime(2026, 1, d, tzinfo=UTC)).report_path for d in (1, 2, 3)]
    assert sorted(p.name for p in paths[0].parent.iterdir()) == sorted(p.name for p in paths[1:])


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

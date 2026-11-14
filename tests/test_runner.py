import csv
import json
import os
import time
from datetime import UTC, datetime

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
    mock_servers.index = index
    return scrobble, put


def clients(settings):
    return build_clients(settings, httpx.Client())


def go(env_, *, apply, limit=None, now=None):
    s = Settings.from_env(env_)
    return run_once(s, apply=apply, limit=limit, now=now or datetime.now(UTC), clients=clients(s))


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
    # same items still unwatched in Plex on the next run: ledger blocks every write
    second = go(env, apply=True)
    assert second.counts == {"skipped_ledger": 3}
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

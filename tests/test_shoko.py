import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from plex_anidb_reconciler.shoko import ShokoClient, ShokoError, to_shoko_time

FIXTURE = Path(__file__).parent / "fixtures" / "shoko_files.json"
NY = ZoneInfo("America/New_York")
BASE = "http://shoko:8111"


def make_client():
    return ShokoClient(BASE, "SECRETKEY", NY, httpx.Client())


@respx.mock
def test_list_files_parses_recognised_and_manual():
    respx.get(f"{BASE}/api/v3/File").mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text())))
    files = make_client().list_files()
    assert [f.file_id for f in files] == [1, 2, 3]
    assert (files[0].fid, files[0].eids, files[0].manual_link) == (12345, (50,), False)
    assert files[0].abs_paths == ("/mnt/media/TV/tv_anime/A/Season 01/x.mkv",)
    assert (files[1].fid, files[1].eids, files[1].manual_link) == (None, (70,), True)
    assert files[2].eids == (60, 61)


@respx.mock
def test_list_files_accepts_bare_list():
    data = json.loads(FIXTURE.read_text())["List"]
    respx.get(f"{BASE}/api/v3/File").mock(return_value=httpx.Response(200, json=data))
    assert len(make_client().list_files()) == 3


@respx.mock
def test_list_files_sends_apikey_header_and_query():
    route = respx.get(f"{BASE}/api/v3/File").mock(return_value=httpx.Response(200, json={"Total": 0, "List": []}))
    make_client().list_files()
    req = route.calls.last.request
    assert req.headers["apikey"] == "SECRETKEY"
    assert req.url.params["pageSize"] == "0"
    assert req.url.params["include"] == "XRefs,AbsolutePaths"
    assert req.url.params["includeDataFrom"] == "AniDB"


@respx.mock
def test_set_watched_body():
    route = respx.put(f"{BASE}/api/v3/File/7/UserStats").mock(return_value=httpx.Response(200, json={}))
    make_client().set_watched(7, datetime(2023, 3, 2, 2, 0, 0, 999, tzinfo=UTC))
    req = route.calls.last.request
    assert req.headers["apikey"] == "SECRETKEY"
    assert json.loads(req.content) == {
        "WatchedCount": 1,
        "LastWatchedAt": "2023-03-01T21:00:00-05:00",
        "ResumePosition": None,
        "LastUpdatedAt": "2023-03-01T21:00:00-05:00",
    }


@respx.mock
def test_set_watched_error_raises():
    respx.put(f"{BASE}/api/v3/File/7/UserStats").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(ShokoError) as exc:
        make_client().set_watched(7, datetime(2023, 3, 2, tzinfo=UTC))
    assert "SECRETKEY" not in str(exc.value)


@respx.mock
def test_list_files_error_raises():
    respx.get(f"{BASE}/api/v3/File").mock(return_value=httpx.Response(401))
    with pytest.raises(ShokoError):
        make_client().list_files()


def test_to_shoko_time_rejects_naive():
    with pytest.raises(ValueError):
        to_shoko_time(datetime(2023, 1, 1), NY)


@respx.mock
@pytest.mark.parametrize(
    "created,expected",
    [
        ("2023-05-01T20:00:00-04:00", datetime(2023, 5, 2, 0, 0, tzinfo=UTC)),
        ("2023-05-01T20:00:00", datetime(2023, 5, 2, 0, 0, tzinfo=UTC)),  # naive -> America/New_York
        (None, None),
    ],
)
def test_created_parsing(created, expected):
    raw = {"ID": 9, "AniDB": {"ID": 1}, "Locations": []}
    if created is not None:
        raw["Created"] = created
    respx.get(f"{BASE}/api/v3/File").mock(return_value=httpx.Response(200, json={"List": [raw]}))
    (f,) = make_client().list_files()
    assert f.created_at == expected
    if expected is not None:
        assert f.created_at.tzinfo is not None


@respx.mock
def test_created_missing_key_is_none():
    respx.get(f"{BASE}/api/v3/File").mock(return_value=httpx.Response(200, json={"List": [{"ID": 9}]}))
    assert make_client().list_files()[0].created_at is None

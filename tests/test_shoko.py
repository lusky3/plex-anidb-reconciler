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


@respx.mock
def test_get_file_parses_single_file_and_sends_includes():
    raw = json.loads(FIXTURE.read_text())["List"][0]
    route = respx.get(f"{BASE}/api/v3/File/1").mock(return_value=httpx.Response(200, json=raw))
    f = make_client().get_file(1)
    assert (f.file_id, f.fid, f.eids, f.manual_link) == (1, 12345, (50,), False)
    req = route.calls.last.request
    assert req.headers["apikey"] == "SECRETKEY"
    assert req.url.params["includeDataFrom"] == "AniDB"
    assert req.url.params["include"] == "XRefs,AbsolutePaths"


@respx.mock
def test_get_file_error_raises():
    respx.get(f"{BASE}/api/v3/File/9").mock(return_value=httpx.Response(404))
    with pytest.raises(ShokoError):
        make_client().get_file(9)


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"Total": 1}),  # missing List
        httpx.Response(200, json={"List": [{"AniDB": {"ID": 1}}]}),  # item without ID
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"List": ["not-an-object"]}),
        httpx.Response(200, json={"List": [{"ID": 1, "SeriesIDs": [{"EpisodeIDs": "bad"}]}]}),
        httpx.Response(200, json=42),
    ],
    ids=["missing_list", "no_id", "non_json", "non_object_item", "bad_xrefs", "scalar"],
)
def test_list_files_malformed_body_raises_shoko_error(response):
    respx.get(f"{BASE}/api/v3/File").mock(return_value=response)
    with pytest.raises(ShokoError, match="unexpected body") as exc:
        make_client().list_files()
    assert "SECRETKEY" not in str(exc.value)


@respx.mock
def test_get_file_non_json_raises_shoko_error():
    respx.get(f"{BASE}/api/v3/File/9").mock(return_value=httpx.Response(200, text="nope"))
    with pytest.raises(ShokoError, match="unexpected body"):
        make_client().get_file(9)


@respx.mock
def test_transport_error_hides_details():
    respx.get(f"{BASE}/api/v3/File").mock(side_effect=httpx.ConnectError("boom SECRETKEY http://shoko"))
    with pytest.raises(ShokoError) as exc:
        make_client().list_files()
    assert "SECRETKEY" not in str(exc.value)
    assert "ConnectError" in str(exc.value)


def test_to_shoko_time_summer_uses_edt_offset():
    assert to_shoko_time(datetime(2023, 7, 4, 16, 30, 15, 500, tzinfo=UTC), NY) == "2023-07-04T12:30:15-04:00"


@pytest.mark.parametrize(
    "utc,expected",
    [
        # spring forward 2023-03-12: 02:00 EST -> 03:00 EDT
        (datetime(2023, 3, 12, 6, 59, 59, tzinfo=UTC), "2023-03-12T01:59:59-05:00"),
        (datetime(2023, 3, 12, 7, 0, 0, tzinfo=UTC), "2023-03-12T03:00:00-04:00"),
        # fall back 2023-11-05: 01:xx occurs twice; the offset disambiguates
        (datetime(2023, 11, 5, 5, 30, tzinfo=UTC), "2023-11-05T01:30:00-04:00"),
        (datetime(2023, 11, 5, 6, 30, tzinfo=UTC), "2023-11-05T01:30:00-05:00"),
    ],
)
def test_to_shoko_time_across_dst_transitions(utc, expected):
    stamp = to_shoko_time(utc, NY)
    assert stamp == expected
    assert datetime.fromisoformat(stamp) == utc  # same instant


# --- read watched state (never-overwrite guard) ---


@respx.mock
@pytest.mark.parametrize(
    "body,count,last",
    [
        (
            {"WatchedCount": 2, "LastWatchedAt": "2023-03-01T21:00:00-05:00", "ResumePosition": None},
            2,
            datetime(2023, 3, 2, 2, tzinfo=UTC),
        ),
        ({"WatchedCount": 0, "LastWatchedAt": None, "ResumePosition": None}, 0, None),
        ({"WatchedCount": 0, "LastWatchedAt": "2023-03-01T21:00:00"}, 0, datetime(2023, 3, 2, 2, tzinfo=UTC)),
    ],
    ids=["watched", "unwatched", "naive_date_is_shoko_tz"],
)
def test_get_user_stats_parses(body, count, last):
    route = respx.get(f"{BASE}/api/v3/File/7/UserStats").mock(return_value=httpx.Response(200, json=body))
    stats = make_client().get_user_stats(7)
    assert (stats.watched_count, stats.last_watched_at) == (count, last)
    assert stats.watched is (count > 0 or last is not None)
    assert route.calls.last.request.headers["apikey"] == "SECRETKEY"


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="nope"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"LastWatchedAt": None}),
        httpx.Response(200, json={"WatchedCount": "1", "LastWatchedAt": None}),
        httpx.Response(200, json={"WatchedCount": True, "LastWatchedAt": None}),
        httpx.Response(200, json={"WatchedCount": -1, "LastWatchedAt": None}),
        httpx.Response(200, json={"WatchedCount": 0}),
        httpx.Response(200, json={"WatchedCount": 0, "LastWatchedAt": "yesterday"}),
        httpx.Response(200, json={"WatchedCount": 0, "LastWatchedAt": 5}),
    ],
    ids=["non_json", "list", "no_count", "str_count", "bool_count", "neg_count", "no_date", "bad_date", "int_date"],
)
def test_get_user_stats_malformed_raises_shoko_error(response):
    respx.get(f"{BASE}/api/v3/File/7/UserStats").mock(return_value=response)
    with pytest.raises(ShokoError, match="unexpected body") as exc:
        make_client().get_user_stats(7)
    assert "SECRETKEY" not in str(exc.value)


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, text="No FileUserStats entry for the given fileID for the current user"),
        httpx.Response(404, json="No FileUserStats entry for the given fileID for the current user"),
        httpx.Response(
            404,
            json={"title": "Not Found", "status": 404, "detail": "No FileUserStats entry for the given fileID"},
            headers={"Content-Type": "application/problem+json"},
        ),
        httpx.Response(404, text="no fileuserstats ENTRY"),
    ],
    ids=["plain_text", "json_string", "problem_details", "other_case"],
)
def test_get_user_stats_missing_stats_404_is_unwatched(response):
    """Shoko 5.3.3 answers 404 'No FileUserStats entry ...' for a file nobody has watched yet."""
    respx.get(f"{BASE}/api/v3/File/7/UserStats").mock(return_value=response)
    stats = make_client().get_user_stats(7)
    assert (stats.watched_count, stats.last_watched_at, stats.watched) == (0, None, False)


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, text="No File entry for the given fileID"),
        httpx.Response(404),
        httpx.Response(500, text="No FileUserStats entry"),
        httpx.Response(401),
    ],
    ids=["file_missing", "empty_404", "server_error", "unauthorised"],
)
def test_get_user_stats_other_errors_raise(response):
    respx.get(f"{BASE}/api/v3/File/7/UserStats").mock(return_value=response)
    with pytest.raises(ShokoError) as exc:
        make_client().get_user_stats(7)
    assert f"HTTP {response.status_code}" in str(exc.value) and "SECRETKEY" not in str(exc.value)


@respx.mock
def test_get_user_stats_transport_error_raises():
    respx.get(f"{BASE}/api/v3/File/7/UserStats").mock(side_effect=httpx.ConnectError("SECRETKEY"))
    with pytest.raises(ShokoError) as exc:
        make_client().get_user_stats(7)
    assert "SECRETKEY" not in str(exc.value)


@respx.mock
def test_list_files_parses_aid_and_release_group():
    respx.get(f"{BASE}/api/v3/File").mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text())))
    files = make_client().list_files()
    assert [(f.aid, f.release_group) for f in files] == [(10, "SG"), (11, None), (12, "Long Name Only")]


@respx.mock
def test_missing_series_and_group_are_none():
    respx.get(f"{BASE}/api/v3/File").mock(
        return_value=httpx.Response(
            200, json={"List": [{"ID": 9, "AniDB": {"ID": 1, "ReleaseGroup": None}, "SeriesIDs": [{"EpisodeIDs": []}]}]}
        )
    )
    (f,) = make_client().list_files()
    assert (f.aid, f.release_group) == (None, None)


@respx.mock
@pytest.mark.parametrize(
    "series_id,aid",
    [
        ("not-a-dict", None),
        ([1, 2], None),
        ({"AniDB": True}, None),
        ({"AniDB": "abc"}, None),
        ({"AniDB": 1.5}, None),
        ({"AniDB": None}, None),
        ({"AniDB": "42"}, 42),
        ({"AniDB": 42}, 42),
    ],
    ids=["str", "list", "bool", "non_numeric", "float", "null", "numeric_str", "int"],
)
def test_report_aid_parsed_defensively(series_id, aid):
    raw = {"ID": 9, "AniDB": {"ID": 1}, "SeriesIDs": [{"SeriesID": series_id, "EpisodeIDs": [{"AniDB": 50}]}]}
    respx.get(f"{BASE}/api/v3/File").mock(return_value=httpx.Response(200, json={"List": [raw]}))
    (f,) = make_client().list_files()
    assert (f.aid, f.eids, f.fid) == (aid, (50,), 1)

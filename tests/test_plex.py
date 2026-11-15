import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from plex_anidb_reconciler.plex import PlexClient, PlexError

FIX = Path(__file__).parent / "fixtures"
BASE = "http://plex:32400"
TOKEN = "SECRETTOKEN"


def make_client():
    return PlexClient(BASE, TOKEN, httpx.Client())


def load(name):
    return json.loads((FIX / name).read_text())


@respx.mock
def test_list_episodes_parses_parts_and_unwatched():
    route = respx.get(f"{BASE}/library/sections/4/all").mock(
        return_value=httpx.Response(200, json=load("plex_episodes.json"))
    )
    items = make_client().list_episodes(4)
    assert [i.rating_key for i in items] == ["100", "101", "102"]
    assert items[0].paths == ("/plex/a/e1.mkv", "/plex/a/e1b.mkv")
    assert items[0].view_count == 2
    assert items[0].last_viewed_at == datetime.fromtimestamp(1682971200, UTC)
    assert items[0].last_viewed_at.tzinfo is not None
    assert (items[1].view_count, items[1].last_viewed_at, items[2].paths) == (0, None, ())
    assert items[0].section == 4
    req = route.calls[0].request
    assert req.url.params["type"] == "4"
    assert req.headers["X-Plex-Token"] == TOKEN
    assert req.headers["Accept"] == "application/json"
    assert req.headers["X-Plex-Container-Start"] == "0"
    assert req.headers["X-Plex-Container-Size"] == "500"
    assert TOKEN not in str(req.url)


@respx.mock
def test_list_episodes_paginates():
    def page(start, n):
        return {
            "MediaContainer": {
                "size": n,
                "totalSize": 501,
                "Metadata": [{"ratingKey": str(start + i)} for i in range(n)],
            }
        }

    route = respx.get(f"{BASE}/library/sections/5/all").mock(
        side_effect=[httpx.Response(200, json=page(0, 500)), httpx.Response(200, json=page(500, 1))]
    )
    items = make_client().list_episodes(5)
    assert len(items) == 501
    assert [c.request.headers["X-Plex-Container-Start"] for c in route.calls] == ["0", "500"]


@respx.mock
def test_history_groups_by_rating_key():
    route = respx.get(f"{BASE}/status/sessions/history/all").mock(
        return_value=httpx.Response(200, json=load("plex_history.json"))
    )
    hist = make_client().history(4)
    assert hist == {
        "100": [datetime.fromtimestamp(1682971200, UTC), datetime.fromtimestamp(1682971300, UTC)],
        "101": [datetime.fromtimestamp(1682980000, UTC)],
    }
    params = route.calls[0].request.url.params
    assert (params["librarySectionID"], params["accountID"]) == ("4", "1")


@respx.mock
def test_mark_played_request():
    route = respx.get(f"{BASE}/:/scrobble").mock(return_value=httpx.Response(200))
    make_client().mark_played("100")
    req = route.calls[0].request
    assert req.url.params["key"] == "100"
    assert req.url.params["identifier"] == "com.plexapp.plugins.library"
    assert req.headers["X-Plex-Token"] == TOKEN
    assert TOKEN not in str(req.url)


@respx.mock
def test_token_never_in_exception_message():
    respx.get(f"{BASE}/library/sections/4/all").mock(return_value=httpx.Response(500))
    with pytest.raises(PlexError) as ei:
        make_client().list_episodes(4)
    assert TOKEN not in str(ei.value)
    assert "500" in str(ei.value)


@respx.mock
def test_transport_error_hides_details():
    respx.get(f"{BASE}/:/scrobble").mock(side_effect=httpx.ConnectError("boom " + TOKEN))
    with pytest.raises(PlexError) as ei:
        make_client().mark_played("1")
    assert TOKEN not in str(ei.value)
    assert str(ei.value) == "Plex GET /:/scrobble failed: ConnectError"


@respx.mock
def test_paged_stops_on_empty_page_without_total():
    full = {"MediaContainer": {"Metadata": [{"ratingKey": str(i)} for i in range(500)]}}
    route = respx.get(f"{BASE}/library/sections/5/all").mock(
        side_effect=[httpx.Response(200, json=full), httpx.Response(200, json={"MediaContainer": {}})]
    )
    assert len(make_client().list_episodes(5)) == 500
    assert route.call_count == 2


@respx.mock
def test_paged_raises_when_server_ignores_offset():
    full = {"MediaContainer": {"Metadata": [{"ratingKey": str(i)} for i in range(500)]}}
    route = respx.get(f"{BASE}/library/sections/5/all").mock(return_value=httpx.Response(200, json=full))
    with pytest.raises(PlexError, match="pagination"):
        make_client().list_episodes(5)
    assert route.call_count == 2


@respx.mock
def test_paged_hard_page_cap(monkeypatch):
    from plex_anidb_reconciler import plex

    monkeypatch.setattr(plex, "MAX_PAGES", 3)
    counter = iter(range(10**6))

    def page(request):
        base = next(counter) * 500
        return httpx.Response(
            200, json={"MediaContainer": {"Metadata": [{"ratingKey": str(base + i)} for i in range(500)]}}
        )

    route = respx.get(f"{BASE}/library/sections/5/all").mock(side_effect=page)
    with pytest.raises(PlexError, match="pagination"):
        make_client().list_episodes(5)
    assert route.call_count == 3


def _hist_page(rows):
    return httpx.Response(200, json={"MediaContainer": {"Metadata": rows}})


@respx.mock
def test_history_pages_sharing_first_rating_key_do_not_stall(monkeypatch):
    """Rewatches repeat ratingKeys across history pages: only an identical page is a stall."""
    from plex_anidb_reconciler import plex

    monkeypatch.setattr(plex, "PAGE_SIZE", 2)
    p1 = [
        {"historyKey": "/h/1", "ratingKey": "7", "viewedAt": 100},
        {"historyKey": "/h/2", "ratingKey": "8", "viewedAt": 101},
    ]
    p2 = [
        {"historyKey": "/h/3", "ratingKey": "7", "viewedAt": 200},
        {"historyKey": "/h/4", "ratingKey": "9", "viewedAt": 201},
    ]
    respx.get(f"{BASE}/status/sessions/history/all").mock(side_effect=[_hist_page(p1), _hist_page(p2), _hist_page([])])
    hist = make_client().history(4)
    assert hist["7"] == [datetime.fromtimestamp(100, UTC), datetime.fromtimestamp(200, UTC)]


@respx.mock
def test_history_pages_without_history_key_compare_rating_key_and_viewed_at(monkeypatch):
    from plex_anidb_reconciler import plex

    monkeypatch.setattr(plex, "PAGE_SIZE", 1)
    respx.get(f"{BASE}/status/sessions/history/all").mock(
        side_effect=[
            _hist_page([{"ratingKey": "7", "viewedAt": 100}]),
            _hist_page([{"ratingKey": "7", "viewedAt": 200}]),
            _hist_page([]),
        ]
    )
    assert make_client().history(4) == {"7": [datetime.fromtimestamp(100, UTC), datetime.fromtimestamp(200, UTC)]}


@respx.mock
def test_identical_consecutive_history_pages_raise(monkeypatch):
    from plex_anidb_reconciler import plex

    monkeypatch.setattr(plex, "PAGE_SIZE", 2)
    page = [
        {"historyKey": "/h/1", "ratingKey": "7", "viewedAt": 100},
        {"historyKey": "/h/2", "ratingKey": "8", "viewedAt": 1},
    ]
    route = respx.get(f"{BASE}/status/sessions/history/all").mock(return_value=_hist_page(page))
    with pytest.raises(PlexError, match="pagination"):
        make_client().history(4)
    assert route.call_count == 2


@respx.mock
def test_history_entries_without_rating_key_do_not_trigger_stall(monkeypatch):
    from plex_anidb_reconciler import plex

    monkeypatch.setattr(plex, "PAGE_SIZE", 1)
    respx.get(f"{BASE}/status/sessions/history/all").mock(
        side_effect=[
            _hist_page([{"historyKey": "/h/1", "viewedAt": 100}]),
            _hist_page([{"historyKey": "/h/2", "viewedAt": 200}]),
            _hist_page([{"historyKey": "/h/3", "ratingKey": "5", "viewedAt": 300}]),
            _hist_page([]),
        ]
    )
    assert make_client().history(4) == {"5": [datetime.fromtimestamp(300, UTC)]}


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"MediaContainer": {"Metadata": {"ratingKey": "1"}}},  # Metadata not a list
        {"MediaContainer": {"Metadata": [{"viewCount": 1}]}},  # item without ratingKey
        {"MediaContainer": {"Metadata": ["x"]}},
        {"MediaContainer": {"Metadata": [{"ratingKey": "1", "Media": "bad"}]}},
        {"MediaContainer": {"Metadata": [{"ratingKey": "1", "lastViewedAt": "yesterday"}]}},
        {"MediaContainer": "oops"},
        [],
    ],
    ids=["metadata_dict", "no_rating_key", "non_object", "bad_media", "bad_last_viewed", "container_str", "list"],
)
def test_list_episodes_malformed_body_raises_plex_error(body):
    respx.get(f"{BASE}/library/sections/4/all").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(PlexError, match="unexpected body") as ei:
        make_client().list_episodes(4)
    assert TOKEN not in str(ei.value)


@respx.mock
@pytest.mark.parametrize(
    "rows",
    [[{"ratingKey": "1", "viewedAt": "soon"}], ["x"], {"ratingKey": "1"}],
    ids=["bad_viewed_at", "non_object", "metadata_dict"],
)
def test_history_malformed_body_raises_plex_error(rows):
    respx.get(f"{BASE}/status/sessions/history/all").mock(return_value=_hist_page(rows))
    with pytest.raises(PlexError, match="unexpected body"):
        make_client().history(4)


@respx.mock
def test_scrobble_non_2xx_raises_plex_error():
    respx.get(f"{BASE}/:/scrobble").mock(return_value=httpx.Response(401))
    with pytest.raises(PlexError, match="401") as ei:
        make_client().mark_played("1")
    assert TOKEN not in str(ei.value)
    assert str(ei.value) == "Plex GET /:/scrobble returned HTTP 401"

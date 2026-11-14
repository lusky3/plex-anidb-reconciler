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
    assert items[0].paths == ("/data/a/e1.mkv", "/data/a/e1b.mkv")
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
    assert "ConnectError" in str(ei.value)

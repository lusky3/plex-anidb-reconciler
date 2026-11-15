"""Plex Media Server client: episode watch state, owner play history, mark played."""

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from plex_anidb_reconciler.config import WRITE_PATH_TIMEOUT
from plex_anidb_reconciler.models import PlexItem

PAGE_SIZE = 500
MAX_PAGES = 1000
OWNER_ACCOUNT_ID = 1


class PlexError(Exception):
    """Plex request failed. Messages name the backend, method and host-less path, never the token."""


def _utc(epoch: object) -> datetime:
    if isinstance(epoch, bool) or not isinstance(epoch, int | float):
        raise TypeError("timestamp is not a number")
    return datetime.fromtimestamp(epoch, UTC)


def _bad_body(path: str, exc: Exception) -> PlexError:
    """Malformed 2xx body: name the exception type only (values may echo request data)."""
    return PlexError(f"Plex GET {path} returned an unexpected body ({type(exc).__name__})")


def _parse_episode(m: dict, section: int) -> PlexItem:
    if m.get("ratingKey") is None:
        raise KeyError("ratingKey")
    last = m.get("lastViewedAt")
    return PlexItem(
        rating_key=str(m["ratingKey"]),
        section=section,
        paths=tuple(p["file"] for media in m.get("Media") or [] for p in media.get("Part") or [] if p.get("file")),
        view_count=int(m.get("viewCount") or 0),
        last_viewed_at=_utc(last) if last else None,
    )


def _parse_page(path: str, resp: httpx.Response) -> tuple[list, int | None]:
    """The ``Metadata`` entries and ``totalSize`` of one page."""
    try:
        container = resp.json()["MediaContainer"]
        if not isinstance(container, dict):
            raise TypeError("MediaContainer is not an object")
        items = container.get("Metadata") or []
        if not isinstance(items, list):
            raise TypeError("Metadata is not a list")
        total = container.get("totalSize")
        return items, None if total is None else int(total)
    except (ValueError, KeyError, TypeError) as exc:
        raise _bad_body(path, exc) from None


def _episode_sig(m: dict) -> object:
    return m.get("ratingKey")


def _history_sig(m: dict) -> object:
    key = m.get("historyKey")
    return key if key is not None else (m.get("ratingKey"), m.get("viewedAt"))


class PlexClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client):
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._headers = {"X-Plex-Token": token, "Accept": "application/json"}

    def _request(
        self,
        path: str,
        params: dict[str, str],
        extra_headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        """GET with the client's timeout, or ``timeout`` if given."""
        headers = {**self._headers, **(extra_headers or {})}
        extra = {} if timeout is None else {"timeout": timeout}
        try:
            resp = self._client.get(f"{self.base_url}{path}", params=params, headers=headers, **extra)
        except httpx.HTTPError as exc:
            raise PlexError(f"Plex GET {path} failed: {type(exc).__name__}") from None
        if not resp.is_success:
            raise PlexError(f"Plex GET {path} returned HTTP {resp.status_code}")
        return resp

    def _paged(self, path: str, params: dict[str, str], signature: Callable[[dict], object]) -> list[dict]:
        """Fetch every page. A page identical to the previous one (by ``signature`` of each entry) means
        the server ignored the offset; history pages may legitimately repeat ratingKeys (rewatches)."""
        out: list[dict] = []
        start = 0
        previous: tuple | None = None
        for _ in range(MAX_PAGES):
            headers = {"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(PAGE_SIZE)}
            items, total = _parse_page(path, self._request(path, params, headers))
            if not items:
                return out
            page = tuple(signature(m) if isinstance(m, dict) else None for m in items)
            if page == previous:
                raise PlexError(f"Plex GET {path} pagination did not advance")
            previous = page
            out.extend(items)
            start += len(items)
            if len(items) < PAGE_SIZE or (total is not None and start >= total):
                return out
        raise PlexError(f"Plex GET {path} pagination exceeded {MAX_PAGES} pages")

    def list_episodes(self, section: int) -> list[PlexItem]:
        path = f"/library/sections/{section}/all"
        raw = self._paged(path, {"type": "4"}, _episode_sig)
        try:
            return [_parse_episode(m, section) for m in raw]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise _bad_body(path, exc) from None

    def history(self, section: int) -> dict[str, list[datetime]]:
        path = "/status/sessions/history/all"
        params = {"librarySectionID": str(section), "accountID": str(OWNER_ACCOUNT_ID)}
        grouped: dict[str, list[datetime]] = defaultdict(list)
        try:
            for entry in self._paged(path, params, _history_sig):
                if entry.get("ratingKey") is None or entry.get("viewedAt") is None:
                    continue
                grouped[str(entry["ratingKey"])].append(_utc(entry["viewedAt"]))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError) as exc:
            raise _bad_body(path, exc) from None
        return {rk: sorted(v) for rk, v in grouped.items()}

    def mark_played(self, rating_key: str) -> None:
        params = {"key": rating_key, "identifier": "com.plexapp.plugins.library"}
        self._request("/:/scrobble", params, timeout=WRITE_PATH_TIMEOUT)

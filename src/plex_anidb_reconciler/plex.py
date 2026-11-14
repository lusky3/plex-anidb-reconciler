"""Plex Media Server client: episode watch state, owner play history, mark played."""

from collections import defaultdict
from datetime import UTC, datetime

import httpx

from plex_anidb_reconciler.models import PlexItem

PAGE_SIZE = 500
OWNER_ACCOUNT_ID = 1


class PlexError(Exception):
    """Plex request failed. Messages never contain the token."""


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


class PlexClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client):
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._headers = {"X-Plex-Token": token, "Accept": "application/json"}

    def _request(
        self, path: str, params: dict[str, str], extra_headers: dict[str, str] | None = None
    ) -> httpx.Response:
        headers = {**self._headers, **(extra_headers or {})}
        try:
            resp = self._client.get(f"{self.base_url}{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise PlexError(f"GET {path} failed: {type(exc).__name__}") from None
        if not resp.is_success:
            raise PlexError(f"GET {path} returned HTTP {resp.status_code}")
        return resp

    def _paged(self, path: str, params: dict[str, str]) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            headers = {"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(PAGE_SIZE)}
            try:
                container = self._request(path, params, headers).json()["MediaContainer"]
            except ValueError, KeyError, TypeError:
                raise PlexError(f"GET {path} returned an unexpected body") from None
            items = container.get("Metadata") or []
            out.extend(items)
            start += len(items)
            total = container.get("totalSize")
            if len(items) < PAGE_SIZE or (total is not None and start >= total):
                return out

    def list_episodes(self, section: int) -> list[PlexItem]:
        raw = self._paged(f"/library/sections/{section}/all", {"type": "4"})
        return [
            PlexItem(
                rating_key=str(m["ratingKey"]),
                section=section,
                paths=tuple(
                    p["file"] for media in m.get("Media") or [] for p in media.get("Part") or [] if p.get("file")
                ),
                view_count=m.get("viewCount") or 0,
                last_viewed_at=_utc(m["lastViewedAt"]) if m.get("lastViewedAt") else None,
            )
            for m in raw
        ]

    def history(self, section: int) -> dict[str, list[datetime]]:
        params = {"librarySectionID": str(section), "accountID": str(OWNER_ACCOUNT_ID)}
        grouped: dict[str, list[datetime]] = defaultdict(list)
        for entry in self._paged("/status/sessions/history/all", params):
            if entry.get("ratingKey") is None or entry.get("viewedAt") is None:
                continue
            grouped[str(entry["ratingKey"])].append(_utc(entry["viewedAt"]))
        return {rk: sorted(v) for rk, v in grouped.items()}

    def mark_played(self, rating_key: str) -> None:
        self._request("/:/scrobble", {"key": rating_key, "identifier": "com.plexapp.plugins.library"})

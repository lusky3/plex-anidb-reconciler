"""Shoko Server v3 client: file index (read) and UserStats (the only write)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from plex_anidb_reconciler.models import ShokoFile, ShokoUserStats


class ShokoError(Exception):
    """Shoko request failed. Messages never contain the API key."""


PARSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError)


def _bad_body(path: str, exc: Exception) -> ShokoError:
    """Malformed 2xx body: name the exception type only (values may echo request data)."""
    return ShokoError(f"GET {path} returned an unexpected body ({type(exc).__name__})")


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("boolean where an id was expected")
    return int(value)


def to_shoko_time(when: datetime, tz: ZoneInfo) -> str:
    if when.tzinfo is None:
        raise ValueError("naive datetime; a tz-aware value is required")
    return when.replace(microsecond=0).astimezone(tz).isoformat()


def _parse_created(value: object, tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=tz)


def _parse_user_stats(raw: object, tz: ZoneInfo) -> ShokoUserStats:
    """Strict: anything unexpected raises, so the caller fails closed instead of writing."""
    if not isinstance(raw, dict):
        raise TypeError("user stats is not an object")
    count = raw["WatchedCount"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("WatchedCount is not a non-negative integer")
    last = raw["LastWatchedAt"]
    if last is None:
        return ShokoUserStats(count, None)
    if not isinstance(last, str):
        raise TypeError("LastWatchedAt is not a string")
    parsed = datetime.fromisoformat(last)
    return ShokoUserStats(count, parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=tz))


def _parse_file(raw: dict, tz: ZoneInfo) -> ShokoFile:
    eids: list[int] = []
    manual = False
    for series in raw.get("SeriesIDs") or []:
        for ep in series.get("EpisodeIDs") or []:
            if ep.get("Source") == "User":
                manual = True
            eid = _opt_int(ep.get("AniDB"))
            if eid is not None and eid not in eids:
                eids.append(eid)
    anidb = raw.get("AniDB") or {}
    paths = tuple(str(p) for loc in raw.get("Locations") or [] if (p := loc.get("AbsolutePath")))
    return ShokoFile(
        file_id=int(raw["ID"]),
        fid=_opt_int(anidb.get("ID")),
        eids=tuple(eids),
        manual_link=manual,
        abs_paths=paths,
        created_at=_parse_created(raw.get("Created"), tz),
    )


class ShokoClient:
    def __init__(self, base_url: str, api_key: str, tz: ZoneInfo, client: httpx.Client):
        self.base_url = base_url.rstrip("/")
        self.tz = tz
        self._client = client
        self._headers = {"apikey": api_key}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.request(method, f"{self.base_url}{path}", headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ShokoError(f"{method} {path} failed: {type(exc).__name__}") from None
        if not resp.is_success:
            raise ShokoError(f"{method} {path} returned HTTP {resp.status_code}")
        return resp

    def list_files(self) -> list[ShokoFile]:
        params = {"pageSize": "0", "include": "XRefs,AbsolutePaths", "includeDataFrom": "AniDB"}
        resp = self._request("GET", "/api/v3/File", params=params)
        try:
            data = resp.json()
            items = data["List"] if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise TypeError("file list is not a list")
            return [_parse_file(item, self.tz) for item in items]
        except PARSE_ERRORS as exc:
            raise _bad_body("/api/v3/File", exc) from None

    def get_file(self, file_id: int) -> ShokoFile:
        """Current state of one file (read-only), used to re-check right before a write.
        The include* booleans are sent too in case this endpoint ignores ``include``."""
        params = {
            "include": "XRefs,AbsolutePaths",
            "includeXRefs": "true",
            "includeAbsolutePaths": "true",
            "includeDataFrom": "AniDB",
        }
        resp = self._request("GET", f"/api/v3/File/{file_id}", params=params)
        try:
            return _parse_file(resp.json(), self.tz)
        except PARSE_ERRORS as exc:
            raise _bad_body(f"/api/v3/File/{file_id}", exc) from None

    def get_user_stats(self, file_id: int) -> ShokoUserStats:
        """Shoko's current watched state of one file (read-only), checked right before a write."""
        path = f"/api/v3/File/{file_id}/UserStats"
        resp = self._request("GET", path)
        try:
            return _parse_user_stats(resp.json(), self.tz)
        except PARSE_ERRORS as exc:
            raise _bad_body(path, exc) from None

    def set_watched(self, file_id: int, when: datetime) -> None:
        stamp = to_shoko_time(when, self.tz)
        body = {"WatchedCount": 1, "LastWatchedAt": stamp, "ResumePosition": None, "LastUpdatedAt": stamp}
        self._request("PUT", f"/api/v3/File/{file_id}/UserStats", json=body)

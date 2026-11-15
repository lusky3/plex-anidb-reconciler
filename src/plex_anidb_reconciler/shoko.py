"""Shoko Server v3 client: file index (read) and UserStats (the only write)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from plex_anidb_reconciler.config import WRITE_PATH_TIMEOUT
from plex_anidb_reconciler.models import ShokoFile, ShokoUserStats


class ShokoError(Exception):
    """Shoko request failed. Messages name the backend, method and host-less path, never the API key."""


PARSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError)


def _bad_body(path: str, exc: Exception) -> ShokoError:
    """Malformed 2xx body: name the exception type only (values may echo request data)."""
    return ShokoError(f"Shoko GET {path} returned an unexpected body ({type(exc).__name__})")


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


def _report_aid(series_id: object) -> int | None:
    """AniDB anime id of a SeriesIDs entry (report only): anything unexpected is None, never an error."""
    value = series_id.get("AniDB") if isinstance(series_id, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    return None


def _release_group(raw: object) -> str | None:
    """Short name, else full name, of the AniDB release group (report only; never fails a parse)."""
    if not isinstance(raw, dict):
        return None
    for key in ("ShortName", "Name"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_file(raw: dict, tz: ZoneInfo) -> ShokoFile:
    eids: list[int] = []
    manual = False
    aid: int | None = None
    for series in raw.get("SeriesIDs") or []:
        if aid is None:
            aid = _report_aid(series.get("SeriesID"))
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
        aid=aid,
        release_group=_release_group(anidb.get("ReleaseGroup")),
    )


# Marker of Shoko 5.3.3's "no stats yet" 404 body (plain text, a JSON string or ProblemDetails alike).
_NO_USER_STATS = "fileuserstats"


class ShokoClient:
    def __init__(self, base_url: str, api_key: str, tz: ZoneInfo, client: httpx.Client):
        self.base_url = base_url.rstrip("/")
        self.tz = tz
        self._client = client
        self._headers = {"apikey": api_key}

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Any HTTP response; transport errors become ShokoError (type name only)."""
        try:
            return self._client.request(method, f"{self.base_url}{path}", headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ShokoError(f"Shoko {method} {path} failed: {type(exc).__name__}") from None

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = self._send(method, path, **kwargs)
        if not resp.is_success:
            raise ShokoError(f"Shoko {method} {path} returned HTTP {resp.status_code}")
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
        It sends the include* booleans too, in case this endpoint ignores ``include``."""
        params = {
            "include": "XRefs,AbsolutePaths",
            "includeXRefs": "true",
            "includeAbsolutePaths": "true",
            "includeDataFrom": "AniDB",
        }
        resp = self._request("GET", f"/api/v3/File/{file_id}", params=params, timeout=WRITE_PATH_TIMEOUT)
        try:
            return _parse_file(resp.json(), self.tz)
        except PARSE_ERRORS as exc:
            raise _bad_body(f"/api/v3/File/{file_id}", exc) from None

    def get_user_stats(self, file_id: int) -> ShokoUserStats:
        """Shoko's current watched state of one file (read-only), checked right before a write.

        Shoko 5.3.3 (FileController.GetFileUserStats) answers 404 with the text/plain body
        "No FileUserStats entry for the given fileID for the current user" for a file nobody has watched
        yet: that is unwatched. Any other 404 (e.g. "No File entry for the given fileID"), any other error
        status, a transport error or a malformed 2xx body raises ShokoError."""
        path = f"/api/v3/File/{file_id}/UserStats"
        resp = self._send("GET", path, timeout=WRITE_PATH_TIMEOUT)
        if resp.status_code == 404 and _NO_USER_STATS in resp.text.lower():
            return ShokoUserStats(0, None)
        if not resp.is_success:
            raise ShokoError(f"Shoko GET {path} returned HTTP {resp.status_code}")
        try:
            return _parse_user_stats(resp.json(), self.tz)
        except PARSE_ERRORS as exc:
            raise _bad_body(path, exc) from None

    def set_watched(self, file_id: int, when: datetime) -> None:
        stamp = to_shoko_time(when, self.tz)
        body = {"WatchedCount": 1, "LastWatchedAt": stamp, "ResumePosition": None, "LastUpdatedAt": stamp}
        self._request("PUT", f"/api/v3/File/{file_id}/UserStats", json=body, timeout=WRITE_PATH_TIMEOUT)

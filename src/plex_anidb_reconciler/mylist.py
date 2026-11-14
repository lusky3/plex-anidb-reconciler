"""Load Shoko's AniDB MyList backup (``mylist.json``) and index it for lookups."""

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from plex_anidb_reconciler.models import MyListEntry


class SnapshotError(Exception):
    """The MyList snapshot is missing, unreadable, or malformed."""


def _parse_viewed_at(value: object, path: Path) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise SnapshotError(f"{path}: invalid ViewedAt timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotError(f"{path}: ViewedAt has no timezone offset")
    return parsed


def _parse_entry(raw: object, path: Path) -> MyListEntry:
    if not isinstance(raw, dict):
        raise SnapshotError(f"{path}: entry is not an object")
    try:
        return MyListEntry(
            fid=raw.get("FileID") or None,
            eid=raw.get("EpisodeID"),
            aid=raw.get("AnimeID"),
            viewed_at=_parse_viewed_at(raw.get("ViewedAt"), path),
            state=int(raw.get("State") or 0),
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"{path}: malformed entry") from exc


def load_mylist(path: Path) -> list[MyListEntry]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotError(f"{path}: cannot read snapshot ({exc.strerror or type(exc).__name__})") from exc
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"{path}: snapshot is not valid UTF-8") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SnapshotError(f"{path}: invalid JSON") from exc
    if not isinstance(data, list):
        raise SnapshotError(f"{path}: expected a JSON array")
    return [_parse_entry(raw, path) for raw in data]


def snapshot_age(path: Path, now: datetime) -> timedelta:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
    return now - mtime


class MyListIndex:
    def __init__(self, entries: Iterable[MyListEntry]) -> None:
        self._watched_fids: set[int] = set()
        self._all_fids: set[int] = set()
        self._views: dict[int, list[datetime]] = defaultdict(list)
        self._fid_eids: dict[int, set[int]] = defaultdict(set)
        for entry in entries:
            if entry.fid:
                self._all_fids.add(entry.fid)
                if entry.eid is not None:
                    self._fid_eids[entry.fid].add(entry.eid)
            if entry.viewed_at is None:
                continue
            if entry.fid:
                self._watched_fids.add(entry.fid)
            if entry.eid is not None:
                self._views[entry.eid].append(entry.viewed_at)

    def has_fid(self, fid: int | None) -> bool:
        return bool(fid) and fid in self._all_fids

    def fid_watched(self, fid: int | None) -> bool:
        return bool(fid) and fid in self._watched_fids

    def eids_for_fid(self, fid: int | None) -> frozenset[int]:
        """Episodes AniDB links this file to (every snapshot entry for the fid with an EpisodeID)."""
        return frozenset(self._fid_eids.get(fid, ())) if fid else frozenset()

    def episode_watched_at(self, eid: int) -> list[datetime]:
        return list(self._views.get(eid, []))

    def watched_eids(self) -> set[int]:
        return set(self._views)

    def max_watch(self, eids: Iterable[int]) -> datetime | None:
        dates = [d for eid in eids for d in self._views.get(eid, [])]
        return max(dates) if dates else None

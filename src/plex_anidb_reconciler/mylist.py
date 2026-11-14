"""Load Shoko's AniDB MyList backup (``mylist.json``) and index it for lookups."""

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from plex_anidb_reconciler.models import MyListEntry


class SnapshotError(Exception):
    """The MyList snapshot is missing, unreadable, or malformed."""


class _BadEntry(Exception):
    """One snapshot entry is malformed (the message names the problem, never a value)."""


def _parse_viewed_at(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _BadEntry("invalid ViewedAt timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _BadEntry("invalid ViewedAt timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _BadEntry("ViewedAt has no timezone offset")
    return parsed


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise _BadEntry(f"{name} is not an integer")
    try:
        return int(value)
    except ValueError:
        raise _BadEntry(f"{name} is not an integer") from None


def _opt_int(raw: dict, name: str) -> int | None:
    value = raw.get(name)
    return None if value is None else _as_int(value, name)


def _parse_entry(raw: object) -> MyListEntry:
    if not isinstance(raw, dict):
        raise _BadEntry("entry is not an object")
    return MyListEntry(
        fid=_opt_int(raw, "FileID") or None,  # FileID 0 = generic entry (no file)
        eid=_opt_int(raw, "EpisodeID"),
        aid=_opt_int(raw, "AnimeID"),
        viewed_at=_parse_viewed_at(raw.get("ViewedAt")),
        state=_opt_int(raw, "State") or 0,  # informational only: never used for decisions
    )


def load_mylist(path: Path) -> list[MyListEntry]:
    """Parse the snapshot. Any malformed entry rejects the whole snapshot (fail closed): silently
    dropping an entry could hide a watched episode and lead to a duplicate dated MyList entry."""
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
    entries: list[MyListEntry] = []
    bad: list[tuple[int, str]] = []
    for i, raw in enumerate(data):
        try:
            entries.append(_parse_entry(raw))
        except _BadEntry as exc:
            bad.append((i, str(exc)))
    if bad:
        first_index, first_problem = bad[0]
        raise SnapshotError(
            f"{path}: {len(bad)} malformed entr{'y' if len(bad) == 1 else 'ies'}"
            f" (first at index {first_index}: {first_problem}); snapshot rejected"
        )
    return entries


FUTURE_SKEW = timedelta(minutes=5)  # tolerated clock skew between the Shoko host and this one


def snapshot_is_fresh(age: timedelta, max_age: timedelta) -> bool:
    """Fresh = not older than max_age and not (beyond skew) in the future: a bogus future mtime must not
    pass as fresh forever."""
    return -FUTURE_SKEW <= age < max_age


def snapshot_age(path: Path, now: datetime) -> timedelta:
    """Signed age of the snapshot by mtime (negative: mtime is in the future)."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
    except OSError as exc:
        raise SnapshotError(f"{path}: cannot stat snapshot ({exc.strerror or type(exc).__name__})") from exc
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

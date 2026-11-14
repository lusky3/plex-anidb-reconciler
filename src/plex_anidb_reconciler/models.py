"""Plain data types shared across the reconciler."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


@dataclass(frozen=True)
class MyListEntry:
    fid: int | None
    eid: int | None
    aid: int | None
    viewed_at: datetime | None
    state: int


@dataclass(frozen=True)
class ShokoFile:
    file_id: int
    fid: int | None
    eids: tuple[int, ...]
    manual_link: bool
    abs_paths: tuple[str, ...]
    created_at: datetime | None = None


@dataclass(frozen=True)
class PlexItem:
    rating_key: str
    section: int
    paths: tuple[str, ...]
    view_count: int
    last_viewed_at: datetime | None


class ActionKind(Enum):
    MARK_PLEX_PLAYED = "mark_plex_played"
    SET_FILE_WATCHED = "set_file_watched"
    REPORT = "report"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    rule: str  # one of R1, R2, R2b, R4
    rating_key: str | None
    file_id: int | None
    fid: int | None
    eids: tuple[int, ...]
    when: datetime | None
    reason: str
    # An R2b whose episode counts as watched only because of an R2 planned in this same run: it may be
    # executed only after that R2 (this Shoko file id) was applied in this run.
    depends_on_file_id: int | None = None


@dataclass(frozen=True)
class PendingWatch:
    """A watch the reconciler wrote (or is about to write) that the MyList snapshot may not show yet."""

    file_id: int
    fid: int | None
    eids: tuple[int, ...]
    when: datetime

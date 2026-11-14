"""Plain data types shared across the reconciler."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, StrEnum


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
class ShokoUserStats:
    """Shoko's current watched state of one file (GET /api/v3/File/{id}/UserStats)."""

    watched_count: int
    last_watched_at: datetime | None

    @property
    def watched(self) -> bool:
        return self.last_watched_at is not None or self.watched_count > 0


@dataclass(frozen=True)
class PlexItem:
    rating_key: str
    section: int
    paths: tuple[str, ...]
    view_count: int
    last_viewed_at: datetime | None


class Rule(StrEnum):
    R1 = "R1"  # AniDB episode watched -> mark the Plex item played
    R2 = "R2"  # new watch -> that exact Shoko file
    R2B = "R2b"  # re-watch with a different release
    R4 = "R4"  # ambiguity: report only


class Status(StrEnum):
    """Outcome of one action in a run (CSV ``status`` column; only APPLIED/FAILED reach the ledger)."""

    APPLIED = "applied"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    REPORTED = "reported"
    CAPPED = "capped"
    SKIPPED = "skipped"
    SKIPPED_LEDGER = "skipped_ledger"


class R2bPolicy(StrEnum):
    RECORD = "record"
    IGNORE = "ignore"


class SnapshotState(StrEnum):
    OK = "ok"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class ActionKind(Enum):
    MARK_PLEX_PLAYED = "mark_plex_played"
    SET_FILE_WATCHED = "set_file_watched"
    REPORT = "report"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    rule: Rule
    rating_key: str | None
    file_id: int | None
    fid: int | None
    eids: tuple[int, ...]
    when: datetime | None
    reason: str
    # An R2b whose episode counts as watched only because of an R2 planned in this same run: it may be
    # executed only after that R2 (this Shoko file id) was applied in this run.
    depends_on_file_id: int | None = None
    # SETs only: the Shoko absolute path the Plex path matched exactly. The pre-write re-check fails
    # (shoko_changed) if the file no longer has it, e.g. another release replaced the file at that path.
    matched_path: str | None = None


@dataclass(frozen=True)
class PendingWatch:
    """A watch the reconciler wrote (or is about to write) that the MyList snapshot may not show yet."""

    file_id: int
    fid: int | None
    eids: tuple[int, ...]
    when: datetime


@dataclass(frozen=True)
class Outcome:
    """What happened to one planned action in a run (one CSV row)."""

    action: Action
    status: Status
    detail: str = ""

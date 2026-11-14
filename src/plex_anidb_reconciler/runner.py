"""One reconcile pass: load, plan, filter via ledger, apply under a cap, report."""

import json
import logging
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.ledger import Ledger
from plex_anidb_reconciler.models import (
    Action,
    ActionKind,
    MyListEntry,
    Outcome,
    PlexItem,
    Rule,
    ShokoFile,
    SnapshotState,
    Status,
)
from plex_anidb_reconciler.mylist import MyListIndex, SnapshotError, load_mylist, snapshot_age, snapshot_is_fresh
from plex_anidb_reconciler.paths import PathMatcher
from plex_anidb_reconciler.plex import PlexClient, PlexError
from plex_anidb_reconciler.report import prune_reports, summarize, write_report
from plex_anidb_reconciler.rules import plan
from plex_anidb_reconciler.shoko import ShokoClient, ShokoError

log = logging.getLogger(__name__)
EXECUTABLE_RULES = (Rule.R1, Rule.R2, Rule.R2B)
KEEP_REPORTS = 200


@dataclass
class Clients:
    shoko: ShokoClient
    plex: PlexClient


@dataclass
class RunResult:
    run_id: str
    counts: dict[str, int]
    report_path: Path
    snapshot: SnapshotState = SnapshotState.OK


@dataclass(frozen=True)
class Snapshot:
    entries: list[MyListEntry]
    fresh: bool


def build_clients(settings: Settings, http: httpx.Client) -> Clients:
    return Clients(
        ShokoClient(settings.shoko_url, settings.shoko_api_key, settings.shoko_tz, http),
        PlexClient(settings.plex_url, settings.plex_token, http),
    )


# --- Shoko index cache ---


def _file_to_json(shoko_file: ShokoFile) -> dict:
    return {
        "file_id": shoko_file.file_id,
        "fid": shoko_file.fid,
        "eids": list(shoko_file.eids),
        "manual_link": shoko_file.manual_link,
        "abs_paths": list(shoko_file.abs_paths),
        "created_at": shoko_file.created_at.isoformat() if shoko_file.created_at else None,
    }


def _file_from_json(d: dict) -> ShokoFile:
    return ShokoFile(
        file_id=d["file_id"],
        fid=d["fid"],
        eids=tuple(d["eids"]),
        manual_link=d["manual_link"],
        abs_paths=tuple(d["abs_paths"]),
        created_at=datetime.fromisoformat(d["created_at"]) if d["created_at"] else None,
    )


def _cached_index(settings: Settings, cache: Path, now: datetime) -> list[ShokoFile] | None:
    """The cached Shoko index if it is from this server and within the TTL, else None."""
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(data["fetched_at"])
        same_server = data.get("shoko_url") == settings.shoko_url  # a cache from another server is never reused
        if same_server and timedelta(0) <= now - fetched < timedelta(minutes=settings.shoko_index_ttl_min):
            return [_file_from_json(d) for d in data["files"]]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.debug("shoko index cache not used (%s)", type(exc).__name__)
    return None


def _store_index(settings: Settings, cache: Path, now: datetime, files: list[ShokoFile]) -> None:
    payload = {
        "shoko_url": settings.shoko_url,
        "fetched_at": now.isoformat(),
        "files": [_file_to_json(f) for f in files],
    }
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.debug("shoko index cache not written (%s)", type(exc).__name__)


def _shoko_index(settings: Settings, shoko: ShokoClient, now: datetime) -> list[ShokoFile]:
    cache = Path(settings.data_dir) / "shoko_index.json"
    files = _cached_index(settings, cache, now)
    if files is None:
        files = shoko.list_files()
        _store_index(settings, cache, now, files)
    return files


# --- inputs and planning ---


def _fetch_plex(settings: Settings, plex: PlexClient) -> tuple[list[PlexItem], dict[str, list[datetime]]]:
    items: list[PlexItem] = []
    history: dict[str, list[datetime]] = {}
    for section in settings.plex_sections:
        items.extend(plex.list_episodes(section))
        for rating_key, times in plex.history(section).items():
            history.setdefault(rating_key, []).extend(times)
    for times in history.values():
        times.sort()
    return items, history


def _load_snapshot(settings: Settings, now: datetime) -> Snapshot | None:
    """The MyList snapshot and whether it is fresh enough for R2/R2b, or None if it cannot be read."""
    path = Path(settings.mylist_path)
    try:
        entries = load_mylist(path)
        age = snapshot_age(path, now)
    except SnapshotError, OSError:
        return None
    return Snapshot(entries, snapshot_is_fresh(age, timedelta(hours=settings.snapshot_max_age_h)))


def _plan_actions(
    settings: Settings, clients: Clients, ledger: Ledger, snapshot: Snapshot, now: datetime
) -> list[Action]:
    files = _shoko_index(settings, clients.shoko, now)
    items, history = _fetch_plex(settings, clients.plex)
    return plan(
        items,
        history,
        PathMatcher(files, settings.path_map),
        MyListIndex(snapshot.entries),
        settings.r2b_policy,
        snapshot.fresh,
        r1_marked=ledger.applied_r1_keys(),
        pending=ledger.applied_sets(),
    )


def _selected(action: Action, rules: frozenset[Rule] | None, file_id: int | None) -> bool:
    return (rules is None or action.rule in rules) and (file_id is None or action.file_id == file_id)


def _execution_order(actions: list[Action], rules: frozenset[Rule] | None, file_id: int | None) -> list[Action]:
    """R1 (Plex only) first, then every R2 before any R2b: an R2b may depend on an R2 of this run
    (Action.depends_on_file_id)."""
    marks = [a for a in actions if a.kind is ActionKind.MARK_PLEX_PLAYED]
    sets = [a for a in actions if a.kind is ActionKind.SET_FILE_WATCHED]
    ordered = marks + [a for a in sets if a.rule is not Rule.R2B] + [a for a in sets if a.rule is Rule.R2B]
    return [a for a in ordered if _selected(a, rules, file_id)]


# --- execution ---


def _shoko_changed(action: Action, current: ShokoFile) -> bool:
    """The Shoko index may be cached and stale, and a file can be re-linked or replaced between runs:
    only write if Shoko still links this file to the same fid and episodes, not manually, and the file
    still sits at the exact path the Plex item matched (another release may have replaced it)."""
    return (
        current.fid is None
        or current.fid != action.fid
        or current.manual_link
        or sorted(current.eids) != sorted(action.eids)
        or action.matched_path is None
        or action.matched_path not in current.abs_paths
    )


def _execute(action: Action, clients: Clients) -> Outcome:
    """Perform one write. Raises ValueError for an action the planner can never produce."""
    if action.kind is ActionKind.MARK_PLEX_PLAYED:
        if action.rating_key is None:
            raise ValueError("R1 action without rating_key")
    elif action.kind is not ActionKind.SET_FILE_WATCHED or action.file_id is None or action.when is None:
        raise ValueError("not an executable action")
    try:
        if action.kind is ActionKind.MARK_PLEX_PLAYED:
            clients.plex.mark_played(action.rating_key)
        elif _shoko_changed(action, clients.shoko.get_file(action.file_id)):
            return Outcome(action, Status.FAILED, "shoko_changed")
        else:
            clients.shoko.set_watched(action.file_id, action.when)
    except (ShokoError, PlexError) as exc:
        return Outcome(action, Status.FAILED, str(exc))
    return Outcome(action, Status.APPLIED)


class _Executor:
    """Decides each executable action's status in order, writing (apply) under the cap. Applied and
    failed writes are committed to the ledger before the next write starts."""

    def __init__(
        self, run_id: str, ledger: Ledger, clients: Clients, *, apply: bool, budget: int, stop: threading.Event | None
    ) -> None:
        self._run_id = run_id
        self._ledger = ledger
        self._clients = clients
        self._apply = apply
        self._budget = budget
        self._stop = stop
        self._set_status: dict[int, Status] = {}  # file_id -> status of its SET in this run
        self.shoko_changed = 0

    def run(self, actions: list[Action]) -> list[Outcome]:
        return [self._run_one(action) for action in actions]

    def _run_one(self, action: Action) -> Outcome:
        outcome = self._without_write(action) or self._write(action)
        if action.kind is ActionKind.SET_FILE_WATCHED and action.file_id is not None:
            self._set_status[action.file_id] = outcome.status
        return outcome

    def _without_write(self, action: Action) -> Outcome | None:
        """The outcome of an action that is not written this run, or None if it should be written."""
        key = action.rating_key if action.kind is ActionKind.MARK_PLEX_PLAYED else action.file_id
        if self._ledger.seen_success(action.kind, key):
            return Outcome(action, Status.SKIPPED_LEDGER)
        if not self._prerequisite_met(action):
            return Outcome(action, Status.SKIPPED, "prerequisite_not_applied")  # CSV only; reconsidered next run
        if not self._apply:
            return Outcome(action, Status.DRY_RUN)
        if self._stop is not None and self._stop.is_set():
            return Outcome(action, Status.SKIPPED, "shutdown")  # CSV only; re-planned next run
        if self._budget <= 0:
            return Outcome(action, Status.CAPPED)
        return None

    def _prerequisite_met(self, action: Action) -> bool:
        if action.depends_on_file_id is None:
            return True
        success = Status.APPLIED if self._apply else Status.DRY_RUN
        return self._set_status.get(action.depends_on_file_id) == success

    def _write(self, action: Action) -> Outcome:
        self._budget -= 1
        outcome = _execute(action, self._clients)
        self.shoko_changed += outcome.detail == "shoko_changed"
        self._ledger.record(self._run_id, action, outcome.status, outcome.detail)
        return outcome


# --- run ---


def _new_run_id(now: datetime) -> str:
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _write_budget(settings: Settings, limit: int | None) -> int:
    cap = settings.max_writes if limit is None else min(limit, settings.max_writes)
    return max(cap, 0)


@contextmanager
def _open_clients(settings: Settings, clients: Clients | None) -> Iterator[Clients]:
    """The given clients, or real ones on an HTTP client that is closed afterwards."""
    if clients is not None:
        yield clients
        return
    with httpx.Client(timeout=60) as http:
        yield build_clients(settings, http)


def _finish(outcomes: list[Outcome], report_path: Path) -> None:
    write_report(outcomes, report_path)
    prune_reports(report_path.parent, KEEP_REPORTS)


def _snapshot_unavailable(run_id: str, report_path: Path) -> RunResult:
    _finish([], report_path)
    print("snapshot_unavailable: no actions planned")
    log.warning("run %s: MyList snapshot unavailable, no actions planned", run_id)
    return RunResult(run_id, {"snapshot_unavailable": 1}, report_path, SnapshotState.UNAVAILABLE)


def _summarise(
    run_id: str, outcomes: list[Outcome], shoko_changed: int, report_path: Path, *, apply: bool, fresh: bool
) -> RunResult:
    print(summarize(outcomes))
    counts: dict[str, int] = dict(Counter(o.status for o in outcomes))
    if shoko_changed:
        counts["shoko_changed"] = shoko_changed
    snapshot = SnapshotState.OK if fresh else SnapshotState.STALE
    log.info("run %s (%s, snapshot %s): %s", run_id, "apply" if apply else "plan", snapshot, counts or "no actions")
    return RunResult(run_id, counts, report_path, snapshot)


def run_once(
    settings: Settings,
    *,
    apply: bool,
    limit: int | None,
    now: datetime,
    clients: Clients | None = None,
    rules: frozenset[Rule] | None = None,
    file_id: int | None = None,
    stop: threading.Event | None = None,
) -> RunResult:
    """rules/file_id restrict which executable actions (R1/R2/R2b) run; reports are always listed.
    Once ``stop`` is set (shutdown), no further write starts: the remaining executable actions are recorded
    as skipped/shutdown (an in-flight write completes and is recorded first).
    Dry runs are uncapped. Only applied/failed writes reach the ledger; everything goes to the CSV."""
    run_id = _new_run_id(now)
    report_path = Path(settings.data_dir) / "reports" / f"{run_id}.csv"
    snapshot = _load_snapshot(settings, now)
    if snapshot is None:
        return _snapshot_unavailable(run_id, report_path)

    with (
        _open_clients(settings, clients) as active,
        closing(Ledger(Path(settings.data_dir) / "ledger.sqlite")) as ledger,
    ):
        actions = _plan_actions(settings, active, ledger, snapshot, now)
        executor = _Executor(run_id, ledger, active, apply=apply, budget=_write_budget(settings, limit), stop=stop)
        reported = [Outcome(a, Status.REPORTED) for a in actions if a.kind is ActionKind.REPORT]
        outcomes = reported + executor.run(_execution_order(actions, rules, file_id))

    _finish(outcomes, report_path)
    return _summarise(run_id, outcomes, executor.shoko_changed, report_path, apply=apply, fresh=snapshot.fresh)

"""One reconcile pass: load, plan, filter via ledger, apply under a cap, report."""

import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from plex_anidb_reconciler.config import Settings
from plex_anidb_reconciler.ledger import Ledger
from plex_anidb_reconciler.models import Action, ActionKind, ShokoFile
from plex_anidb_reconciler.mylist import MyListIndex, SnapshotError, load_mylist, snapshot_age
from plex_anidb_reconciler.paths import PathMatcher
from plex_anidb_reconciler.plex import PlexClient, PlexError
from plex_anidb_reconciler.report import prune_reports, summarize, write_report
from plex_anidb_reconciler.rules import plan
from plex_anidb_reconciler.shoko import ShokoClient, ShokoError

log = logging.getLogger(__name__)
EXECUTABLE_RULES = ("R1", "R2", "R2b")
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
    snapshot: str = "ok"  # ok | stale | unavailable


def build_clients(settings: Settings, http: httpx.Client) -> Clients:
    return Clients(
        ShokoClient(settings.shoko_url, settings.shoko_api_key, settings.shoko_tz, http),
        PlexClient(settings.plex_url, settings.plex_token, http),
    )


def _file_to_json(f: ShokoFile) -> dict:
    return {
        "file_id": f.file_id,
        "fid": f.fid,
        "eids": list(f.eids),
        "manual_link": f.manual_link,
        "abs_paths": list(f.abs_paths),
        "created_at": f.created_at.isoformat() if f.created_at else None,
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


def _shoko_index(settings: Settings, shoko: ShokoClient, now: datetime) -> list[ShokoFile]:
    cache = Path(settings.data_dir) / "shoko_index.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(data["fetched_at"])
        same_server = data.get("shoko_url") == settings.shoko_url  # a cache from another server is never reused
        if same_server and timedelta(0) <= now - fetched < timedelta(minutes=settings.shoko_index_ttl_min):
            return [_file_from_json(d) for d in data["files"]]
    except OSError, ValueError, KeyError, TypeError, AttributeError:
        pass
    files = shoko.list_files()
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "shoko_url": settings.shoko_url,
            "fetched_at": now.isoformat(),
            "files": [_file_to_json(f) for f in files],
        }
        cache.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass
    return files


def _fetch_plex(settings: Settings, plex: PlexClient):
    items = []
    history: dict[str, list[datetime]] = {}
    for section in settings.plex_sections:
        items.extend(plex.list_episodes(section))
        for rk, times in plex.history(section).items():
            history.setdefault(rk, []).extend(times)
    for times in history.values():
        times.sort()
    return items, history


def _new_run_id(now: datetime) -> str:
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _shoko_changed(a: Action, current: ShokoFile) -> bool:
    """The cached index can be stale (ruling 19): only write if Shoko still links this file the same way."""
    return current.fid is None or current.fid != a.fid or current.manual_link or sorted(current.eids) != sorted(a.eids)


def _execute(a: Action, clients: Clients) -> tuple[str, str]:
    try:
        if a.kind is ActionKind.MARK_PLEX_PLAYED:
            if a.rating_key is None:
                return "failed", "invalid_action"
            clients.plex.mark_played(a.rating_key)
        else:
            if a.file_id is None or a.when is None:
                return "failed", "invalid_action"
            if _shoko_changed(a, clients.shoko.get_file(a.file_id)):
                return "failed", "shoko_changed"
            clients.shoko.set_watched(a.file_id, a.when)
    except (ShokoError, PlexError) as exc:
        return "failed", str(exc)
    return "applied", ""


def _selected(a: Action, rules: frozenset[str] | None, file_id: int | None) -> bool:
    return (rules is None or a.rule in rules) and (file_id is None or a.file_id == file_id)


def _finish(rows: list[tuple[Action, str, str]], report_path: Path) -> None:
    write_report(rows, report_path)
    prune_reports(report_path.parent, KEEP_REPORTS)


def run_once(
    settings: Settings,
    *,
    apply: bool,
    limit: int | None,
    now: datetime,
    clients: Clients | None = None,
    rules: frozenset[str] | None = None,
    file_id: int | None = None,
) -> RunResult:
    """rules/file_id restrict which executable actions (R1/R2/R2b) run; reports are always listed.
    Dry runs are uncapped. Only applied/failed writes reach the ledger; everything goes to the CSV."""
    run_id = _new_run_id(now)
    report_path = Path(settings.data_dir) / "reports" / f"{run_id}.csv"

    mylist_path = Path(settings.mylist_path)
    try:
        entries = load_mylist(mylist_path)
        age = snapshot_age(mylist_path, now)
    except SnapshotError, OSError:
        _finish([], report_path)
        print("snapshot_unavailable: no actions planned")
        log.warning("run %s: MyList snapshot unavailable, no actions planned", run_id)
        return RunResult(run_id, {"snapshot_unavailable": 1}, report_path, "unavailable")
    fresh = age < timedelta(hours=settings.snapshot_max_age_h)

    own_http = None
    if clients is None:
        own_http = httpx.Client(timeout=60)
        clients = build_clients(settings, own_http)
    rows: list[tuple[Action, str, str]] = []
    changed = 0
    try:
        ledger = Ledger(Path(settings.data_dir) / "ledger.sqlite")
        try:
            files = _shoko_index(settings, clients.shoko, now)
            items, history = _fetch_plex(settings, clients.plex)
            actions = plan(
                items,
                history,
                PathMatcher(files, settings.path_map),
                MyListIndex(entries),
                settings.r2b_policy,
                fresh,
                r1_marked=ledger.applied_r1_keys(),
                pending=ledger.applied_sets(),
            )
            r1 = [a for a in actions if a.kind is ActionKind.MARK_PLEX_PLAYED]
            sets = [a for a in actions if a.kind is ActionKind.SET_FILE_WATCHED]
            # every R2 before any R2b: an R2b may depend on an R2 of this run (Action.depends_on_file_id)
            sets = [a for a in sets if a.rule != "R2b"] + [a for a in sets if a.rule == "R2b"]
            rows = [(a, "reported", "") for a in actions if a.kind is ActionKind.REPORT]
            executable = [a for a in r1 + sets if _selected(a, rules, file_id)]

            cap = settings.max_writes if limit is None else min(limit, settings.max_writes)
            budget = max(cap, 0)
            set_status: dict[int, str] = {}  # file_id -> status of its SET in this run
            ok = "applied" if apply else "dry_run"
            for a in executable:
                key = a.rating_key if a.kind is ActionKind.MARK_PLEX_PLAYED else a.file_id
                detail = ""
                if ledger.seen_success(a.kind, key):
                    status = "skipped_ledger"
                elif a.depends_on_file_id is not None and set_status.get(a.depends_on_file_id) != ok:
                    status, detail = "skipped", "prerequisite_not_applied"  # CSV only; reconsidered next run
                elif not apply:
                    status = "dry_run"
                elif budget <= 0:
                    status = "capped"
                else:
                    budget -= 1
                    status, detail = _execute(a, clients)
                    changed += detail == "shoko_changed"
                    ledger.record(run_id, a, status, detail)  # committed before the next write
                if a.kind is ActionKind.SET_FILE_WATCHED and a.file_id is not None:
                    set_status[a.file_id] = status
                rows.append((a, status, detail))
        finally:
            ledger.close()
    finally:
        if own_http is not None:
            own_http.close()

    _finish(rows, report_path)
    summary = summarize(rows)
    print(summary)
    counts = dict(Counter(s for _, s, _ in rows))
    if changed:
        counts["shoko_changed"] = changed
    snapshot = "ok" if fresh else "stale"
    log.info("run %s (%s, snapshot %s): %s", run_id, "apply" if apply else "plan", snapshot, counts or "no actions")
    return RunResult(run_id, counts, report_path, snapshot)

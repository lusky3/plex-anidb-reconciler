"""One reconcile pass: load, plan, filter via ledger, apply under a cap, report."""

import json
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
from plex_anidb_reconciler.report import summarize, write_report
from plex_anidb_reconciler.rules import plan
from plex_anidb_reconciler.shoko import ShokoClient, ShokoError


@dataclass
class Clients:
    shoko: ShokoClient
    plex: PlexClient


@dataclass
class RunResult:
    run_id: str
    counts: dict[str, int]
    report_path: Path


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
        if timedelta(0) <= now - fetched < timedelta(minutes=settings.shoko_index_ttl_min):
            return [_file_from_json(d) for d in data["files"]]
    except OSError, ValueError, KeyError, TypeError:
        pass
    files = shoko.list_files()
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_at": now.isoformat(), "files": [_file_to_json(f) for f in files]}
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


def _execute(a: Action, clients: Clients) -> tuple[str, str]:
    try:
        if a.kind is ActionKind.MARK_PLEX_PLAYED:
            assert a.rating_key is not None
            clients.plex.mark_played(a.rating_key)
        else:
            assert a.file_id is not None and a.when is not None
            clients.shoko.set_watched(a.file_id, a.when)
    except (ShokoError, PlexError) as exc:
        return "failed", str(exc)
    return "applied", ""


def run_once(
    settings: Settings,
    *,
    apply: bool,
    limit: int | None,
    now: datetime,
    clients: Clients | None = None,
) -> RunResult:
    run_id = _new_run_id(now)
    report_path = Path(settings.data_dir) / "reports" / f"{run_id}.csv"

    mylist_path = Path(settings.mylist_path)
    try:
        entries = load_mylist(mylist_path)
        age = snapshot_age(mylist_path, now)
    except SnapshotError, OSError:
        write_report([], report_path)
        print("snapshot_unavailable: no actions planned")
        return RunResult(run_id, {"snapshot_unavailable": 1}, report_path)
    fresh = age < timedelta(hours=settings.snapshot_max_age_h)

    own_http = None
    if clients is None:
        own_http = httpx.Client(timeout=60)
        clients = build_clients(settings, own_http)
    try:
        files = _shoko_index(settings, clients.shoko, now)
        items, history = _fetch_plex(settings, clients.plex)
        actions = plan(
            items, history, PathMatcher(files, settings.path_map), MyListIndex(entries), settings.r2b_policy, fresh
        )
        r1 = [a for a in actions if a.kind is ActionKind.MARK_PLEX_PLAYED]
        sets = [a for a in actions if a.kind is ActionKind.SET_FILE_WATCHED]
        reports = [a for a in actions if a.kind is ActionKind.REPORT]

        cap = settings.max_writes if limit is None else min(limit, settings.max_writes)
        rows: list[tuple[Action, str]] = []
        ledger = Ledger(Path(settings.data_dir) / "ledger.sqlite")
        try:
            for a in reports:
                ledger.record(run_id, a, "reported")
                rows.append((a, "reported"))
            budget = max(cap, 0)
            for a in r1 + sets:
                key = a.rating_key if a.kind is ActionKind.MARK_PLEX_PLAYED else a.file_id
                if ledger.seen_success(a.kind, key):
                    status, detail = "skipped_ledger", ""
                elif budget <= 0:
                    status, detail = "capped", ""
                else:
                    budget -= 1
                    status, detail = _execute(a, clients) if apply else ("dry_run", "")
                ledger.record(run_id, a, status, detail)
                rows.append((a, status))
        finally:
            ledger.close()
    finally:
        if own_http is not None:
            own_http.close()

    write_report(rows, report_path)
    print(summarize(rows))
    return RunResult(run_id, dict(Counter(s for _, s in rows)), report_path)

"""Pure decision engine: snapshots in, actions out. No I/O, no clock, no logging."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from plex_anidb_reconciler.models import Action, ActionKind, PendingWatch, PlexItem, ShokoFile
from plex_anidb_reconciler.mylist import MyListIndex
from plex_anidb_reconciler.paths import Ambiguous, Match, PathMatcher

R2B_POLICIES = ("record", "ignore")


class _Watched:
    """The MyList snapshot plus watches the reconciler has written (ledger) or is about to write
    (this plan) that the snapshot cannot show yet. Pending fids count as watched; pending eids count
    as watched on their pending dates, so later files of those episodes go through the gated R2b path."""

    def __init__(
        self, mylist: MyListIndex, pending: Iterable[PendingWatch], planned: Iterable[PendingWatch] = ()
    ) -> None:
        self._ml = mylist
        self._fids: set[int] = set()
        self._views: dict[int, list[datetime]] = defaultdict(list)
        self._planned = list(planned)  # SETs planned in this run (not yet written)
        for p in [*pending, *self._planned]:
            if p.fid:
                self._fids.add(p.fid)
            for e in p.eids:
                self._views[e].append(p.when)
        self._watched_eids = frozenset(self._ml.watched_eids() | set(self._views))

    def has_fid(self, fid: int | None) -> bool:
        return self._ml.has_fid(fid)

    def eids_for_fid(self, fid: int | None) -> frozenset[int]:
        return self._ml.eids_for_fid(fid)

    def fid_watched(self, fid: int | None) -> bool:
        return self._ml.fid_watched(fid) or (bool(fid) and fid in self._fids)

    def watched_eids(self) -> frozenset[int]:
        return self._watched_eids

    def planned_prerequisites(self, eids: Iterable[int]) -> set[int]:
        """File ids of SETs planned in this run that cover any of these eids. A planned R2 only exists
        for eids unwatched in the snapshot and ledger, so each one is needed for the eids to count."""
        wanted = set(eids)
        return {p.file_id for p in self._planned if wanted.intersection(p.eids)}

    def max_watch(self, eids: Iterable[int]) -> datetime | None:
        eids = list(eids)
        dates = [d for e in eids for d in self._views.get(e, [])]
        snap = self._ml.max_watch(eids)
        if snap is not None:
            dates.append(snap)
        return max(dates) if dates else None


def _report(rule: str, reason: str, rk: str, f: ShokoFile | None = None, when: datetime | None = None) -> Action:
    return Action(
        ActionKind.REPORT,
        rule,
        rk,
        f.file_id if f else None,
        f.fid if f else None,
        f.eids if f else (),
        when,
        reason,
    )


def _set(
    rule: str, reason: str, rk: str, f: ShokoFile, when: datetime, path: str | None, depends_on: int | None = None
) -> Action:
    return Action(ActionKind.SET_FILE_WATCHED, rule, rk, f.file_id, f.fid, f.eids, when, reason, depends_on, path)


def _resolve(item: PlexItem, matcher: PathMatcher) -> list[Match] | tuple[Action, ...]:
    """The Shoko files behind a Plex item, or the R4 reports explaining why there are none usable.
    Independent of watch state, so plan() resolves each item once."""
    rk = item.rating_key
    found = [matcher.lookup(p) for p in item.paths]
    if any(m is None for m in found):
        return (_report("R4", "no_shoko_match", rk),)
    if any(isinstance(m, Ambiguous) for m in found):
        return (_report("R4", "ambiguous_path", rk),)
    matches: list[Match] = [m for m in found if isinstance(m, Match)]
    fallback = {m.file.file_id: m.file for m in matches if m.path is None}
    if fallback:
        # parent-dir + filename only: may be another show's same-named episode, so never act on it
        return tuple(_report("R4", "fallback_match", rk, f) for f in fallback.values())
    files = [m.file for m in matches]
    if not files or any(f.fid is None for f in files):
        return (_report("R4", "unrecognised", rk),)
    if any(f.manual_link for f in files):
        return (_report("R4", "manual_link", rk),)
    if any(not f.eids for f in files):
        return (_report("R4", "no_episodes", rk),)
    return matches


def _eid_mismatch(f: ShokoFile, mylist: _Watched) -> bool:
    anidb_eids = mylist.eids_for_fid(f.fid)
    return bool(anidb_eids) and anidb_eids != set(f.eids)


@dataclass(frozen=True)
class _Ctx:
    """Per-pass inputs of _plan_item. r1_state drives R1 (snapshot + applied writes only); mylist drives
    R2/R2b and also includes SETs planned earlier in this same plan() call."""

    history: dict[str, list[datetime]]
    r1_state: _Watched
    mylist: _Watched
    r2b_policy: str
    snapshot_fresh: bool
    r1_marked: frozenset[str]


def _plan_r1(rk: str, eids: tuple[int, ...], r1_state: _Watched) -> list[Action]:
    r1_watched = r1_state.watched_eids()
    if all(e in r1_watched for e in eids):
        return [Action(ActionKind.MARK_PLEX_PLAYED, "R1", rk, None, None, eids, None, "anidb_episode_watched")]
    if any(e in r1_watched for e in eids):
        return [_report("R4", "partial_episode", rk)]
    return []


def _plan_r2(item: PlexItem, f: ShokoFile, path: str | None, plays_all: list[datetime], ctx: _Ctx) -> Action:
    rk = item.rating_key
    if plays_all:
        when = min(plays_all)
    elif rk in ctx.r1_marked:
        # lastViewedAt may be our own R1 scrobble, not a real event for this file (ruling 15)
        return _report("R4", "plex_marked_by_reconciler", rk, f)
    else:
        when = item.last_viewed_at
    if when is None:
        return _report("R4", "no_date", rk, f)
    if not ctx.snapshot_fresh:
        return _report("R4", "stale_snapshot", rk, f, when)
    return _set("R2", "new_watch", rk, f, when, path)


def _plan_r2b(rk: str, f: ShokoFile, path: str | None, plays_all: list[datetime], ctx: _Ctx) -> Action:
    if f.created_at is None:
        return _report("R2b", "unknown_file_age", rk, f)
    if ctx.r2b_policy != "record":
        return _report("R2b", "ignore_policy", rk, f)
    cutoff = ctx.mylist.max_watch(f.eids)
    plays = [t for t in plays_all if cutoff is not None and t > cutoff and t > f.created_at]
    if not plays:
        return _report("R2b", "no_playback", rk, f)
    if not ctx.snapshot_fresh:
        return _report("R2b", "stale_snapshot", rk, f, min(plays))
    prereqs = ctx.mylist.planned_prerequisites(f.eids)
    if len(prereqs) > 1:
        # would need several R2s of this run to land first: decide next run, once they are written
        return _report("R2b", "multiple_prerequisites", rk, f, min(plays))
    dep = next(iter(prereqs)) if prereqs else None
    return _set("R2b", "rewatch_new_release", rk, f, min(plays), path, dep)


def _plan_file(item: PlexItem, f: ShokoFile, path: str | None, ctx: _Ctx) -> Action | None:
    rk = item.rating_key
    if ctx.mylist.fid_watched(f.fid):
        return None
    if not ctx.mylist.has_fid(f.fid):
        return _report("R4", "not_in_snapshot", rk, f)
    watched = ctx.mylist.watched_eids()
    watched_f = [e for e in f.eids if e in watched]
    plays_all = ctx.history.get(rk) or []
    if not watched_f:
        return _plan_r2(item, f, path, plays_all, ctx)
    if len(watched_f) < len(f.eids):
        return _report("R4", "partial_episode", rk, f)
    return _plan_r2b(rk, f, path, plays_all, ctx)


def _plan_item(item: PlexItem, resolved: list[Match] | tuple[Action, ...], ctx: _Ctx) -> list[Action]:
    if isinstance(resolved, tuple):
        return list(resolved)
    rk = item.rating_key
    files = [m.file for m in resolved]
    matched: dict[int, str | None] = {}
    for m in resolved:
        matched.setdefault(m.file.file_id, m.path)

    # AniDB and Shoko link a file to different episodes: never write, not even R1 (spec R4)
    mismatched = {f.file_id: f for f in files if _eid_mismatch(f, ctx.mylist)}
    if mismatched:
        return [_report("R4", "eid_mismatch", rk, f) for f in mismatched.values()]
    if item.view_count == 0:
        return _plan_r1(rk, tuple(sorted({e for f in files for e in f.eids})), ctx.r1_state)
    distinct = {f.file_id: f for f in files}
    if len(distinct) > 1:
        return [_report("R4", "multi_version", rk, f) for f in distinct.values()]
    planned = (_plan_file(item, f, matched[f.file_id], ctx) for f in files)
    return [a for a in planned if a is not None]


def _sort_key(a: Action) -> tuple:
    return (a.rating_key or "", a.file_id is not None, a.file_id or 0, a.rule, a.reason)


def _keep_earliest_set(best_set: dict[int, Action], a: Action) -> None:
    if a.file_id is None or a.when is None:
        raise ValueError("SET action without file_id or date")
    cur = best_set.get(a.file_id)
    # earliest date wins; equal dates go to the smallest rating key (independent of input order)
    if cur is None or cur.when is None or (a.when, a.rating_key or "") < (cur.when, cur.rating_key or ""):
        best_set[a.file_id] = a


def _merge(actions: list[Action]) -> tuple[list[Action], dict[int, Action]]:
    """One SET per file_id (earliest date), one R1 per rating_key."""
    kept: list[Action] = []
    best_set: dict[int, Action] = {}
    seen_r1: set[str | None] = set()
    for a in actions:
        if a.kind is ActionKind.SET_FILE_WATCHED:
            _keep_earliest_set(best_set, a)
        elif a.kind is not ActionKind.MARK_PLEX_PLAYED:
            kept.append(a)
        elif a.rating_key not in seen_r1:
            seen_r1.add(a.rating_key)
            kept.append(a)
    return kept, best_set


def _r2_conflict_winners(sets: Iterable[Action]) -> list[Action]:
    """R2 SETs of different files that share an eid conflict. Conflicts linked through shared eids form
    one group; the earliest SET of each group wins. Groups cannot affect each other (a winner only marks
    its own eids watched, and every R2 sharing one of them is in its group), so settling all groups in
    one pass gives the same result as settling one conflict per pass."""
    r2 = [a for a in sets if a.rule == "R2"]
    files_by_eid: dict[int, set[int | None]] = defaultdict(set)
    for a in r2:
        for e in a.eids:
            files_by_eid[e].add(a.file_id)
    conflicted = [a for a in r2 if any(len(files_by_eid[e]) > 1 for e in a.eids)]
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in conflicted:  # union each SET's eids into one group
        root = find(a.eids[0])
        for e in a.eids[1:]:
            parent[find(e)] = root
    best: dict[int, Action] = {}
    for a in conflicted:
        g = find(a.eids[0])
        if g not in best or (a.when, a.file_id or 0) < (best[g].when, best[g].file_id or 0):
            best[g] = a
    return sorted(best.values(), key=lambda a: (a.when, a.file_id or 0))


def plan(
    items: list[PlexItem],
    history: dict[str, list[datetime]],
    matcher: PathMatcher,
    mylist: MyListIndex,
    r2b_policy: str,
    snapshot_fresh: bool,
    *,
    r1_marked: frozenset[str] = frozenset(),
    pending: Iterable[PendingWatch] = (),
) -> list[Action]:
    """r1_marked: rating keys the reconciler already marked played (R1); their lastViewedAt is never
    used as a date. pending: SETs already applied that the snapshot may not show yet."""
    if r2b_policy not in R2B_POLICIES:
        raise ValueError("r2b_policy must be 'record' or 'ignore'")
    pending = list(pending)
    r1_state = _Watched(mylist, pending)
    resolved = [(item, _resolve(item, matcher)) for item in items]
    committed: list[Action] = []
    while True:
        planned = [PendingWatch(a.file_id or 0, a.fid, a.eids, a.when) for a in committed if a.when is not None]
        ctx = _Ctx(history, r1_state, _Watched(mylist, pending, planned), r2b_policy, snapshot_fresh, r1_marked)
        actions: list[Action] = []
        for item, res in resolved:
            actions.extend(_plan_item(item, res, ctx))
        kept, best_set = _merge(actions)
        # Two different files of one episode must not both get R2 in one run: keep the earliest as R2,
        # treat it as written and re-plan, so the others go through the gated R2b path (ruling 16).
        winners = _r2_conflict_winners(best_set.values())
        if not winners:
            break
        committed.extend(winners)
    kept.extend(best_set.values())
    kept.extend(committed)
    return sorted(kept, key=_sort_key)

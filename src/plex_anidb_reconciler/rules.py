"""Pure decision engine: snapshots in, actions out. No I/O, no clock, no logging."""

from collections import defaultdict
from collections.abc import Iterable
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


def _set(rule: str, reason: str, rk: str, f: ShokoFile, when: datetime, depends_on: int | None = None) -> Action:
    return Action(ActionKind.SET_FILE_WATCHED, rule, rk, f.file_id, f.fid, f.eids, when, reason, depends_on)


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


def _plan_item(
    item: PlexItem,
    resolved: list[Match] | tuple[Action, ...],
    history: dict[str, list[datetime]],
    r1_state: _Watched,
    mylist: _Watched,
    r2b_policy: str,
    snapshot_fresh: bool,
    r1_marked: frozenset[str],
) -> list[Action]:
    """r1_state drives R1 (snapshot + applied writes only); mylist drives R2/R2b and also includes
    SETs planned earlier in this same plan() call."""
    if isinstance(resolved, tuple):
        return list(resolved)
    rk = item.rating_key
    files = [m.file for m in resolved]
    eids = tuple(sorted({e for f in files for e in f.eids}))

    if item.view_count == 0:
        r1_watched = r1_state.watched_eids()
        if all(e in r1_watched for e in eids):
            return [Action(ActionKind.MARK_PLEX_PLAYED, "R1", rk, None, None, eids, None, "anidb_episode_watched")]
        if any(e in r1_watched for e in eids):
            return [_report("R4", "partial_episode", rk)]
        return []

    watched = mylist.watched_eids()

    distinct = {f.file_id: f for f in files}
    if len(distinct) > 1:
        return [_report("R4", "multi_version", rk, f) for f in distinct.values()]

    out: list[Action] = []
    plays_all = history.get(rk) or []
    for f in files:
        if mylist.fid_watched(f.fid):
            continue
        if not mylist.has_fid(f.fid):
            out.append(_report("R4", "not_in_snapshot", rk, f))
            continue
        watched_f = [e for e in f.eids if e in watched]
        if watched_f and len(watched_f) < len(f.eids):
            out.append(_report("R4", "partial_episode", rk, f))
            continue
        anidb_eids = mylist.eids_for_fid(f.fid)
        if anidb_eids and anidb_eids != set(f.eids):
            # AniDB and Shoko link this file to different episodes: never write (spec R4)
            out.append(_report("R4", "eid_mismatch", rk, f))
            continue
        if not watched_f:
            if plays_all:
                when = min(plays_all)
            elif rk in r1_marked:
                # lastViewedAt may be our own R1 scrobble, not a real event for this file (ruling 15)
                out.append(_report("R4", "plex_marked_by_reconciler", rk, f))
                continue
            else:
                when = item.last_viewed_at
            if when is None:
                out.append(_report("R4", "no_date", rk, f))
            elif not snapshot_fresh:
                out.append(_report("R4", "stale_snapshot", rk, f, when))
            else:
                out.append(_set("R2", "new_watch", rk, f, when))
        else:
            cutoff = mylist.max_watch(f.eids)
            if f.created_at is None:
                out.append(_report("R2b", "unknown_file_age", rk, f))
                continue
            plays = [t for t in plays_all if cutoff is not None and t > cutoff and t > f.created_at]
            prereqs = mylist.planned_prerequisites(f.eids)
            if r2b_policy == "record" and plays and snapshot_fresh and len(prereqs) > 1:
                # would need several R2s of this run to land first: decide next run, once they are written
                out.append(_report("R2b", "multiple_prerequisites", rk, f, min(plays)))
            elif r2b_policy == "record" and plays and snapshot_fresh:
                dep = next(iter(prereqs)) if prereqs else None
                out.append(_set("R2b", "rewatch_new_release", rk, f, min(plays), dep))
            elif r2b_policy != "record":
                out.append(_report("R2b", "ignore_policy", rk, f))
            elif not plays:
                out.append(_report("R2b", "no_playback", rk, f))
            else:
                out.append(_report("R2b", "stale_snapshot", rk, f, min(plays)))
    return out


def _sort_key(a: Action) -> tuple:
    return (a.rating_key or "", a.file_id is not None, a.file_id or 0, a.rule, a.reason)


def _merge(actions: list[Action]) -> tuple[list[Action], dict[int, Action]]:
    """One SET per file_id (earliest date), one R1 per rating_key."""
    kept: list[Action] = []
    best_set: dict[int, Action] = {}
    seen_r1: set[str | None] = set()
    for a in actions:
        if a.kind is ActionKind.SET_FILE_WATCHED:
            if a.file_id is None or a.when is None:
                raise ValueError("SET action without file_id or date")
            cur = best_set.get(a.file_id)
            # earliest date wins; equal dates go to the smallest rating key (independent of input order)
            if cur is None or cur.when is None or (a.when, a.rating_key or "") < (cur.when, cur.rating_key or ""):
                best_set[a.file_id] = a
        elif a.kind is ActionKind.MARK_PLEX_PLAYED:
            if a.rating_key not in seen_r1:
                seen_r1.add(a.rating_key)
                kept.append(a)
        else:
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
        state = _Watched(mylist, pending, planned)
        actions: list[Action] = []
        for item, res in resolved:
            actions.extend(_plan_item(item, res, history, r1_state, state, r2b_policy, snapshot_fresh, r1_marked))
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

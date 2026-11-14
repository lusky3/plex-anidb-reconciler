"""Pure decision engine: snapshots in, actions out. No I/O, no clock, no logging."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from plex_anidb_reconciler.models import Action, ActionKind, PendingWatch, PlexItem, R2bPolicy, Rule, ShokoFile
from plex_anidb_reconciler.mylist import MyListIndex
from plex_anidb_reconciler.paths import Ambiguous, Match, PathMatcher


class WatchState:
    """The MyList snapshot plus watches the reconciler has written (ledger) or is about to write
    (this plan) that the snapshot cannot show yet. Pending fids count as watched; pending eids count
    as watched on their pending dates, so later files of those episodes go through the gated R2b path.
    Questions only the snapshot can answer (is a fid in MyList, which eids AniDB links it to) go to
    the MyListIndex directly."""

    def __init__(
        self, snapshot: MyListIndex, pending: Iterable[PendingWatch], planned: Iterable[PendingWatch] = ()
    ) -> None:
        self._snapshot = snapshot
        self._fids: set[int] = set()
        self._views: dict[int, list[datetime]] = defaultdict(list)
        self._planned = list(planned)  # SETs planned in this run (not yet written)
        for watch in [*pending, *self._planned]:
            if watch.fid:
                self._fids.add(watch.fid)
            for eid in watch.eids:
                self._views[eid].append(watch.when)
        self._watched_eids = frozenset(snapshot.watched_eids() | set(self._views))

    def fid_watched(self, fid: int | None) -> bool:
        return self._snapshot.fid_watched(fid) or (bool(fid) and fid in self._fids)

    def watched_eids(self) -> frozenset[int]:
        return self._watched_eids

    def planned_prerequisites(self, eids: Iterable[int]) -> set[int]:
        """File ids of SETs planned in this run that cover any of these eids. A planned R2 only exists
        for eids unwatched in the snapshot and ledger, so each one is needed for the eids to count."""
        wanted = set(eids)
        return {watch.file_id for watch in self._planned if wanted.intersection(watch.eids)}

    def max_watch(self, eids: Iterable[int]) -> datetime | None:
        eids = list(eids)
        dates = [d for e in eids for d in self._views.get(e, [])]
        latest_in_snapshot = self._snapshot.max_watch(eids)
        if latest_in_snapshot is not None:
            dates.append(latest_in_snapshot)
        return max(dates) if dates else None


def _report(
    rule: Rule, reason: str, rating_key: str, shoko_file: ShokoFile | None = None, when: datetime | None = None
) -> Action:
    return Action(
        kind=ActionKind.REPORT,
        rule=rule,
        rating_key=rating_key,
        file_id=shoko_file.file_id if shoko_file else None,
        fid=shoko_file.fid if shoko_file else None,
        eids=shoko_file.eids if shoko_file else (),
        when=when,
        reason=reason,
        aid=shoko_file.aid if shoko_file else None,
        release_group=shoko_file.release_group if shoko_file else None,
    )


def _set(
    rule: Rule,
    reason: str,
    rating_key: str,
    shoko_file: ShokoFile,
    *,
    when: datetime,
    matched_path: str | None,
    depends_on: int | None = None,
) -> Action:
    return Action(
        kind=ActionKind.SET_FILE_WATCHED,
        rule=rule,
        rating_key=rating_key,
        file_id=shoko_file.file_id,
        fid=shoko_file.fid,
        eids=shoko_file.eids,
        when=when,
        reason=reason,
        depends_on_file_id=depends_on,
        matched_path=matched_path,
        aid=shoko_file.aid,
        release_group=shoko_file.release_group,
    )


def _resolve(item: PlexItem, matcher: PathMatcher) -> list[Match] | tuple[Action, ...]:
    """The Shoko files behind a Plex item, or the R4 reports explaining why there are none usable.
    Independent of watch state, so plan() resolves each item once."""
    rating_key = item.rating_key
    found = [matcher.lookup(p) for p in item.paths]
    if any(m is None for m in found):
        return (_report(Rule.R4, "no_shoko_match", rating_key),)
    if any(isinstance(m, Ambiguous) for m in found):
        return (_report(Rule.R4, "ambiguous_path", rating_key),)
    matches: list[Match] = [m for m in found if isinstance(m, Match)]
    fallback = {m.file.file_id: m.file for m in matches if m.path is None}
    if fallback:
        # parent-dir + filename only: may be another show's same-named episode, so never act on it
        return tuple(_report(Rule.R4, "fallback_match", rating_key, f) for f in fallback.values())
    files = [m.file for m in matches]
    if not files or any(f.fid is None for f in files):
        return (_report(Rule.R4, "unrecognised", rating_key),)
    if any(f.manual_link for f in files):
        return (_report(Rule.R4, "manual_link", rating_key),)
    if any(not f.eids for f in files):
        return (_report(Rule.R4, "no_episodes", rating_key),)
    return matches


def _eid_mismatch(shoko_file: ShokoFile, snapshot: MyListIndex) -> bool:
    anidb_eids = snapshot.eids_for_fid(shoko_file.fid)
    return bool(anidb_eids) and anidb_eids != set(shoko_file.eids)


@dataclass(frozen=True)
class PlanContext:
    """Per-pass inputs of _plan_item.

    watch_state: snapshot + writes already applied (ledger); drives R1, which must only follow
    watches that really exist. watch_state_with_planned: additionally counts the SETs planned
    earlier in this same plan() call; drives R2/R2b, so a second file of an episode is gated as a
    re-watch instead of getting its own R2."""

    history: dict[str, list[datetime]]
    snapshot: MyListIndex
    watch_state: WatchState
    watch_state_with_planned: WatchState
    r2b_policy: R2bPolicy
    snapshot_fresh: bool
    r1_marked: frozenset[str]


def _plan_r1(rating_key: str, eids: tuple[int, ...], watch_state: WatchState) -> list[Action]:
    watched = watch_state.watched_eids()
    if all(e in watched for e in eids):
        mark = Action(
            kind=ActionKind.MARK_PLEX_PLAYED,
            rule=Rule.R1,
            rating_key=rating_key,
            file_id=None,
            fid=None,
            eids=eids,
            when=None,
            reason="anidb_episode_watched",
        )
        return [mark]
    if any(e in watched for e in eids):
        return [_report(Rule.R4, "partial_episode", rating_key)]
    return []


def _plan_r2(
    item: PlexItem, shoko_file: ShokoFile, path: str | None, plays: list[datetime], ctx: PlanContext
) -> Action:
    rating_key = item.rating_key
    if plays:
        when = min(plays)
    elif rating_key in ctx.r1_marked:
        # Without a history row the only date is Plex lastViewedAt, and for an item the reconciler
        # marked played itself (R1) that is the reconciler's own scrobble time, not a real watch of
        # this file: never send it to AniDB.
        return _report(Rule.R4, "plex_marked_by_reconciler", rating_key, shoko_file)
    else:
        when = item.last_viewed_at
    if when is None:
        return _report(Rule.R4, "no_date", rating_key, shoko_file)
    if not ctx.snapshot_fresh:
        return _report(Rule.R4, "stale_snapshot", rating_key, shoko_file, when)
    return _set(Rule.R2, "new_watch", rating_key, shoko_file, when=when, matched_path=path)


def _plan_r2b(
    rating_key: str, shoko_file: ShokoFile, path: str | None, plays_all: list[datetime], ctx: PlanContext
) -> Action:
    if shoko_file.created_at is None:
        return _report(Rule.R2B, "unknown_file_age", rating_key, shoko_file)
    if ctx.r2b_policy is not R2bPolicy.RECORD:
        return _report(Rule.R2B, "ignore_policy", rating_key, shoko_file)
    cutoff = ctx.watch_state_with_planned.max_watch(shoko_file.eids)
    plays = [t for t in plays_all if cutoff is not None and t > cutoff and t > shoko_file.created_at]
    if not plays:
        return _report(Rule.R2B, "no_playback", rating_key, shoko_file)
    if not ctx.snapshot_fresh:
        return _report(Rule.R2B, "stale_snapshot", rating_key, shoko_file, min(plays))
    prereqs = ctx.watch_state_with_planned.planned_prerequisites(shoko_file.eids)
    if len(prereqs) > 1:
        # would need several R2s of this run to land first: decide next run, once they are written
        return _report(Rule.R2B, "multiple_prerequisites", rating_key, shoko_file, min(plays))
    prerequisite = next(iter(prereqs)) if prereqs else None
    return _set(
        Rule.R2B,
        "rewatch_new_release",
        rating_key,
        shoko_file,
        when=min(plays),
        matched_path=path,
        depends_on=prerequisite,
    )


def _plan_file(item: PlexItem, shoko_file: ShokoFile, path: str | None, ctx: PlanContext) -> Action | None:
    rating_key = item.rating_key
    state = ctx.watch_state_with_planned
    if state.fid_watched(shoko_file.fid):
        return None
    if not ctx.snapshot.has_fid(shoko_file.fid):
        return _report(Rule.R4, "not_in_snapshot", rating_key, shoko_file)
    watched = state.watched_eids()
    watched_of_file = [e for e in shoko_file.eids if e in watched]
    plays_all = ctx.history.get(rating_key) or []
    if not watched_of_file:
        return _plan_r2(item, shoko_file, path, plays_all, ctx)
    if len(watched_of_file) < len(shoko_file.eids):
        return _report(Rule.R4, "partial_episode", rating_key, shoko_file)
    return _plan_r2b(rating_key, shoko_file, path, plays_all, ctx)


def _plan_item(item: PlexItem, resolution: list[Match] | tuple[Action, ...], ctx: PlanContext) -> list[Action]:
    if isinstance(resolution, tuple):
        return list(resolution)
    rating_key = item.rating_key
    files = [m.file for m in resolution]
    matched: dict[int, str | None] = {}
    for m in resolution:
        matched.setdefault(m.file.file_id, m.path)

    # AniDB and Shoko link a file to different episodes: we cannot tell which is right, so nothing is
    # written for this item, not even the Plex-only R1
    mismatched = {f.file_id: f for f in files if _eid_mismatch(f, ctx.snapshot)}
    if mismatched:
        return [_report(Rule.R4, "eid_mismatch", rating_key, f) for f in mismatched.values()]
    if item.view_count == 0:
        return _plan_r1(rating_key, tuple(sorted({e for f in files for e in f.eids})), ctx.watch_state)
    distinct = {f.file_id: f for f in files}
    if len(distinct) > 1:
        return [_report(Rule.R4, "multi_version", rating_key, f) for f in distinct.values()]
    planned = (_plan_file(item, f, matched[f.file_id], ctx) for f in files)
    return [action for action in planned if action is not None]


def _sort_key(action: Action) -> tuple:
    return (action.rating_key or "", action.file_id is not None, action.file_id or 0, action.rule, action.reason)


def _keep_earliest_set(best_set: dict[int, Action], action: Action) -> None:
    if action.file_id is None or action.when is None:
        raise ValueError("SET action without file_id or date")
    current = best_set.get(action.file_id)
    # earliest date wins; equal dates go to the smallest rating key (independent of input order)
    if (
        current is None
        or current.when is None
        or (action.when, action.rating_key or "") < (current.when, current.rating_key or "")
    ):
        best_set[action.file_id] = action


def _merge(actions: list[Action]) -> tuple[list[Action], dict[int, Action]]:
    """One SET per file_id (earliest date), one R1 per rating_key."""
    kept: list[Action] = []
    best_set: dict[int, Action] = {}
    seen_r1: set[str | None] = set()
    for action in actions:
        if action.kind is ActionKind.SET_FILE_WATCHED:
            _keep_earliest_set(best_set, action)
        elif action.kind is not ActionKind.MARK_PLEX_PLAYED:
            kept.append(action)
        elif action.rating_key not in seen_r1:
            seen_r1.add(action.rating_key)
            kept.append(action)
    return kept, best_set


def _r2_conflict_winners(sets: Iterable[Action]) -> list[Action]:
    """R2 SETs of different files that share an eid conflict. Conflicts linked through shared eids form
    one group; the earliest SET of each group wins. Groups cannot affect each other (a winner only marks
    its own eids watched, and every R2 sharing one of them is in its group), so settling all groups in
    one pass gives the same result as settling one conflict per pass."""
    r2 = [a for a in sets if a.rule is Rule.R2]
    files_by_eid: dict[int, set[int | None]] = defaultdict(set)
    for action in r2:
        for e in action.eids:
            files_by_eid[e].add(action.file_id)
    conflicted = [a for a in r2 if any(len(files_by_eid[e]) > 1 for e in a.eids)]
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for action in conflicted:  # union each SET's eids into one group
        root = find(action.eids[0])
        for e in action.eids[1:]:
            parent[find(e)] = root
    best: dict[int, Action] = {}
    for action in conflicted:
        group = find(action.eids[0])
        if group not in best or (action.when, action.file_id or 0) < (best[group].when, best[group].file_id or 0):
            best[group] = action
    return sorted(best.values(), key=lambda a: (a.when, a.file_id or 0))


def plan(
    items: list[PlexItem],
    history: dict[str, list[datetime]],
    matcher: PathMatcher,
    mylist: MyListIndex,
    r2b_policy: R2bPolicy | str,
    snapshot_fresh: bool,
    *,
    r1_marked: frozenset[str] = frozenset(),
    pending: Iterable[PendingWatch] = (),
) -> list[Action]:
    """r1_marked: rating keys the reconciler already marked played (R1); their lastViewedAt is never
    used as a date. pending: SETs already applied that the snapshot may not show yet."""
    try:
        policy = R2bPolicy(r2b_policy)
    except ValueError:
        raise ValueError("r2b_policy must be 'record' or 'ignore'") from None
    pending = list(pending)
    watch_state = WatchState(mylist, pending)
    resolved = [(item, _resolve(item, matcher)) for item in items]
    committed: list[Action] = []
    while True:
        planned = [PendingWatch(a.file_id or 0, a.fid, a.eids, a.when) for a in committed if a.when is not None]
        ctx = PlanContext(
            history=history,
            snapshot=mylist,
            watch_state=watch_state,
            watch_state_with_planned=WatchState(mylist, pending, planned),
            r2b_policy=policy,
            snapshot_fresh=snapshot_fresh,
            r1_marked=r1_marked,
        )
        actions: list[Action] = []
        for item, resolution in resolved:
            actions.extend(_plan_item(item, resolution, ctx))
        kept, best_set = _merge(actions)
        # Two different files of one episode must not both get R2 in one run: Plex history cannot tell
        # which file was played, so both would claim the same first-watch date. Keep the earliest as R2,
        # treat it as written and re-plan, so the others go through the gated R2b path (only plays after
        # that date and after the file's import count).
        winners = _r2_conflict_winners(best_set.values())
        if not winners:
            break
        committed.extend(winners)
    kept.extend(best_set.values())
    kept.extend(committed)
    return sorted(kept, key=_sort_key)

"""Pure decision engine: snapshots in, actions out. No I/O, no clock, no logging."""

from datetime import datetime

from plex_anidb_reconciler.models import Action, ActionKind, PlexItem, ShokoFile
from plex_anidb_reconciler.mylist import MyListIndex
from plex_anidb_reconciler.paths import Ambiguous, PathMatcher


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


def _set(rule: str, reason: str, rk: str, f: ShokoFile, when: datetime) -> Action:
    return Action(ActionKind.SET_FILE_WATCHED, rule, rk, f.file_id, f.fid, f.eids, when, reason)


def _plan_item(
    item: PlexItem,
    history: dict[str, list[datetime]],
    matcher: PathMatcher,
    mylist: MyListIndex,
    r2b_policy: str,
    snapshot_fresh: bool,
) -> list[Action]:
    rk = item.rating_key
    matches = [matcher.match(p) for p in item.paths]
    if any(m is None for m in matches):
        return [_report("R4", "no_shoko_match", rk)]
    if any(isinstance(m, Ambiguous) for m in matches):
        return [_report("R4", "ambiguous_path", rk)]
    files: list[ShokoFile] = [m for m in matches if isinstance(m, ShokoFile)]
    if not files or any(f.fid is None or not f.eids for f in files):
        return [_report("R4", "unrecognised", rk)]
    if any(f.manual_link for f in files):
        return [_report("R4", "manual_link", rk)]

    watched = mylist.watched_eids()
    eids = tuple(sorted({e for f in files for e in f.eids}))

    if item.view_count == 0:
        if all(e in watched for e in eids):
            return [Action(ActionKind.MARK_PLEX_PLAYED, "R1", rk, None, None, eids, None, "anidb_episode_watched")]
        if any(e in watched for e in eids):
            return [_report("R4", "partial_episode", rk)]
        return []

    out: list[Action] = []
    plays_all = history.get(rk) or []
    for f in files:
        if mylist.fid_watched(f.fid):
            continue
        watched_f = [e for e in f.eids if e in watched]
        if not watched_f:
            when = min(plays_all) if plays_all else item.last_viewed_at
            if when is None:
                out.append(_report("R4", "no_date", rk, f))
            elif not snapshot_fresh:
                out.append(_report("R4", "stale_snapshot", rk, f, when))
            else:
                out.append(_set("R2", "new_watch", rk, f, when))
        elif len(watched_f) < len(f.eids):
            out.append(_report("R4", "partial_episode", rk, f))
        else:
            cutoff = mylist.max_watch(f.eids)
            plays = [t for t in plays_all if cutoff is not None and t > cutoff]
            if r2b_policy == "record" and plays and snapshot_fresh:
                out.append(_set("R2b", "rewatch_new_release", rk, f, min(plays)))
            elif r2b_policy != "record":
                out.append(_report("R2b", "ignore_policy", rk, f))
            elif not plays:
                out.append(_report("R2b", "no_playback", rk, f))
            else:
                out.append(_report("R2b", "stale_snapshot", rk, f, min(plays)))
    return out


def _sort_key(a: Action) -> tuple:
    return (a.rating_key or "", a.file_id is not None, a.file_id or 0, a.rule, a.reason)


def plan(
    items: list[PlexItem],
    history: dict[str, list[datetime]],
    matcher: PathMatcher,
    mylist: MyListIndex,
    r2b_policy: str,
    snapshot_fresh: bool,
) -> list[Action]:
    actions: list[Action] = []
    for item in items:
        actions.extend(_plan_item(item, history, matcher, mylist, r2b_policy, snapshot_fresh))

    # Post-pass: one SET per file_id (earliest date), one R1 per rating_key.
    kept: list[Action] = []
    best_set: dict[int, Action] = {}
    seen_r1: set[str | None] = set()
    for a in actions:
        if a.kind is ActionKind.SET_FILE_WATCHED:
            assert a.file_id is not None and a.when is not None
            cur = best_set.get(a.file_id)
            if cur is None or a.when < (cur.when or a.when):
                best_set[a.file_id] = a
        elif a.kind is ActionKind.MARK_PLEX_PLAYED:
            if a.rating_key not in seen_r1:
                seen_r1.add(a.rating_key)
                kept.append(a)
        else:
            kept.append(a)
    kept.extend(best_set.values())
    return sorted(kept, key=_sort_key)

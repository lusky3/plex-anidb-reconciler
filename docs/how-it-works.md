# How it works

Each run reads four inputs:

- Plex: the episodes in `PLEX_SECTIONS` with their file paths, `viewCount` and `lastViewedAt`, plus the server
  owner's play history.
- Shoko: every file with its absolute paths, AniDB file id (fid), episode ids (eids), release group and import time
  (`Created`). The tool caches this for `SHOKO_INDEX_TTL_MIN`.
- The MyList snapshot (`mylist.json`) that Shoko's MyList sync writes, which says which fids are in MyList and their
  view dates.
- The ledger of writes this tool already made, since the snapshot may not show them yet.

From those it plans actions, then executes them (`apply`/`serve`) or only lists them (`plan`).

## Matching Plex files to Shoko files

The tool rewrites each Plex file path with `PATH_MAP`, taking the longest matching prefix on a path-component
boundary, and compares the result with every Shoko `AbsolutePath`.

Only an exact match, where the rewritten path equals one Shoko path, can lead to a write. Without an exact hit the
tool tries parent folder plus filename (case-insensitive), and if that identifies one Shoko file it calls the result a
fallback match. That file could be another show's same-named episode, so it lands in the report as R4
`fallback_match` and never gets written. A non-zero count means `PATH_MAP` is wrong or missing a root.

Several Shoko files on the same path or fallback key give R4 `ambiguous_path`; no hit at all gives R4
`no_shoko_match`.

Matching looks at file paths and nothing else, so it doesn't matter which agent or scanner built your Plex library:
the native agent, Absolute Series Scanner and ShokoRelay all work.

## Rules

**R1** (AniDB → Plex) fires when the Plex item is unwatched and every episode it covers is watched in AniDB. Plex
marks it played. R1 marks an item at most once, because the ledger remembers it. If you unmark the item in Plex
afterwards, the tool leaves it unwatched and won't mark it again later.

**R2** (Plex → AniDB) covers a watched Plex item where none of the file's episodes are watched in AniDB. The file
gets the earliest owner play from Plex history as its view date, or `lastViewedAt` when there's no history. That
fallback has no file identity and may be a manual mark or a scrobble, so it only counts if it isn't earlier than the
file's Shoko import time (`Created`).

**R2b** handles a re-watch of a new release: the file's episodes are already watched in AniDB through another file.
Here a play only counts if it came after both the latest AniDB view date of those episodes and the file's Shoko
import time, and the earliest such play becomes the date. Set `R2B_POLICY=ignore` and R2b turns into reports.

**R4** is report only. It never writes.

## Order of checks

For each Plex item, the first check that fails ends planning for it:

1. Path resolution: `no_shoko_match`, `ambiguous_path`, `fallback_match`; then `unrecognised` (no AniDB fid),
   `manual_link` (manually linked in Shoko), `no_episodes`.
2. `eid_mismatch`: AniDB and Shoko link the file to different episodes. Nothing gets written, not even R1.
3. Unwatched in Plex: R1 if all episodes are watched in AniDB, R4 `partial_episode` if only some are.
4. `multi_version`: a watched item with several distinct files.
5. Per file: skipped silently if its fid is already watched; `not_in_snapshot` if the fid isn't in MyList;
   `partial_episode` if only some of its episodes are watched; otherwise R2 (no episode watched) or R2b (all
   watched).

R2 can report `no_date`, `plex_marked_by_reconciler` (the only date would be this tool's own R1 scrobble),
`unknown_file_age` or `predates_file` (see below) or `stale_snapshot`. R2b has its own set: `unknown_file_age`, `ignore_policy`, `no_playback`, `stale_snapshot` and
`multiple_prerequisites`.

Once every item is planned, the tool keeps one SET per Shoko file; if several Plex items plan one, the earliest date
wins. Two R2s of different files that share an episode conflict, since Plex history has no file identity, so the
earliest stays R2 and the tool re-plans the others as R2b against it.

R2 and R2b need a snapshot younger than `SNAPSHOT_MAX_AGE_H`. An older one turns them into `stale_snapshot` reports,
and with no usable snapshot (missing, unreadable or malformed) the run plans nothing, logs why, and
`plan`/`apply` exit with code 3.

## Execution

`apply` runs every R1 first, then every R2, then every R2b. If an R2b depends on an R2 from the same run, it runs
only when that R2 was applied in this run; otherwise it's `skipped` with `prerequisite_not_applied` and the next run
looks at it again. Writes past `MAX_WRITES` (or `--limit`) come out `capped`, and writes the ledger already holds come
out `skipped_ledger`. `--rules` and `--file-id` restrict which writes run, though the run always lists reports. `--rating-key` scopes a run to Plex items the way a `/trigger` does: everything is planned, but only those items' actions run and get listed.

Right before each Shoko write the tool:

1. Re-reads the file (`GET /api/v3/File/{id}`). If the fid, episodes or matched path changed, the write fails as
   `shoko_changed` without a PUT.
2. Reads `GET /api/v3/File/{id}/UserStats`. If Shoko already has a `LastWatchedAt` or a non-zero `WatchedCount`, the
   row becomes `skipped` with `shoko_already_watched`, and it'll keep recurring each run until the snapshot shows the
   fid watched.

Shoko 5.3.3 answers the UserStats GET of a never-watched file with HTTP 404 and `No FileUserStats entry for the given
fileID for the current user`. The tool reads that as unwatched. Any other 404 or error fails the write without a PUT.
A dry run never reads UserStats. If the service gets SIGTERM between these reads and the PUT, the write stops
there with `skipped`/`shutdown` and nothing is sent.

The tool reads naive dates from Shoko in `SHOKO_TZ`.

## Reason codes

Each report row and each write carries a reason; the `detail` column adds what happened at execution time.

| Reason | Rule | Meaning | What to do |
| --- | --- | --- | --- |
| `anidb_episode_watched` | R1 | AniDB has every episode of this unwatched Plex item as watched; Plex gets marked played. | Nothing. |
| `new_watch` | R2 | Watched in Plex, none of the file's episodes watched in AniDB; the file gets the first Plex play as its date. | Nothing. |
| `rewatch_new_release` | R2b | A real replay of this release after the episode's AniDB date and after the file's import. | Nothing, or `R2B_POLICY=ignore` to stop these. |
| `no_shoko_match` | R4 | No Shoko file at the rewritten path. | A handful is normal; many mean `PATH_MAP` is wrong. |
| `ambiguous_path` | R4 | Several Shoko files share the path or fallback key. | Remove the duplicates in Shoko. |
| `fallback_match` | R4 | Matched only by folder and filename. | Fix `PATH_MAP`; this should be zero. |
| `unrecognised` | R4 | Shoko has no AniDB file id for the file. | Out of scope; Shoko has to recognise it first. |
| `manual_link` | R4 | The file is linked to its episode by hand in Shoko. | Out of scope; never written. |
| `no_episodes` | R4 | Shoko links the file to no AniDB episode. | Check the file in Shoko. |
| `eid_mismatch` | R4 | AniDB and Shoko link the file to different episodes. | Fix the link in Shoko or on AniDB. |
| `partial_episode` | R4 | Only some of the episodes involved are watched in AniDB. | Nothing; the tool won't guess. |
| `multi_version` | R4 | A watched Plex item with several distinct files; Plex can't say which one was played. | Split or remove versions in Plex if you want it written. |
| `not_in_snapshot` | R4 | The file isn't in MyList. | Check `AniDb.MyList_AddFiles`, then sync MyList. |
| `no_date` | R4 | Watched in Plex with no play history and no `lastViewedAt`. | Nothing usable; set it on anidb.net if you care. |
| `plex_marked_by_reconciler` | R4 | The only date is this tool's own R1 scrobble. | Nothing; that date isn't a watch. |
| `stale_snapshot` | R4, R2b | `mylist.json` is older than `SNAPSHOT_MAX_AGE_H`. | Run Shoko's MyList sync. |
| `predates_file` | R4 | No play history, and Plex's `lastViewedAt` is older than the file's Shoko import. | Nothing; that date belongs to an earlier file or mark. |
| `unknown_file_age` | R4, R2b | Shoko has no import time for the file, and the date would need one. | Nothing; the file can't be dated safely. |
| `ignore_policy` | R2b | `R2B_POLICY=ignore`. | Nothing. |
| `no_playback` | R2b | No play after the AniDB date and the file's import. | Nothing. |
| `multiple_prerequisites` | R2b | Would need several R2s of this run to land first. | Nothing; the next run decides. |

Detail values on write rows:

| Detail | Status | Meaning | What to do |
| --- | --- | --- | --- |
| `shoko_already_watched` | `skipped` | Shoko already had a date for the file, so nothing was written. | Nothing; it stops once the snapshot shows the file watched. |
| `shoko_changed` | `failed` | The file's fid, episodes or path changed since planning. | Nothing; the next run re-plans it. |
| `prerequisite_not_applied` | `skipped` | The R2 this R2b depends on wasn't applied in this run. | Nothing; the next run looks again. |
| `shutdown` | `skipped` | The service was stopping. | Nothing; re-planned next run. |
| an error message, e.g. `Shoko PUT /api/v3/File/7/UserStats returned HTTP 500` | `failed` | The request failed. | Check the backend; the write is retried next run. |

## Reports and ledger

Everything lives under `DATA_DIR`. Each run writes `reports/<run_id>.csv`, a self-contained
`reports/<run_id>.html` with summary counts first and the rows after, and `reports/<run_id>.json`, which records
whether the run was a plan or an apply, whether it was scoped to some Plex items, and the snapshot state. The tool
keeps the newest 200 of each. The
columns are `rule, kind, status, rating_key, file_id, fid, eids, when, reason, detail, aid, release_group,
anidb_state, anidb_viewdate`.

`when` is UTC. `aid` and `release_group` come from Shoko, with `aid` falling back to the snapshot. `anidb_state` is
the MyList `State` of the fid (1 HDD, 2 disk, 3 deleted, 4 remote) and `anidb_viewdate` its view date in UTC; both
stay empty when the fid isn't in the snapshot. A row's status is one of `dry_run`, `applied`, `failed`, `skipped`,
`skipped_ledger`, `capped` or `reported`.

`ledger.sqlite` holds applied and failed writes only, plus `pending` rows for R1. An R1 row goes in as `pending`
before the scrobble is sent and becomes `applied` or `failed` right after. Pending, applied and failed R1 rows all
count as marked by this tool, so a crash mid-scrobble or a request that timed out after Plex applied it can't turn
that `lastViewedAt` into an AniDB date. Its `before` column stores the snapshot state and view date,
Plex `viewCount`, and Shoko's `LastWatchedAt`/`WatchedCount` as read just before the write. `after` stores the date
written or `{"plex_played": true}`, and stays empty for a failed write. Older ledgers get these columns on first
open.

On stdout you get one summary line per rule and status (plus reason, for reports), e.g. `R2 dry_run: 120` or
`R4 reported no_shoko_match: 3`.

## Triggered runs

`serve` runs a full reconcile every `POLL_MINUTES`, and `POST /trigger` queues an immediate one.

Send a JSON body `{"rating_key": "123"}` and the tool still plans everything as in a full run, but only executes and
lists that Plex item's actions. A file shared by several items gets the one SET a full run would make, with the
earliest date. If an R2b's prerequisite R2 belongs to an item nobody triggered, the R2b is skipped with
`prerequisite_not_applied` until that R2 is applied. An empty or non-JSON body, or a missing or non-numeric key,
means a full run.

Triggers that arrive mid-run merge into the next run: the union of their keys, or a full run if any of them was
keyless.

Tautulli's "Watched" threshold can fire before Plex has raised the item's `viewCount`. The triggered run then finds
nothing to write, and the next poll picks the watch up.

## Accepted risks

**R2 may credit an older file's play to the current file.** Plex history has no file identity. Suppose the current
file replaced an older release that someone played in Plex but that never made it into AniDB: that play becomes the
current file's view date. The only alternative is never backfilling such episodes. R2b doesn't have this problem,
because it only counts plays after the AniDB date and the file's import time.

**Snapshot freshness.** R2 and R2b trust a snapshot up to `SNAPSHOT_MAX_AGE_H` old, so an episode first watched
through another AniDB client inside that window can get a second dated MyList entry.

**`/trigger` is unauthenticated.** It only queues a run, but anyone who can reach the port can trigger one. Don't
publish the port.

**SIGKILL during a write.** A write cut off before it reaches the ledger gets re-planned next run as the same file
with the same date, which is an idempotent PUT.

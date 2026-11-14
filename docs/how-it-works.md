# How it works

Each run reads four inputs:

- Plex: the episodes in `PLEX_SECTIONS` with their file paths, `viewCount` and `lastViewedAt`, and the
  server owner's play history.
- Shoko: every file with its absolute paths, AniDB file id (fid), episode ids (eids), release group and
  import time (`Created`). Cached for `SHOKO_INDEX_TTL_MIN`.
- The MyList snapshot (`mylist.json`) that Shoko's MyList sync writes: which fids are in MyList and their
  view dates.
- The ledger of writes this tool already made, which the snapshot may not show yet.

It plans actions, then executes them (`apply`/`serve`) or only lists them (`plan`).

## Matching Plex files to Shoko files

Each Plex file path is rewritten with `PATH_MAP` (longest matching prefix, on a path-component boundary) and
compared with every Shoko `AbsolutePath`.

- **Exact match**: the rewritten path equals one Shoko path. Only exact matches can lead to a write.
- **Fallback match**: no exact hit, but parent folder + filename (case-insensitive) identify one Shoko file.
  This can be another show's same-named episode, so it is reported as R4 `fallback_match` and never written.
  A non-zero count means `PATH_MAP` is wrong or missing a root.
- Several Shoko files on the same path or fallback key: R4 `ambiguous_path`. No hit at all: R4
  `no_shoko_match`.

## Rules

- **R1** (AniDB → Plex): the Plex item is unwatched and every episode it covers is watched in AniDB. Plex
  marks it played.
- **R2** (Plex → AniDB): the Plex item is watched, and none of the file's episodes are watched in AniDB.
  The file gets the earliest owner play from Plex history (or `lastViewedAt` without history) as its view
  date.
- **R2b** (re-watch of a new release): the file's episodes are already watched in AniDB through another
  file. Only plays after both the latest AniDB view date of those episodes and the file's Shoko import time
  count; the earliest of them becomes the date. `R2B_POLICY=ignore` turns R2b into reports.
- **R4**: report only. Never writes.

## Order of checks

For each Plex item, the first check that fails ends planning for it:

1. Path resolution: `no_shoko_match`, `ambiguous_path`, `fallback_match`; then `unrecognised` (no AniDB
   fid), `manual_link` (manually linked in Shoko), `no_episodes`.
2. `eid_mismatch`: AniDB and Shoko link the file to different episodes. Nothing is written, not even R1.
3. Unwatched in Plex: R1 if all episodes are watched in AniDB, R4 `partial_episode` if only some are.
4. `multi_version`: a watched item with several distinct files.
5. Per file: skipped silently if its fid is already watched; `not_in_snapshot` if the fid is not in
   MyList; `partial_episode` if only some of its episodes are watched; otherwise R2 (no episode watched) or
   R2b (all watched).

R2 reports: `no_date`, `plex_marked_by_reconciler` (the only date would be this tool's own R1 scrobble),
`stale_snapshot`. R2b reports: `unknown_file_age`, `ignore_policy`, `no_playback`, `stale_snapshot`,
`multiple_prerequisites`.

After planning:

- One SET per Shoko file; if several Plex items plan one, the earliest date wins.
- Two R2s of different files sharing an episode conflict, because Plex history has no file identity. The
  earliest stays R2; the others are re-planned as R2b against it.

R2 and R2b need a fresh snapshot (younger than `SNAPSHOT_MAX_AGE_H`); otherwise they are reported as
`stale_snapshot`. Without a snapshot the run plans nothing.

## Execution

`apply` runs every R1 first, then every R2, then every R2b. An R2b that depends on an R2 from the same run
runs only if that R2 was applied in this run; otherwise it is `skipped` with `prerequisite_not_applied` and
reconsidered next run. Writes beyond `MAX_WRITES` (or `--limit`) are `capped`. Writes already in the ledger
are `skipped_ledger`. `--rules` and `--file-id` restrict which writes run; reports are always listed.

Right before each Shoko write the tool:

1. Re-reads the file (`GET /api/v3/File/{id}`). If the fid, episodes or matched path changed, the write
   fails as `shoko_changed` without a PUT.
2. Reads `GET /api/v3/File/{id}/UserStats`. If Shoko already has a `LastWatchedAt` or a non-zero
   `WatchedCount`, the row is `skipped` with `shoko_already_watched`. It recurs each run until the snapshot
   shows the fid watched.

Shoko 5.3.3 answers the UserStats GET of a never-watched file with HTTP 404 and `No FileUserStats entry for
the given fileID for the current user`. That is read as unwatched. Any other 404 or error fails the write
without a PUT. A dry run never reads UserStats.

Naive dates from Shoko are read in `SHOKO_TZ`.

## Reports and ledger

Under `DATA_DIR`:

- `reports/<run_id>.csv` and a self-contained `reports/<run_id>.html` (summary counts, then the rows). The
  newest 200 of each are kept. Columns: `rule, kind, status, rating_key, file_id, fid, eids, when, reason,
  detail, aid, release_group, anidb_state, anidb_viewdate`.
  - `when` is UTC. `aid` and `release_group` come from Shoko (`aid` falls back to the snapshot).
  - `anidb_state` is the MyList `State` of the fid (1 HDD, 2 disk, 3 deleted, 4 remote); `anidb_viewdate`
    is its view date in UTC. Both are empty if the fid is not in the snapshot.
  - Statuses: `dry_run`, `applied`, `failed`, `skipped`, `skipped_ledger`, `capped`, `reported`.
- `ledger.sqlite`: applied and failed writes only. `before` holds the snapshot state and view date, Plex
  `viewCount` and Shoko's `LastWatchedAt`/`WatchedCount` read just before the write. `after` holds the date
  written or `{"plex_played": true}`, and is empty for a failed write. Older ledgers get these columns on
  first open.

stdout gets one summary line per rule and status (and reason, for reports), e.g. `R2 dry_run: 120` or
`R4 reported no_shoko_match: 3`.

## Triggered runs

`serve` runs a full reconcile every `POLL_MINUTES`. `POST /trigger` queues an immediate run.

- With a JSON body `{"rating_key": "123"}`, everything is planned as in a full run, then only that Plex
  item's actions are executed and listed. A file shared by several items gets the one SET a full run would
  make, with the earliest date. An R2b whose prerequisite R2 belongs to an untriggered item is skipped with
  `prerequisite_not_applied` until that R2 is applied.
- Without a usable key (empty or non-JSON body, missing or non-numeric key) it is a full run.
- Triggers that arrive during a run are merged into the next run: the union of their keys, or a full run if
  any was keyless.

## Accepted risks

- **R2 may credit an older file's play to the current file.** Plex history has no file identity. If the
  current file replaced an older release that was played in Plex but never recorded in AniDB, that play
  becomes the current file's view date. The alternative is never backfilling such episodes. R2b does not
  have this problem: it only counts plays after the AniDB date and the file's import time.
- **Snapshot freshness.** R2 and R2b trust a snapshot up to `SNAPSHOT_MAX_AGE_H` old. An episode first
  watched through another AniDB client inside that window can get a second dated MyList entry.
- **`/trigger` is unauthenticated.** It only queues a run, but anyone who can reach the port can trigger one.
  Do not publish the port.
- **SIGKILL during a write.** If a write is cut off before it reaches the ledger, it is re-planned next run
  as the same file with the same date (an idempotent PUT).

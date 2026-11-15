# Deployment

Work through the steps in order, and stop at any step that doesn't hold.

Two placeholders appear below. `<shoko-data>` is Shoko's data directory on the host (the one that contains
`MyList/` and `settings-server.json`), and `<state-dir>` is a host directory for this tool's state. The commands
assume you copied `compose.yml` from `compose.example.yml` and keep the secrets (`SHOKO_API_KEY`, `PLEX_TOKEN`) in an
`.env` file next to it, or have your secret manager export them.

## Prerequisites

**Plex token.** It has to be the server owner's token. The tool reads the owner's play history and marks items
played for the account the token belongs to, so plays by other Plex users (managed or shared) never count. To find
it, sign in to Plex Web as the owner, open any episode, choose `...` → Get Info → View XML, and copy the
`X-Plex-Token` value from the end of the URL that opens. Plex describes the same thing in
[Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

Plex Pass isn't needed. Only Plex's own webhooks require it, and this tool doesn't use them; Tautulli is the
supported way to trigger a run after a watch.

**Shoko with AniDB MyList.** Your AniDB account login has to be set up in Shoko (Settings → AniDB → Login Options),
and Shoko's MyList sync has to have run at least once. That sync writes `MyList/mylist.json` under Shoko's data
directory, and this tool reads it as its MyList snapshot. The tool was built and tested against Shoko Server 5.3.3.
Other versions may behave differently; the handling of the UserStats 404 described in
[how it works](how-it-works.md#execution) matches 5.3.3's exact reply.

**Shell variables.** The `curl` checks in this guide run in your host shell, which doesn't read `.env`, and the
container names `plex` and `shoko` only resolve on the Docker network. Export these first:

```sh
export PLEX_TOKEN=<your token>
export PLEX=http://localhost:32400    # any Plex address reachable from this host
export SHOKO_API_KEY=<your key>
export SHOKO=http://localhost:8111    # any Shoko address reachable from this host
```

**Section ids.** `PLEX_SECTIONS` lists the Plex libraries that hold your anime. List them with:

```sh
curl -s -H "X-Plex-Token: $PLEX_TOKEN" -H 'Accept: application/json' "$PLEX/library/sections" \
  | jq -r '.MediaContainer.Directory[] | "\(.key)\t\(.type)\t\(.title)"'
```

The first column is the id.

## 0. Image and host directories

The image is `ghcr.io/lusky3/plex-anidb-reconciler`; for production, pin a version tag or digest (see
[Upgrading](#upgrading)). It runs as uid 10001 with a read-only root filesystem and keeps all state in `/data`.

Create the state directory owned by that uid first. Docker would otherwise create it root-owned, and the first run
would fail writing the ledger:

```sh
sudo install -d -o 10001 -g 10001 <state-dir>
```

The MyList directory gets mounted at `/mylist:ro`, and uid 10001 has to be able to read it. This check works whether
or not your host has a user with that uid:

```sh
docker run --rm --user 10001:10001 -v <shoko-data>/MyList:/m:ro busybox ls -l /m/mylist.json
```

Put the container on the Docker network that Plex, Shoko and Tautulli share.

The image is distroless, so there's no shell. To debug, use
`docker run --rm --entrypoint /app/.venv/bin/python <image> -c ...`. If you publish the image to your own registry,
note that GHCR makes a new package private: make it public, or run `docker login ghcr.io` with a token that has
`read:packages`.

## 1. Backups

Back up Shoko's database (the `SQLite` folder in `<shoko-data>`) and export your MyList on anidb.net.

## 2. Plex webhooks

In Plex → Settings → Webhooks, remove any URL pointing at Shoko (`/plex` or `/plex.json`). Leave one there and Plex
plays reach AniDB twice.

## 3. Shoko settings

Set these in the Shoko Web UI, under Settings → AniDB → Mylist Options and Settings → Import → Import Options. The
alternative is `<shoko-data>/settings-server.json`, where the names below are the JSON paths; stop Shoko before you
edit that file so it doesn't write over your change.

| Setting | Web UI label | Value | Why |
| --- | --- | --- | --- |
| `AniDb.MyList_AddFiles` | Add Files | `true` | A file that isn't in MyList gets reported as `not_in_snapshot` and never written. |
| `AniDb.MyList_SetWatched` | Set Watched | `true` | Required. The tool writes to Shoko, and this is what makes Shoko send the date on to AniDB. |
| `AniDb.MyList_ReadWatched` | Read Watched | `true` | Shoko picks up AniDB's view dates, so the check before each write sees them and leaves them alone. |
| `AniDb.MyList_SetUnwatched` | Set Unwatched | `false` | Shoko never unwatches anything on AniDB. |
| `AniDb.MyList_ReadUnwatched` | Read Unwatched | `false` | An unwatched AniDB entry can't clear a Shoko date. |
| `AniDb.MyList_StorageState` | Storage State | your choice, e.g. `1` (HDD) or `4` (Remote) | The state Shoko gives MyList entries. Watched dates don't depend on it. |
| `AniDb.MyList_DeleteType` | Delete Action | `1` (Delete File (Local)) | Removing a file in Shoko leaves its AniDB entry and view date in place. |
| `Import.UseExistingFileWatchedStatus` | Set file as watched if episode is watched | `false` | Otherwise a newly imported release inherits the episode's watched state and AniDB gets a date for a file nobody played. |
| `AniDb.MyList_UpdateFrequency` | not in the 5.3.3 Web UI: use `settings-server.json` | `1` (Never) until the backfill is done | Turned on in step 9. The scheduled sync is what keeps `mylist.json` fresh. |

## 4. One MyList sync

Run one sync by hand:

```sh
curl -s -H "apikey: $SHOKO_API_KEY" "$SHOKO/api/v3/Action/SyncMyList"
```

Expect one paced AniDB update, about 2–6 s each, for every local file whose storage state differs from the setting,
so a big library can take hours. Files Shoko already has as watched may show a view date shifted by the `SHOKO_TZ`
offset; the next sync corrects it.

The snapshot this sync writes counts as fresh for `SNAPSHOT_MAX_AGE_H` (26 h by default). If the backfill in step 8
runs longer than that, R2 and R2b rows turn into `stale_snapshot` reports: run the sync again and continue.

## 5. Path map

Plex and Shoko usually see the same files under different prefixes. To find the Plex path of an episode, open it in
Plex Web and use `...` → Get Info, or ask the API (with the variables from [Prerequisites](#prerequisites)):

```sh
curl -s -H "X-Plex-Token: $PLEX_TOKEN" -H 'Accept: application/json' \
  "$PLEX/library/metadata/<ratingKey>" \
  | jq -r '.MediaContainer.Metadata[0].Media[0].Part[0].file'
```

Shoko shows the same file's path under file details → location in its Web UI, or through
`GET /api/v3/File/<id>?include=AbsolutePaths`.

Set `PATH_MAP` to the leading parts that differ, `;`-separated if you have several roots. If Plex sees
`/plex/anime/Show/01.mkv` and Shoko sees `/shoko/anime/Show/01.mkv`, that's `PATH_MAP=/plex/anime=/shoko/anime`.

## 6. Dry run

```sh
docker compose run --rm plex-anidb-reconciler plan
```

Every executable row shows `dry_run`. Review the CSV in `<state-dir>/reports/` and the stdout summary.

You should see only a handful of `no_shoko_match` and `ambiguous_path` rows, for files Shoko doesn't know; hundreds
mean `PATH_MAP` is wrong. `fallback_match` must be zero, and if it isn't, fix `PATH_MAP` and re-run `plan`. Don't
look for `shoko_already_watched` here, because it never appears in a dry run and needs `apply`.

## 7. Canary

```sh
docker compose run --rm plex-anidb-reconciler apply --rules R2 --limit 1
```

A plain `--limit 1` would run an R1, since R1 runs first. Add `--file-id <file_id>` to pick a row from the CSV. Then
check Shoko's UserStats for the file, the Shoko log line `UpdateMyListFileStatusJob ... | True | <date>`, and the
date on anidb.net. An R2 date may come from an older release of the episode
([accepted risk](how-it-works.md#accepted-risks)). Before step 8, work through the live verification below.

## 8. Backfill

R1 goes first (Plex only), then the AniDB writes. Repeat each command until the summary shows no `capped` rows:

```sh
docker compose run --rm plex-anidb-reconciler apply --rules R1
docker compose run --rm plex-anidb-reconciler apply --rules R2,R2b
```

## 9. Go live

Wait for the Shoko queue to empty. Then set `AniDb.MyList_UpdateFrequency=4` (Daily) and start the service with
`docker compose up -d`.

If you use Tautulli, add a **Webhook** notification agent with URL `http://plex-anidb-reconciler:8787/trigger`,
method `POST`, trigger "Watched", and under Data → Watched → JSON Data: `{"rating_key": "{rating_key}"}`. Tautulli's
watched threshold can fire before Plex has raised the item's `viewCount`; that triggered run then finds nothing to do,
and the next poll (`POLL_MINUTES`) picks the watch up.

`serve` applies immediately at startup and on every restart. Deploy it only after the backfill, and fix config
problems first: a crash-looping container re-runs apply on each start.

Optional, once the backfill is done: switch `AniDb.MyList_DeleteType` to `2` (Mark Deleted). Deleting a file in
Shoko then keeps its AniDB entry and view date but flags the entry as deleted, so your MyList shows which files you
still have. The tool works the same with either setting.

## Live verification

Do this after the canary and before the backfill. Stop at the first item that doesn't hold.

- [ ] **PATH_MAP.** The canary's Plex path, rewritten by `PATH_MAP`, is exactly the Shoko `AbsolutePath` of that
  file. `plan` shows few `no_shoko_match`/`ambiguous_path` and no `fallback_match`.
- [ ] **No Plex webhook to Shoko** (step 2).
- [ ] **Same instant everywhere.** The CSV `when` (UTC), the Shoko log line and the anidb.net view date are the same
  instant, allowing for each one's timezone display.
- [ ] **Shoko `Created` has an offset.** `GET /api/v3/File/<id>` returns `Created` with an offset or `Z`. The tool
  reads a naive value as `SHOKO_TZ`, so if Shoko actually sends naive UTC and `SHOKO_TZ` isn't UTC, R2b import gating
  is shifted: stop.
- [ ] **Single-file re-read returns xrefs.**
  `GET /api/v3/File/<id>?include=XRefs,AbsolutePaths&includeXRefs=true&includeDataFrom=AniDB` returns
  `SeriesIDs[].EpisodeIDs[]`, `AniDB.ID` and `Locations[].AbsolutePath`. If it doesn't, every write fails as
  `shoko_changed`: stop.
- [ ] **Plex `/:/scrobble` doesn't create a history row.** Pick one R1 row from the `plan` CSV and note its
  `rating_key`, then its owner history count:

  ```sh
  curl -s -H "X-Plex-Token: $PLEX_TOKEN" -H 'Accept: application/json' \
    "$PLEX/status/sessions/history/all?accountID=1&metadataItemID=<ratingKey>" \
    | jq '.MediaContainer.size'
  ```

  Mark exactly that item with `apply --rules R1 --rating-key <ratingKey> --limit 1`, confirm the summary shows one
  R1 `applied`, and check the count again. If it went up, stop; you'd have to exclude those rows before R2/R2b may
  run.
- [ ] **Multi-episode files in `mylist.json`.** For a file spanning several episodes, `mylist.json` has one entry
  per (`FileID`, `EpisodeID`). With only one entry, such files report `eid_mismatch` and never reconcile (nothing
  gets written for them): stop.

## Operations

**Health.** `GET /healthz` reports the last run, `"snapshot": "ok" | "stale" | "unavailable"`,
`"loop": "ok" | "stale"` and `"failed_runs"`, the number of failed runs in a row.

It answers 503 in three cases. `"loop": "stale"` means the poll loop died, or hasn't beaten within
2 × `POLL_MINUTES` + 5 min while idle. A run that has been active longer than `RUN_STALE_MINUTES` is hung, and
`"run_active_since"` (UTC) shows when it started; restart the container, since applied writes are already in the
ledger. And once `HEALTH_MAX_FAILED_RUNS` runs in a row raised an error, had a failed write or found no snapshot, the
body carries an `"unhealthy"` reason such as `3 consecutive failed runs (last: snapshot_unavailable)`. The next
successful run clears it. A failed write gets retried every run, so a single file that keeps failing keeps
`/healthz` at 503 on its own: look for the `failed` rows, and their `detail`, in the latest report under
`<state-dir>/reports/`.

A snapshot that stays `stale` means Shoko's MyList sync isn't refreshing `mylist.json`, and R2/R2b wait until it
does. `unavailable` means the file is missing, unreadable or malformed; the log line names the path and the problem.

The image's Docker `HEALTHCHECK` runs `pxar healthcheck` against `LISTEN`, which only means something for `serve`.
One-shot `plan`/`apply` containers may show `unhealthy`. That's harmless, and `docker run` accepts
`--no-healthcheck`.

**Logs.** `docker logs plex-anidb-reconciler`. A healthy service logs `serving on 0.0.0.0:8787` once, then one line
per run, e.g. `run 20261114T120000Z-1a2b3c (apply, snapshot ok): applied=2 reported=14`, or `no actions` when there
was nothing to do. It also prints the per-rule summary to stdout. Failures log as `reconcile run failed: ...` with the
backend, method, path and HTTP status or exception type.

**Shutdown.** On SIGTERM, `serve` stops accepting triggers and tells an in-flight run to stop. No new write starts
after that. A Shoko write still re-reading the file stops before its PUT, so at most one request is still in flight,
and a request on the write path gives up after about 20–25 s for a normal-sized reply. The interrupted rows come out `skipped` with `shutdown`
and get re-planned next run. `serve` waits up to `SHUTDOWN_GRACE_S` (25 s) for the run to end; a run that is still
reading Plex or Shoko when the grace runs out gets cut off, which is harmless because it hasn't written anything yet.
Keep Docker's `stop_grace_period` above `SHUTDOWN_GRACE_S`; the example compose file uses 30 s.

**Exit codes.** `plan` and `apply` exit `0` when the run went through, `1` on a config error, `2` when a write or the
run failed or the command line was invalid, and `3` when the MyList snapshot is unavailable. Errors print the
backend, method, path and HTTP status or exception type, never secrets or query strings.

**Network.** `/trigger` has no authentication. Keep port 8787 unpublished and reachable only on the Docker network.

## Upgrading

Pin a release in `compose.yml`, either a version tag (`ghcr.io/lusky3/plex-anidb-reconciler:0.1.0`) or a digest,
and read the [changelog](../CHANGELOG.md) before you move to a new one. Each release publishes `X.Y.Z`, `X.Y` and
`sha-<commit>` tags; `latest` follows `main` and isn't a release.

To upgrade, back up `<state-dir>`, change the tag, then run `docker compose pull` and a `plan` with the new image
before `docker compose up -d`. The ledger gets any new columns on first open, so the state directory carries over.

## Recovery

The tool can't undo a write. Every applied write is in `<state-dir>/ledger.sqlite` with what it saw before and what
it wrote, so you can revert one by hand. The image has no `sqlite3`, so query it from the host:

```sh
sqlite3 <state-dir>/ledger.sqlite \
  "SELECT run_id, rule, rating_key, file_id, before, after FROM actions WHERE status = 'applied' ORDER BY id DESC LIMIT 20"
```

For a Shoko file, `before` holds Shoko's `shoko_last_watched_at` and `shoko_watched_count` from right before the
write. Put them back with a `PUT /api/v3/File/<file_id>/UserStats` carrying those values. With
`MyList_SetUnwatched=false`, Shoko won't pass an unwatch on to AniDB, so fix the AniDB entry on anidb.net as well (or
there only, if Shoko already matches). For an R1, mark the item unwatched in Plex. Keep the ledger row: it's what
stops the tool from making the same write again.

Back up `<state-dir>` along with Shoko's database. If you lose `ledger.sqlite`, the tool forgets which items it
marked and which files it wrote, and R1 may mark an item played once more. Writes that already reached Shoko or
AniDB stay protected, because the Shoko UserStats check and the snapshot still stop any overwrite.

Losing the ledger also means the tool no longer knows that an item's Plex `lastViewedAt` is its own R1 scrobble.
That date could only reach AniDB if all of these hold: the item has no Plex history row, Shoko has no watched date
for the file, AniDB has since stopped showing the episode as watched, and Shoko imported the file before the
scrobble. R2 never uses a `lastViewedAt` older than the file's import (`predates_file`), so a release imported after
the scrobble is safe. A crash or timeout during the scrobble itself doesn't open this gap, because the R1 row is in
the ledger as `pending` before the scrobble is sent and still counts after it ends as `failed`.

## Out of scope

Files Shoko can't recognise (no AniDB file id) and files linked to episodes by hand show up in the report as
`unrecognised` and `manual_link`. The tool never writes them, and fixing them is up to you in Shoko.

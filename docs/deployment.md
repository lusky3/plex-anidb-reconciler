# Deployment

Work through the steps in order, and stop at any step that doesn't hold.

Two placeholders appear below. `<shoko-data>` is Shoko's data directory on the host (the one that contains
`MyList/`), and `<state-dir>` is a host directory for this tool's state. The commands assume you copied
`compose.yml` from `compose.example.yml` and keep the secrets (`SHOKO_API_KEY`, `PLEX_TOKEN`) in an `.env` file next
to it, or have your secret manager export them.

## 0. Image and host directories

The image is `ghcr.io/lusky3/plex-anidb-reconciler`; for production, pin a version tag or digest. It runs as uid
10001 with a read-only root filesystem and keeps all state in `/data`.

Create the state directory owned by that uid first. Docker would otherwise create it root-owned, and the first run
would fail writing the ledger:

```sh
sudo install -d -o 10001 -g 10001 <state-dir>
```

The MyList directory gets mounted at `/mylist:ro`, and uid 10001 has to be able to read it:

```sh
sudo -u '#10001' ls <shoko-data>/MyList/mylist.json
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

- `AniDb.MyList_DeleteType=1`
- `MyList_SetUnwatched=false`
- `MyList_ReadUnwatched=false`
- `MyList_StorageState`: your choice, e.g. `1` (HDD) or `4` (Remote)
- `Import.UseExistingFileWatchedStatus=false`
- `MyList_UpdateFrequency=1` (Never) until the backfill is done

## 4. One MyList sync

Run `GET /api/v3/Action/SyncMyList` once. Expect one paced AniDB update, about 2–6 s each, for every local file
whose storage state differs from the setting. Files Shoko already has as watched may show a view date shifted by the
`SHOKO_TZ` offset; the next sync corrects it.

## 5. Path map

Plex and Shoko usually see the same files under different prefixes. To find the Plex path of an episode, open it in
Plex Web and use `...` → Get Info, or ask the API:

```sh
curl -s -H "X-Plex-Token: $PLEX_TOKEN" -H 'Accept: application/json' \
  http://plex:32400/library/metadata/<ratingKey> \
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

Wait for the Shoko queue to empty. Then set `MyList_UpdateFrequency=4` and start the service with
`docker compose up -d`.

If you use Tautulli, add a **Webhook** notification agent with URL `http://plex-anidb-reconciler:8787/trigger`,
method `POST`, trigger "Watched", and under Data → Watched → JSON Data: `{"rating_key": "{rating_key}"}`.

`serve` applies immediately at startup and on every restart. Deploy it only after the backfill, and fix config
problems first: a crash-looping container re-runs apply on each start.

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
- [ ] **Plex `/:/scrobble` doesn't create a history row.** Note the owner history count of one R1 item:

  ```sh
  curl -s -H "X-Plex-Token: $PLEX_TOKEN" -H 'Accept: application/json' \
    'http://plex:32400/status/sessions/history/all?accountID=1&metadataItemID=<ratingKey>' \
    | jq '.MediaContainer.size'
  ```

  Run `apply --rules R1 --limit 1` and check again. If the count went up, stop; you'd have to exclude those rows
  before R2/R2b may run.
- [ ] **Multi-episode files in `mylist.json`.** For a file spanning several episodes, `mylist.json` has one entry
  per (`FileID`, `EpisodeID`). With only one entry, such files report `eid_mismatch` and never reconcile (nothing
  gets written for them): stop.

## Operations

**Health.** `GET /healthz` reports the last run, `"snapshot": "ok" | "stale" | "unavailable"` and
`"loop": "ok" | "stale"`.

A snapshot that stays `stale` means Shoko's MyList sync isn't refreshing `mylist.json`, and R2/R2b wait until it
does. `"loop": "stale"` (HTTP 503) means the poll loop died, or hasn't beaten within 2 × `POLL_MINUTES` + 5 min while
idle.

During a run, `/healthz` shows `"run_active_since"` (UTC) and stays 200 until the run has lasted
`RUN_STALE_MINUTES`. After that it returns 503 because the run is hung. Restart the container; applied writes are
already in the ledger.

The image's Docker `HEALTHCHECK` runs `pxar healthcheck` against `LISTEN`, which only means something for `serve`.
One-shot `plan`/`apply` containers may show `unhealthy`. That's harmless, and `docker run` accepts
`--no-healthcheck`.

**Shutdown.** On SIGTERM, `serve` stops accepting triggers and tells an in-flight run to stop at the next write
boundary. The write in flight completes and lands in the ledger; the rest come out `skipped` with `shutdown` and get
re-planned next run. `serve` waits up to `SHUTDOWN_GRACE_S`, so keep Docker's `stop_grace_period` above it.

**Exit codes.** `0` ok, `1` config error, `2` a write failed or the run failed. Errors print the method, path and
HTTP status or exception type, never secrets or query strings.

**Network.** `/trigger` has no authentication. Keep port 8787 unpublished and reachable only on the Docker network.

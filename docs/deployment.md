# Deployment

Work through the steps in order. Stop at any step that does not hold.

Placeholders: `<shoko-data>` is Shoko's data directory on the host (the one that contains `MyList/`),
`<state-dir>` is a host directory for this tool's state. Commands assume `compose.yml` copied from
`compose.example.yml` and secrets (`SHOKO_API_KEY`, `PLEX_TOKEN`) in an `.env` file next to it, or exported
by your secret manager.

## 0. Image and host directories

- Use `ghcr.io/lusky3/plex-anidb-reconciler`. For production, pin a version tag or digest.
- The container runs as uid 10001 with a read-only root filesystem. All state lives in `/data`.
- Create the state directory owned by that uid, or the first run fails writing the ledger (Docker would
  create it root-owned):

  ```sh
  sudo install -d -o 10001 -g 10001 <state-dir>
  ```

- The MyList directory, mounted at `/mylist:ro`, must be readable by uid 10001:

  ```sh
  sudo -u '#10001' ls <shoko-data>/MyList/mylist.json
  ```

- Put the container on the Docker network that Plex, Shoko and Tautulli share.
- The image is distroless (no shell). To debug:
  `docker run --rm --entrypoint /app/.venv/bin/python <image> -c ...`.
- If you publish the image to your own registry: a new GHCR package is private. Make it public or run
  `docker login ghcr.io` with a token that has `read:packages`.

## 1. Backups

Back up Shoko's database (the `SQLite` folder in `<shoko-data>`) and export your MyList on anidb.net.

## 2. Plex webhooks

Plex → Settings → Webhooks: remove any URL pointing at Shoko (`/plex` or `/plex.json`). Otherwise Plex
plays reach AniDB twice.

## 3. Shoko settings

- `AniDb.MyList_DeleteType=1`
- `MyList_SetUnwatched=false`
- `MyList_ReadUnwatched=false`
- `MyList_StorageState`: your choice, e.g. `1` (HDD) or `4` (Remote)
- `Import.UseExistingFileWatchedStatus=false`
- `MyList_UpdateFrequency=1` (Never) until the backfill is done

## 4. One MyList sync

Run `GET /api/v3/Action/SyncMyList` once. Expect one paced AniDB update per local file whose storage state
differs from the setting (about 2–6 s each). Files Shoko already has as watched may show a view date shifted
by the `SHOKO_TZ` offset; the next sync corrects it.

## 5. Path map

Plex and Shoko usually see the same files under different prefixes.

- Plex path of an episode: Plex Web → episode → `...` → Get Info, or

  ```sh
  curl -s -H "X-Plex-Token: $PLEX_TOKEN" -H 'Accept: application/json' \
    http://plex:32400/library/metadata/<ratingKey> \
    | jq -r '.MediaContainer.Metadata[0].Media[0].Part[0].file'
  ```

- Shoko path of the same file: Shoko Web UI → file details → location, or
  `GET /api/v3/File/<id>?include=AbsolutePaths`.
- Set `PATH_MAP` to the differing leading parts, `;`-separated for several roots. Example: Plex sees
  `/plex/anime/Show/01.mkv`, Shoko sees `/shoko/anime/Show/01.mkv` → `PATH_MAP=/plex/anime=/shoko/anime`.

## 6. Dry run

```sh
docker compose run --rm plex-anidb-reconciler plan
```

Every executable row shows `dry_run`. Review the CSV in `<state-dir>/reports/` and the stdout summary:

- `no_shoko_match` and `ambiguous_path`: should be a handful (files Shoko does not know). Hundreds mean
  `PATH_MAP` is wrong.
- `fallback_match`: must be zero. Otherwise fix `PATH_MAP` and re-run `plan`.
- `shoko_already_watched` never appears in a dry run; it needs `apply`.

## 7. Canary

```sh
docker compose run --rm plex-anidb-reconciler apply --rules R2 --limit 1
```

A plain `--limit 1` would run an R1, which runs first. Add `--file-id <file_id>` to pick a row from the CSV.
Then check Shoko's UserStats for the file, the Shoko log line `UpdateMyListFileStatusJob ... | True |
<date>`, and the date on anidb.net. Keep in mind that an R2 date may come from an older release of the
episode ([accepted risk](how-it-works.md#accepted-risks)). Work through the live verification below before
step 8.

## 8. Backfill

R1 first (Plex only), then the AniDB writes. Repeat each until the summary shows no `capped` rows:

```sh
docker compose run --rm plex-anidb-reconciler apply --rules R1
docker compose run --rm plex-anidb-reconciler apply --rules R2,R2b
```

## 9. Go live

When the Shoko queue is empty:

- Set `MyList_UpdateFrequency=4`.
- Start the service: `docker compose up -d`.
- Optional, Tautulli: add a **Webhook** notification agent. URL `http://plex-anidb-reconciler:8787/trigger`,
  method `POST`, trigger "Watched", Data → Watched → JSON Data: `{"rating_key": "{rating_key}"}`.

`serve` applies immediately at startup and on every restart. Deploy it only after the backfill, and fix
config problems first: a crash-looping container re-runs apply on each start.

## Live verification

Do this after the canary and before the backfill. Stop at the first item that does not hold.

- [ ] **PATH_MAP.** The canary's Plex path, rewritten by `PATH_MAP`, is exactly the Shoko `AbsolutePath` of
  that file. `plan` shows few `no_shoko_match`/`ambiguous_path` and no `fallback_match`.
- [ ] **No Plex webhook to Shoko** (step 2).
- [ ] **Same instant everywhere.** The CSV `when` (UTC), the Shoko log line and the anidb.net view date are
  the same instant, allowing for each one's timezone display.
- [ ] **Shoko `Created` has an offset.** `GET /api/v3/File/<id>` returns `Created` with an offset or `Z`. A
  naive value is read as `SHOKO_TZ`. If Shoko actually sends naive UTC and `SHOKO_TZ` is not UTC, R2b import
  gating is shifted: stop.
- [ ] **Single-file re-read returns xrefs.**
  `GET /api/v3/File/<id>?include=XRefs,AbsolutePaths&includeXRefs=true&includeDataFrom=AniDB` returns
  `SeriesIDs[].EpisodeIDs[]`, `AniDB.ID` and `Locations[].AbsolutePath`. If not, every write fails as
  `shoko_changed`: stop.
- [ ] **Plex `/:/scrobble` does not create a history row.** Note the owner history count of one R1 item:

  ```sh
  curl -s -H "X-Plex-Token: $PLEX_TOKEN" -H 'Accept: application/json' \
    'http://plex:32400/status/sessions/history/all?accountID=1&metadataItemID=<ratingKey>' \
    | jq '.MediaContainer.size'
  ```

  Run `apply --rules R1 --limit 1` and check again. If the count went up, stop: those rows would have to be
  excluded before R2/R2b may run.
- [ ] **Multi-episode files in `mylist.json`.** For a file spanning several episodes, `mylist.json` has one
  entry per (`FileID`, `EpisodeID`). If it has only one, such files report `eid_mismatch` and never
  reconcile (nothing is written for them): stop.

## Operations

**Health.** `GET /healthz` reports the last run, `"snapshot": "ok" | "stale" | "unavailable"` and
`"loop": "ok" | "stale"`.

- A persistently `stale` snapshot means Shoko's MyList sync is not refreshing `mylist.json`. R2/R2b wait
  until it does.
- `"loop": "stale"` (HTTP 503): the poll loop died, or has not beaten within 2 × `POLL_MINUTES` + 5 min
  while idle.
- During a run, `/healthz` shows `"run_active_since"` (UTC) and stays 200 until the run has lasted
  `RUN_STALE_MINUTES`. After that it returns 503: the run is hung. Restart the container; applied writes are
  already in the ledger.
- The image's Docker `HEALTHCHECK` runs `pxar healthcheck` against `LISTEN`. It only means something for
  `serve`. One-shot `plan`/`apply` containers may show `unhealthy`; that is harmless (`docker run` accepts
  `--no-healthcheck`).

**Shutdown.** On SIGTERM, `serve` stops accepting triggers and tells an in-flight run to stop at the next
write boundary. The write in flight completes and is recorded; the rest are `skipped` with `shutdown` and
re-planned next run. It waits up to `SHUTDOWN_GRACE_S`; keep Docker's `stop_grace_period` above it.

**Exit codes.** `0` ok, `1` config error, `2` a write failed or the run failed. Errors print the method,
path and HTTP status or exception type, never secrets or query strings.

**Network.** `/trigger` has no authentication. Keep port 8787 unpublished and reachable only on the Docker
network.

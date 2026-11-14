# Rollout runbook

State lives in `/data` (host: `/opt/plex-anidb-reconciler`): `ledger.sqlite` (applied and failed writes only) and one report per run at `reports/<run_id>.csv` (the newest 200 are kept).

Secrets come from Doppler, project `misc`, config `dev`: `SHOKO_API_KEY` and `PLEX_TOKEN` (or wherever you keep the Plex token). If `PLEX_TOKEN` is not in Doppler yet, add it there next to `SHOKO_API_KEY` before step 6 (`doppler secrets set PLEX_TOKEN --project misc --config dev`). Every `docker compose` command below is prefixed with `doppler run --project misc --config dev --` so both reach the container; alternatively put them in a root-only env file and use `docker compose --env-file /root/pxar.env ...`.

- [ ] 0. **Host directories.** The container runs as uid 10001. Create the state dir with that owner, or the first run fails writing `ledger.sqlite` / `reports/` (Docker would create it root-owned): `sudo install -d -o 10001 -g 10001 /opt/plex-anidb-reconciler`. The MyList dir mounted at `/mylist:ro` must be readable by uid 10001: check as that uid with `sudo -u '#10001' ls /opt/shoko/config/Shoko.CLI/MyList/mylist.json` (it must list the file without "Permission denied"; fix with world-read or a matching group).
- [ ] 1. **Backups.** Copy `/opt/shoko/config/Shoko.CLI/SQLite`; export MyList on anidb.net.
- [ ] 2. **Plex webhooks.** Plex -> Settings -> Webhooks: remove any URL pointing at Shoko (`/plex` or `/plex.json`).
- [ ] 3. **Shoko settings.**
  - `AniDb.MyList_DeleteType=1`
  - `MyList_SetUnwatched=false`
  - `MyList_ReadUnwatched=false`
  - `MyList_StorageState=4` (Remote)
  - `Import.UseExistingFileWatchedStatus=false`
  - `MyList_UpdateFrequency=1` (Never) until the backfill is done
- [ ] 4. **One MyList sync:** `GET /api/v3/Action/SyncMyList`. Expect ~7.2k state pushes (HDD -> Remote), about 12 h of paced UDP. The 23 locally watched files may see a transient +/-5 h date shift that the next sync corrects (spec section 4).
- [ ] 5. **Path map.** Plex and Shoko see the same files under different prefixes. Find Plex's prefix from any anime episode: Plex Web -> episode -> `...` -> Get Info -> the file path (or `curl -s -H "X-Plex-Token: $PLEX_TOKEN" -H 'Accept: application/json' http://plex:32400/library/metadata/<ratingKey> | jq -r '.MediaContainer.Metadata[0].Media[0].Part[0].file'`), e.g. `/data/TV/tv_anime/...`. Find Shoko's prefix for the same file: Shoko Web UI -> file details -> location, or `GET /api/v3/File/<id>?include=AbsolutePaths` (e.g. `/mnt/media/TV/tv_anime/...`). Set `PATH_MAP` to the differing leading parts, `plexprefix=shokoprefix`, `;`-separated if there are several roots (e.g. `PATH_MAP=/data/TV=/mnt/media/TV`).
- [ ] 6. **Dry run:** `doppler run --project misc --config dev -- docker compose run --rm plex-anidb-reconciler plan` (uncapped: every executable row shows `dry_run`). Review the CSV in `/data/reports/`. Before applying, check the R4 `no_shoko_match` and `ambiguous_path` counts in the stdout summary: they should be small (a handful of files Shoko does not know). Hundreds mean `PATH_MAP` is wrong: fix it and re-run `plan`.
- [ ] 7. **Canary:** `doppler run --project misc --config dev -- docker compose run --rm plex-anidb-reconciler apply --rules R2 --limit 1` (a plain `--limit 1` would only run an R1, which runs first). To pick a specific row from the dry-run CSV use `--file-id <file_id>` as well. Verify UserStats, the Shoko log line `UpdateMyListFileStatusJob ... | True | <date>`, and the date on anidb.net. Then work through the live-verification checklist below before step 8.
- [ ] 8. **Backfill:** R1 first (Plex-only), then the AniDB writes: `doppler run --project misc --config dev -- docker compose run --rm plex-anidb-reconciler apply --rules R1`, then `doppler run --project misc --config dev -- docker compose run --rm plex-anidb-reconciler apply --rules R2,R2b`, each repeated in batches (every run is capped by `MAX_WRITES`, default 500) until the summary shows no `capped` rows.
- [ ] 9. **Go live.** When the Shoko queue is empty:
  - set `MyList_UpdateFrequency=4`
  - deploy `serve`: `doppler run --project misc --config dev -- docker compose up -d`
  - in Tautulli add a Webhook notification agent on "Watched" -> `POST http://plex-anidb-reconciler:8787/trigger`

## Live verification (after the canary, before the backfill)

Stop at the first item that does not hold and report it; do not continue the backfill.

- [ ] **PATH_MAP prefix.** The canary row's Plex path, rewritten by `PATH_MAP`, is exactly the Shoko `AbsolutePath` of that file id (step 5); the `plan` summary shows only a handful of `no_shoko_match`/`ambiguous_path`.
- [ ] **No Plex webhook to Shoko.** Plex -> Settings -> Webhooks lists nothing pointing at Shoko (step 2); otherwise Plex plays reach AniDB twice.
- [ ] **Same instant everywhere.** The canary date in the CSV (`when`, UTC), the Shoko log line `UpdateMyListFileStatusJob ... | True | <date>` and the view date on anidb.net are the same instant (allowing for each one's timezone display).
- [ ] **Shoko `Created` is UTC.** `GET /api/v3/File/<id>` for the canary file returns `Created` with an offset (or `Z`). A naive value is read as `SHOKO_TZ`; if Shoko actually sends UTC without an offset, R2b import-time gating is shifted by the zone offset: stop and report.
- [ ] **Single-file re-check returns xrefs.** `GET /api/v3/File/<id>?include=XRefs,AbsolutePaths&includeXRefs=true&includeDataFrom=AniDB` for the canary file returns `SeriesIDs[].EpisodeIDs[]` and `AniDB.ID`. If it does not, every write fails safe as `failed`/`shoko_changed` (nothing is written): stop and report.
- [ ] **Does Plex `/:/scrobble` create a history row?** Before the first R1 apply, note the owner history count for one R1 item (`curl -s -H "X-Plex-Token: $PLEX_TOKEN" -H 'Accept: application/json' 'http://plex:32400/status/sessions/history/all?accountID=1&metadataItemID=<ratingKey>' | jq '.MediaContainer.size'`), run `apply --rules R1 --limit 1`, and check again. If the count went up, stop and report: R1-marked keys already ignore `lastViewedAt`, but these history rows would also have to be excluded before R2/R2b may run.

## Notes

- `pxar serve` runs an apply immediately at startup and on every container restart, so deploy it only after the backfill (step 8, go live in step 9). Fix config problems before enabling `restart: unless-stopped` loops: a crash-looping container would re-run apply on each start.
- `/trigger` has no authentication. Keep port 8787 unpublished and reachable only on the docker network.
- `GET /healthz` reports the last run, `"snapshot": "ok" | "stale" | "unavailable"` and `"loop": "ok" | "stale"`. A persistently `stale` snapshot means Shoko's daily MyList sync is not refreshing `mylist.json`: R2/R2b are deferred until it is. `"loop": "stale"` (HTTP 503) means the poll loop thread died or has not completed an iteration within 2 x `POLL_MINUTES` + 5 min (a stuck run); restart the container.
- The image has a Docker `HEALTHCHECK` on `/healthz` (healthy = HTTP 200). It only means something for `serve`; one-shot `plan`/`apply` containers never listen, so they may show `starting`/`unhealthy` while they run, which is harmless (`docker compose run` has no switch to disable it; with plain `docker run` add `--no-healthcheck`). It probes `127.0.0.1:8787`, so keep that port if you change `LISTEN`.
- Shutdown: on SIGTERM the service stops accepting triggers and waits up to `SHUTDOWN_GRACE_S` (default 25 s) for an in-flight run, logging a warning if it is still running; compose sets `stop_grace_period: 30s` to stay above it. A run cut off mid-way is safe: every applied write is already in the ledger, and the rest is re-planned next run.
- Exit codes: `0` ok, `1` config error, `2` a write failed or the run failed. On a Shoko/Plex failure the sanitized client message is printed (method, path and HTTP status or exception type, never secrets or query strings); for any other error only the exception type is printed.

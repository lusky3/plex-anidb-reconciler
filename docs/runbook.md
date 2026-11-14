# Rollout runbook

State lives in `/data` (host: `/opt/plex-anidb-reconciler`): `ledger.sqlite` (applied and failed writes only) and one report per run at `reports/<run_id>.csv` (the newest 200 are kept).

Secrets come from Doppler (`SHOKO_API_KEY` in project `misc`, config `dev`). Prefix commands with `doppler run --` or export the variables first.

- [ ] 0. **Host directories.** The container runs as uid 10001. Create the state dir with that owner, or the first run fails writing `ledger.sqlite` / `reports/` (Docker would create it root-owned): `sudo install -d -o 10001 -g 10001 /opt/plex-anidb-reconciler`. The MyList dir mounted at `/mylist:ro` must be readable by uid 10001: check with `ls -ln /opt/shoko/config/Shoko.CLI/MyList` (world-readable or a matching group).
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
- [ ] 6. **Dry run:** `docker compose run --rm plex-anidb-reconciler plan` (uncapped: every executable row shows `dry_run`). Review the CSV in `/data/reports/`. Before applying, check the R4 `no_shoko_match` and `ambiguous_path` counts in the stdout summary: they should be small (a handful of files Shoko does not know). Hundreds mean `PATH_MAP` is wrong: fix it and re-run `plan`.
- [ ] 7. **Canary:** `docker compose run --rm plex-anidb-reconciler apply --rules R2 --limit 1` (a plain `--limit 1` would only run an R1, which runs first). To pick a specific row from the dry-run CSV use `--file-id <file_id>` as well. Verify UserStats, the Shoko log line `UpdateMyListFileStatusJob ... | True | <date>`, and the date on anidb.net.
- [ ] 8. **Backfill:** R1 first (Plex-only), then the AniDB writes: `docker compose run --rm plex-anidb-reconciler apply --rules R1`, then `apply --rules R2,R2b`, each repeated in batches (every run is capped by `MAX_WRITES`, default 500) until the summary shows no `capped` rows.
- [ ] 9. **Go live.** When the Shoko queue is empty:
  - set `MyList_UpdateFrequency=4`
  - deploy `serve`: `docker compose up -d`
  - in Tautulli add a Webhook notification agent on "Watched" -> `POST http://plex-anidb-reconciler:8787/trigger`

## Notes

- `pxar serve` runs an apply immediately at startup and on every container restart, so deploy it only after the backfill (step 8, go live in step 9). Fix config problems before enabling `restart: unless-stopped` loops: a crash-looping container would re-run apply on each start.
- `/trigger` has no authentication. Keep port 8787 unpublished and reachable only on the docker network.
- `GET /healthz` reports the last run and `"snapshot": "ok" | "stale" | "unavailable"`. A persistently `stale` snapshot means Shoko's daily MyList sync is not refreshing `mylist.json`: R2/R2b are deferred until it is.
- Exit codes: `0` ok, `1` config error, `2` a write failed or the run failed (only the error type is printed).

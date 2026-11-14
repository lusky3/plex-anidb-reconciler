# Rollout runbook

State lives in `/data` (host: `/opt/plex-anidb-reconciler`): `ledger.sqlite` and one report per run at `reports/<run_id>.csv`.

Secrets come from Doppler (`SHOKO_API_KEY` in project `misc`, config `dev`). Prefix commands with `doppler run --` or export the variables first.

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
- [ ] 5. **Dry run:** `docker compose run --rm plex-anidb-reconciler plan`. Review the CSV in `/data/reports/`.
- [ ] 6. **Canary:** `docker compose run --rm plex-anidb-reconciler apply --limit 1` on an R2 row. Verify UserStats, the Shoko log line `UpdateMyListFileStatusJob ... | True | <date>`, and the date on anidb.net.
- [ ] 7. **Backfill:** `docker compose run --rm plex-anidb-reconciler apply`, repeated in batches (each run is capped by `MAX_WRITES`, default 500).
- [ ] 8. **Go live.** When the Shoko queue is empty:
  - set `MyList_UpdateFrequency=4`
  - deploy `serve`: `docker compose up -d`
  - in Tautulli add a Webhook notification agent on "Watched" -> `POST http://plex-anidb-reconciler:8787/trigger`

## Notes

- `pxar serve` runs an apply immediately at startup and on every container restart, so deploy it only after the backfill (step 8). Fix config problems before enabling `restart: unless-stopped` loops: a crash-looping container would re-run apply on each start.
- `/trigger` has no authentication. Keep port 8787 unpublished and reachable only on the docker network.

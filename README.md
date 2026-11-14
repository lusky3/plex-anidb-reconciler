# plex-anidb-reconciler

Reconciles Plex watched flags with AniDB MyList via Shoko. Console script: `pxar`.

Status: work in progress. Dry-run by default; writes only with `--apply`.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `SHOKO_URL` | required | Shoko server URL |
| `SHOKO_API_KEY` | required | Shoko API key |
| `SHOKO_TZ` | `America/New_York` | Timezone for dates sent to Shoko |
| `PLEX_URL` | required | Plex server URL |
| `PLEX_TOKEN` | required | Plex token |
| `PLEX_SECTIONS` | `4,5` | Comma-separated Plex library section ids |
| `MYLIST_PATH` | `/mylist/mylist.json` | AniDB MyList snapshot |
| `DATA_DIR` | `/data` | State directory |
| `PATH_MAP` | empty | `plexprefix=shokoprefix[;...]` |
| `R2B_POLICY` | `record` | `record` or `ignore` |
| `SNAPSHOT_MAX_AGE_H` | `26` | Max snapshot age in hours for write rules |
| `MAX_WRITES` | `500` | Apply cap per run |
| `POLL_MINUTES` | `15` | Poll interval |
| `SHOKO_INDEX_TTL_MIN` | `60` | Shoko index cache TTL |
| `LISTEN` | `0.0.0.0:8787` | Listen address |

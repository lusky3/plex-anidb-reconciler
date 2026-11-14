# Configuration

All settings come from environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHOKO_URL` | required | Shoko server URL |
| `SHOKO_API_KEY` | required | Shoko API key (secret) |
| `PLEX_URL` | required | Plex server URL |
| `PLEX_TOKEN` | required | Plex token (secret) |
| `PLEX_SECTIONS` | required | Comma-separated Plex library section ids to reconcile, e.g. `3` or `3,7` |
| `SHOKO_TZ` | `UTC` | Timezone Shoko runs in; used for naive dates from and to Shoko |
| `MYLIST_PATH` | `/mylist/mylist.json` | MyList snapshot written by Shoko |
| `DATA_DIR` | `/data` | State directory: ledger, reports, Shoko index cache |
| `PATH_MAP` | empty | `plexprefix=shokoprefix[;...]`: rewrites Plex paths to Shoko paths |
| `R2B_POLICY` | `record` | `record` writes R2b; `ignore` only reports it |
| `SNAPSHOT_MAX_AGE_H` | `26` | Max snapshot age in hours for R2/R2b (>= 1) |
| `MAX_WRITES` | `500` | Write cap per `apply` run (>= 1) |
| `POLL_MINUTES` | `15` | `serve` poll interval (>= 1) |
| `SHOKO_INDEX_TTL_MIN` | `60` | Shoko file index cache TTL in minutes; `0` fetches every run |
| `LISTEN` | `0.0.0.0:8787` | `serve` listen address: `host:port` or `[ipv6]:port` |
| `RUN_STALE_MINUTES` | `360` | A run active longer than this makes `/healthz` return 503 (>= 1) |
| `SHUTDOWN_GRACE_S` | `25` | Seconds `serve` waits for an in-flight run on SIGTERM (>= 0) |

Rules:

- Required variables must be set and non-blank.
- A blank optional variable means its default, except `SHOKO_TZ`: blank is an error.
- `PLEX_SECTIONS` takes positive integers; duplicates are dropped. Find a section id in Plex Web: open the
  library and read `source=` in the URL.
- `PATH_MAP` sides are trimmed and must be non-empty. See [deployment step 5](deployment.md#5-path-map).
- Integers must be plain decimal digits.
- An invalid value stops the program with exit code 1 and a config error that names the variable, never its
  value.

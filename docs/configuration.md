# Configuration

Every setting comes from an environment variable.

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
| `HEALTH_MAX_FAILED_RUNS` | `3` | This many failed runs in a row make `/healthz` return 503 (>= 1) |
| `SHUTDOWN_GRACE_S` | `25` | Seconds `serve` waits for an in-flight run on SIGTERM (>= 0); keep it at 20 or more |

Required variables must be set and non-blank. A blank optional variable falls back to its default, with one
exception: a blank `SHOKO_TZ` is an error.

`PLEX_SECTIONS` takes positive integers and drops duplicates; [deployment](deployment.md#prerequisites) shows how
to list your section ids. `PATH_MAP` sides get trimmed and must be non-empty; see
[deployment step 5](deployment.md#5-path-map). Integers must be plain decimal digits.

A run counts as failed for `HEALTH_MAX_FAILED_RUNS` when it raises an error, has a failed write, or finds no usable
MyList snapshot. One successful run resets the count.

A request on the write path (the re-reads right before a write, the Shoko PUT, the Plex scrobble) gives up after
about 20 seconds: 5 s to connect, 5 s to send, 10 s waiting for the reply. The default `SHUTDOWN_GRACE_S` of 25
leaves room for one of them. The reads that plan a run wait up to 60 s for data, since a big Shoko file index can take
a while.

An invalid value stops the program with exit code 1 and a config error that names the variable, never its value.

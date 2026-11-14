# plex-anidb-reconciler

Reconciles Plex watched flags with AniDB MyList via Shoko. Console script: `pxar`.

Status: work in progress. Dry-run by default; writes only with `--apply`.

## Usage

```
pxar plan  [--rules R1,R2,R2b] [--file-id N]             # dry run (uncapped), writes a CSV report
pxar apply [--rules R1,R2,R2b] [--file-id N] [--limit N] # apply changes (capped by MAX_WRITES)
pxar serve                                                # periodic runs plus POST /trigger on LISTEN
```

`--rules` and `--file-id` restrict which writes run; reports are always listed. `apply` runs R1 before
R2, and every R2 before any R2b, so a canary write to AniDB is `pxar apply --rules R2 --limit 1`. An R2b
that relies on an R2 planned in the same run is only executed once that R2 was applied in this run;
otherwise it is listed as `skipped` (`detail` column: `prerequisite_not_applied`) and reconsidered next run.

## Docker

Build with `docker build -t pxar .`; the image runs `pxar serve` by default. See `compose.example.yml` for a Saltbox deployment and [docs/runbook.md](docs/runbook.md) for the rollout checklist.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `SHOKO_URL` | required | Shoko server URL |
| `SHOKO_API_KEY` | required | Shoko API key |
| `SHOKO_TZ` | `America/New_York` | Timezone for dates sent to Shoko |
| `PLEX_URL` | required | Plex server URL |
| `PLEX_TOKEN` | required | Plex token |
| `PLEX_SECTIONS` | `4,5` | Comma-separated positive Plex library section ids (must not be empty) |
| `MYLIST_PATH` | `/mylist/mylist.json` | AniDB MyList snapshot |
| `DATA_DIR` | `/data` | State directory |
| `PATH_MAP` | empty | `plexprefix=shokoprefix[;...]` |
| `R2B_POLICY` | `record` | `record` or `ignore` |
| `SNAPSHOT_MAX_AGE_H` | `26` | Max snapshot age in hours for write rules (>= 1) |
| `MAX_WRITES` | `500` | Apply cap per run (>= 1) |
| `POLL_MINUTES` | `15` | Poll interval (>= 1) |
| `SHOKO_INDEX_TTL_MIN` | `60` | Shoko index cache TTL in minutes; `0` = no cache (fetch every run) |
| `LISTEN` | `0.0.0.0:8787` | Listen address: `host:port` or `[ipv6]:port`, port 1-65535 |
| `SHUTDOWN_GRACE_S` | `25` | Seconds `serve` waits for an in-flight run on SIGTERM (>= 0) |

Blank optional variables fall back to their defaults, with two exceptions: a blank `SHOKO_TZ` or `PLEX_SECTIONS` is a config error, and the required variables (`SHOKO_URL`, `SHOKO_API_KEY`, `PLEX_URL`, `PLEX_TOKEN`) must be non-blank. Invalid values stop the program with a config error
(exit 1) that names the variable, never its value. `PATH_MAP` sides are trimmed and must be non-empty.

## Accepted risks

- **R2 credits the earliest Plex play to the current file (ruling 10).** Plex history has no file identity.
  R2 writes the earliest owner play (or `lastViewedAt`) of a Plex item to the Shoko file behind that item now,
  even if the play was of an older file of that episode that AniDB never recorded. R2b (episode already
  watched via another file) only counts plays after both the AniDB date and the current file's import time.
- **Snapshot freshness.** R2/R2b trust `mylist.json` up to `SNAPSHOT_MAX_AGE_H` (26 h) old. An episode first
  watched through another AniDB client inside that window can get a second dated MyList entry for the file
  watched in Plex.
- **`/trigger` is unauthenticated.** It only queues a run, but anyone who can reach port 8787 can trigger one.
  Keep the port unpublished and reachable on the docker network only.

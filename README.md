# plex-anidb-reconciler

Reconciles Plex watched flags with AniDB MyList via Shoko. Console script: `pxar`.

Status: work in progress. Dry-run by default; writes only with `--apply`.

## Usage

```
pxar plan  [--rules R1,R2,R2b] [--file-id N]             # dry run (uncapped), writes a CSV/HTML report
pxar apply [--rules R1,R2,R2b] [--file-id N] [--limit N] # apply changes (capped by MAX_WRITES)
pxar serve                                                # periodic runs plus POST /trigger on LISTEN
```

`serve` runs a full reconcile every `POLL_MINUTES`. `POST /trigger` queues an immediate run: with a JSON body
`{"rating_key": "123"}` (Tautulli Webhook agent, JSON data `{"rating_key": "{rating_key}"}`) only that Plex item
is planned; without a usable key it is a full run. Triggers that arrive during a run are merged into the next
run (the union of their rating keys, or a full run if any was keyless).

Each run writes `reports/<run_id>.csv` and a self-contained `reports/<run_id>.html` (summary counts, then the
same rows; the newest 200 of each are kept) under `DATA_DIR`, one row per action: `rule, kind, status,
rating_key, file_id, fid, eids, when, reason, detail, aid, release_group, anidb_state, anidb_viewdate`.
`aid` and `release_group` come from Shoko (`aid` falls back to the MyList snapshot); `anidb_state` (MyList
`State`: 1 HDD, 2 disk, 3 deleted, 4 remote) and `anidb_viewdate` (UTC, empty if unwatched) are what the
snapshot says about the row's fid, empty when the fid is not in it. `ledger.sqlite` keeps every applied
or failed write with `before`/`after` JSON for audit.

`--rules` and `--file-id` restrict which writes run; reports are always listed. `apply` runs R1 before
R2, and every R2 before any R2b, so a canary write to AniDB is `pxar apply --rules R2 --limit 1`. An R2b
that relies on an R2 planned in the same run is only executed once that R2 was applied in this run;
otherwise it is listed as `skipped` (`detail` column: `prerequisite_not_applied`) and reconsidered next run.

Right before each Shoko write the reconciler re-reads the file (`GET /api/v3/File/{id}`: same fid, episodes
and path, else `failed`/`shoko_changed`) and its watched state (`GET /api/v3/File/{id}/UserStats`). If Shoko
already has a `LastWatchedAt` or a non-zero `WatchedCount` for that file, nothing is written: the row is
`skipped` with detail `shoko_already_watched` (an existing date is never overwritten; not a failure, no ledger row).

## Docker

Images are published to `ghcr.io/lusky3/plex-anidb-reconciler` (`latest` and `sha-<commit>` from `main`, also rebuilt weekly; `X.Y.Z`/`X.Y` and `sha-<commit>` from `vX.Y.Z` tags; `latest` never moves on a release tag) with SBOM and provenance attestations; inspect them with `docker buildx imagetools inspect ghcr.io/lusky3/plex-anidb-reconciler:latest --format '{{ json .SBOM }}'`. Local build: `docker build -t pxar .`. The image runs `pxar serve` by default.

The image is hardened: a distroless runtime (`gcr.io/distroless/cc-debian13`: glibc, libgcc/libstdc++, CA certificates and tzdata; no shell, no package manager, no pip) with a uv-managed standalone CPython 3.14, running as uid 10001. Base images are pinned by digest and kept current by Dependabot. The compose example adds a read-only root filesystem (with a small `noexec` tmpfs at `/tmp`), drops all capabilities, sets `no-new-privileges` and a PID limit. See `compose.example.yml` for a Saltbox deployment and [docs/runbook.md](docs/runbook.md) for the rollout checklist.

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
| `PATH_MAP` | empty | `plexprefix=shokoprefix[;...]`. A rewritten Plex path must equal a Shoko absolute path; a match on parent folder + filename only is reported as R4 `fallback_match` and never written |
| `R2B_POLICY` | `record` | `record` or `ignore` |
| `SNAPSHOT_MAX_AGE_H` | `26` | Max snapshot age in hours for write rules (>= 1) |
| `MAX_WRITES` | `500` | Apply cap per run (>= 1) |
| `POLL_MINUTES` | `15` | Poll interval (>= 1) |
| `SHOKO_INDEX_TTL_MIN` | `60` | Shoko index cache TTL in minutes; `0` = no cache (fetch every run) |
| `LISTEN` | `0.0.0.0:8787` | Listen address: `host:port` or `[ipv6]:port`, port 1-65535 |
| `RUN_STALE_MINUTES` | `360` | `/healthz` turns 503 (`"loop": "stale"`) when one run has been active longer than this (>= 1) |
| `SHUTDOWN_GRACE_S` | `25` | Seconds `serve` waits for an in-flight run on SIGTERM (>= 0) |

Blank optional variables fall back to their defaults, with two exceptions: a blank `SHOKO_TZ` or `PLEX_SECTIONS` is a config error, and the required variables (`SHOKO_URL`, `SHOKO_API_KEY`, `PLEX_URL`, `PLEX_TOKEN`) must be non-blank. Invalid values stop the program with a config error
(exit 1) that names the variable, never its value. `PATH_MAP` sides are trimmed and must be non-empty.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on pull requests and pushes to `main`:

- **test**: `ruff check`, `ruff format --check`, `pytest` with coverage; `coverage.xml` is uploaded to Codecov (needs the `CODECOV_TOKEN` repository secret; upload failures don't fail CI). Pytest treats `ResourceWarning` as an error, so leaked files, sockets or DB connections fail the suite.
- **image** (after `test` passes): builds with Docker Buildx and smoke-tests the image under hardened runtime flags (read-only root, no capabilities, no-new-privileges; it must have no shell and run as uid 10001; a config error must exit 1 without echoing env values). A Trivy scan fails the job on any fixable HIGH/CRITICAL vulnerability in the OS packages or the app's Python dependencies. Trivy cannot see inside the standalone CPython (or its statically linked OpenSSL/SQLite): interpreter fixes arrive when the digest-pinned `uv` image is bumped, so merge those Dependabot PRs promptly. On pull requests the SBOM (SPDX) and max-mode provenance are exported as the `sbom-<sha>` artifact and nothing is pushed; that job only has a read-only token.
- **publish** (pushes to `main`, `v*` tags and a weekly scheduled rebuild; never pull requests): pushes to GHCR with the SBOM and provenance attestations attached and OCI labels/annotations from `docker/metadata-action`, then smoke-tests and Trivy-scans the pushed digest itself (it is built without the build cache, so it is not the image the `image` job tested) and fails if either attestation is missing from the pushed image. Only this job gets `packages: write`.
- All actions are pinned to commit SHAs; Dependabot (`.github/dependabot.yml`) updates actions, base-image digests and `uv.lock` weekly.

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

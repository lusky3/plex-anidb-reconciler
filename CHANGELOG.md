# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-14

First public release.

### Added

- `pxar plan` (dry run), `apply`, `serve` and `healthcheck`. Exit codes: 0 ok, 1 config error, 2 run or write
  failure, 3 MyList snapshot unavailable.
- Per-file reconciliation between Plex and AniDB MyList through Shoko: R1 marks a Plex item played when AniDB has
  all its episodes watched, R2 gives a file its first Plex watch date, R2b records a real replay of a newer release,
  and R4 reports everything ambiguous without writing.
- Path matching with `PATH_MAP`, exact matches only for writes.
- R2 dates a file from Plex `lastViewedAt` only when there's no play history and that date isn't older than the
  file's Shoko import; otherwise it reports `predates_file` or `unknown_file_age`.
- `serve`: a full run every `POLL_MINUTES`, `POST /trigger` for Tautulli (optionally scoped to one Plex item), and
  `/healthz`, which turns 503 when the loop stalls, a run hangs past `RUN_STALE_MINUTES`, or
  `HEALTH_MAX_FAILED_RUNS` runs in a row fail.
- CSV and HTML reports for every run, and a SQLite ledger with each write's before and after state.
- Container image on GHCR (`X.Y.Z`, `X.Y`, `sha-…`, and `latest` from main) with SBOM and provenance attestations.
  CI pushes the image by digest, smoke-tests it, scans it and checks its attestations, and only then tags it.

### Security

- The only writes are Shoko `PUT /api/v3/File/{id}/UserStats` and Plex `/:/scrobble`. Nothing is ever marked
  unwatched, and an existing Shoko or AniDB watched date is never overwritten.
- An R1 is written to the ledger as `pending` before its Plex scrobble and still counts if the request fails, so
  neither a crash mid-write nor a timed-out scrobble can turn the scrobble time into an AniDB date.
- API key and token never appear in logs, error messages, reports or `/healthz`; config errors name the variable,
  not its value.
- The image is distroless with no shell, runs as uid 10001 on a read-only root filesystem, and CI fails on any
  fixable HIGH or CRITICAL vulnerability.

[Unreleased]: https://github.com/lusky3/plex-anidb-reconciler/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/lusky3/plex-anidb-reconciler/releases/tag/v0.1.0

# plex-anidb-reconciler

Reconciles Plex watched state with AniDB MyList through Shoko Server. Plex tracks watched state per library
item; AniDB tracks it per file, with a view date. This tool reads Plex, Shoko and a MyList snapshot, plans
per-file changes in both directions, and writes only the ones it can prove are safe. The console script is
`pxar`.

## What it guarantees

- Writes are per file: one Shoko file gets one watched date, never a whole episode or series.
- An existing AniDB or Shoko watched date is never overwritten.
- Nothing is ever marked unwatched, in Plex or in AniDB.
- Ambiguous paths and episode links are reported, not acted on. A Plex item backed by several files is never
  written to AniDB; it is only marked played in Plex when AniDB already shows all its episodes watched.
- `plan` is a dry run. Nothing is written without `apply` or `serve`.
- Every write is recorded in a ledger with its before and after state.

## Requirements

- Plex Media Server
- Shoko Server 5.3.3 with AniDB MyList enabled
- Docker with Compose
- Tautulli (optional, for a run right after each watch)

## Quick start

```sh
cp compose.example.yml compose.yml   # set host paths, network and PLEX_SECTIONS
cp .env.example .env                 # fill in SHOKO_API_KEY and PLEX_TOKEN
sudo install -d -o 10001 -g 10001 /path/to/state-dir   # container runs as uid 10001
docker compose pull
docker compose run --rm plex-anidb-reconciler plan
```

Review the report, then follow the [deployment guide](docs/deployment.md) before you run `apply` or `serve`.

## Commands

| Command | What it does |
| --- | --- |
| `plan [--rules R] [--file-id N]` | Dry run. Writes a CSV/HTML report. Uncapped. |
| `apply [--rules R] [--file-id N] [--limit N]` | Executes the plan, capped by `MAX_WRITES`. |
| `serve` | Applies every `POLL_MINUTES` and on `POST /trigger`; serves `/healthz`. |
| `healthcheck` | Container probe: exit 0 if the local `/healthz` answers 200. |

`--rules` takes a comma list of `R1`, `R2`, `R2b`. The image runs `serve` by default.

## Documentation

- [How it works](docs/how-it-works.md): matching, rules, safety checks, reports, accepted risks
- [Configuration](docs/configuration.md): environment variables
- [Deployment](docs/deployment.md): Shoko settings, rollout checklist, operations
- [Development](docs/development.md): tests, image hardening, CI

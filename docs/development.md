# Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run pytest --cov=plex_anidb_reconciler
```

Pytest treats `ResourceWarning` as an error, so leaked files, sockets or database connections fail the
suite.

Local image: `docker build -t pxar .`

## Container hardening

- Runtime base is distroless `gcr.io/distroless/cc-debian13` (glibc, libstdc++, CA certificates, tzdata). No
  shell, no package manager, no pip.
- Python is a uv-managed standalone CPython 3.14, because distroless `python3` ships Debian's 3.11. Unused
  parts (Tk, IDLE, headers, pip) are removed.
- Runs as uid 10001.
- Base images are pinned by digest.
- `compose.example.yml` adds a read-only root filesystem, a small `noexec` tmpfs at `/tmp`, `cap_drop: ALL`,
  `no-new-privileges` and a PID limit.

## CI

`.github/workflows/ci.yml` runs on pull requests, pushes to `main`, `v*` tags and a weekly schedule.

- **test**: ruff, format check, pytest with coverage. `coverage.xml` goes to Codecov (needs the
  `CODECOV_TOKEN` secret; upload failures do not fail CI).
- **image** (after test): builds the image and smoke-tests it under hardened flags. It must have no shell,
  run as uid 10001, and exit 1 on a config error without echoing env values. Trivy fails the job on any
  fixable HIGH/CRITICAL vulnerability. On pull requests the SBOM (SPDX) and provenance are uploaded as the
  `sbom-<sha>` artifact; nothing is pushed and the token is read-only.
- **publish** (pushes and schedule, never pull requests): builds without cache, pushes to GHCR with SBOM and
  provenance attestations and OCI labels, then smoke-tests and Trivy-scans the pushed digest and checks both
  attestations exist. Only this job has `packages: write`.

Tags: `latest` and `sha-<commit>` from `main` (also rebuilt weekly); `X.Y.Z`, `X.Y` and `sha-<commit>` from
`vX.Y.Z` tags. `latest` never moves on a release tag. Inspect the SBOM with
`docker buildx imagetools inspect ghcr.io/lusky3/plex-anidb-reconciler:latest --format '{{ json .SBOM }}'`.

**Trivy scope.** Trivy covers the OS packages and the app's Python dependencies. It cannot see inside the
standalone CPython or its statically linked OpenSSL and SQLite. Those fixes arrive when the digest-pinned
`uv` image is bumped, so merge those Dependabot PRs promptly.

## Dependabot

`.github/dependabot.yml` updates weekly: GitHub Actions (pinned to commit SHAs), Dockerfile base-image
digests, and `uv.lock`.

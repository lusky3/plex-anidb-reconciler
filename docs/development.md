# Development

You'll need [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run pytest --cov=plex_anidb_reconciler
```

Pytest treats `ResourceWarning` as an error, so a leaked file, socket or database connection fails the suite.

Build a local image with `docker build -t pxar .`.

## Container hardening

The runtime base is distroless `gcr.io/distroless/cc-debian13`, which brings glibc, libstdc++, CA certificates and
tzdata but no shell, no package manager and no pip. Python comes from a uv-managed standalone CPython 3.14, because
distroless `python3` ships Debian's 3.11; the build strips the parts nothing uses (Tk, IDLE, headers, pip).

The container runs as uid 10001, and the Dockerfile pins every base image by digest. On top of that, `compose.example.yml`
adds a read-only root filesystem, a small `noexec` tmpfs at `/tmp`, `cap_drop: ALL`, `no-new-privileges` and a PID
limit.

## CI

`.github/workflows/ci.yml` runs on pull requests, pushes to `main`, `v*` tags and a weekly schedule.

**test** runs ruff, the format check and pytest with coverage, then sends `coverage.xml` to Codecov. That upload needs
the `CODECOV_TOKEN` secret, and a failed upload doesn't fail CI.

**image** runs after test. It builds the image and smoke-tests it under hardened flags: the image must have no shell,
run as uid 10001, and exit 1 on a config error without echoing env values. Trivy fails the job on any fixable
HIGH/CRITICAL vulnerability. On pull requests the job uploads the SBOM (SPDX) and provenance as the `sbom-<sha>`
artifact; it pushes nothing, and its token is read-only.

**publish** runs on pushes and the schedule, never on pull requests. It builds without cache and pushes to GHCR with
SBOM and provenance attestations and OCI labels. Then it smoke-tests and Trivy-scans the pushed digest and checks
that both attestations exist. No other job has `packages: write`.

Builds from `main` get `latest` and `sha-<commit>` (and get rebuilt weekly); a `vX.Y.Z` tag produces `X.Y.Z`, `X.Y`
and `sha-<commit>`. `latest` never moves on a release tag. To inspect the SBOM, run
`docker buildx imagetools inspect ghcr.io/lusky3/plex-anidb-reconciler:latest --format '{{ json .SBOM }}'`.

Trivy covers the OS packages and the app's Python dependencies, but it can't see inside the standalone CPython or its
statically linked OpenSSL and SQLite. Fixes for those arrive when someone bumps the digest-pinned `uv` image, so merge
those Dependabot PRs promptly.

## Dependabot

`.github/dependabot.yml` runs weekly updates for GitHub Actions (pinned to commit SHAs), the Dockerfile base-image
digests and `uv.lock`.

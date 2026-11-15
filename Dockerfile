# Runtime is distroless cc (glibc, libgcc/libstdc++, CA certs, tzdata; no shell, no package manager) with a
# uv-managed standalone CPython 3.14, because distroless/python3 ships Debian's 3.11.
# Each FROM pins its base image by digest, and Dependabot keeps the digests current.

FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
# Interpreter version comes from .python-version (3.14).
COPY .python-version pyproject.toml uv.lock README.md LICENSE ./
RUN uv python install \
    && uv sync --locked --no-dev --no-install-project
COPY src ./src
# Install the project, then drop parts of the standalone CPython the service never uses (GUI, IDE,
# headers, installers). pip goes too: the runtime needs no package installer, and its vendored
# certifi/urllib3/msgpack lag behind the app's own (locked) dependencies.
RUN set -eu \
    && uv sync --locked --no-dev --no-editable \
    && mkdir -p /out/data \
    && py="$(readlink -f /app/.venv/bin/python | sed 's#/bin/python[0-9.]*$##')/" \
    && test -d "${py}lib" \
    && rm -rf /python/.lock /python/.temp /python/.gitignore \
        "$py"include "$py"share "$py"lib/tcl* "$py"lib/tk* "$py"lib/itcl* "$py"lib/thread* \
        "$py"lib/python3.*/idlelib "$py"lib/python3.*/tkinter "$py"lib/python3.*/turtledemo \
        "$py"lib/python3.*/turtle.py "$py"lib/python3.*/ensurepip "$py"lib/python3.*/pydoc_data \
        "$py"lib/python3.*/lib-dynload/_tkinter*.so \
        "$py"lib/python3.*/site-packages/pip "$py"lib/python3.*/site-packages/pip-*.dist-info \
        "$py"bin/pip*

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:cb87acc574abe719b6e5c547384defa2f7b1f24af9605d3f618cf852495d8be2
LABEL org.opencontainers.image.title="plex-anidb-reconciler" \
      org.opencontainers.image.description="Reconciles Plex watched state with AniDB MyList per-file watched dates via Shoko" \
      org.opencontainers.image.source="https://github.com/lusky3/plex-anidb-reconciler" \
      org.opencontainers.image.url="https://github.com/lusky3/plex-anidb-reconciler" \
      org.opencontainers.image.documentation="https://github.com/lusky3/plex-anidb-reconciler/blob/main/docs/deployment.md" \
      org.opencontainers.image.vendor="lusky3" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="gcr.io/distroless/cc-debian13:nonroot"
# The venv's interpreter symlinks into /python, so it must keep the same path.
COPY --from=build /python /python
COPY --from=build /app/.venv /app/.venv
COPY --from=build --chown=10001:10001 /out/data /data
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# uid 10001 (not distroless's 65532) so the host /data ownership in docs/deployment.md step 0 stays valid.
USER 10001:10001
WORKDIR /data
EXPOSE 8787
# Only meaningful for `serve`: healthy means /healthz answers 200 (loop alive and recent), and the probe follows LISTEN.
# One-shot `plan`/`apply` containers never listen and would report unhealthy. That's harmless, or run them with
# --no-healthcheck.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["pxar", "healthcheck"]
ENTRYPOINT ["pxar"]
CMD ["serve"]

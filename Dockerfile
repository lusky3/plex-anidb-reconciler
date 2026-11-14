FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim
RUN useradd --system --uid 10001 --no-create-home pxar \
    && mkdir /data && chown pxar /data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER pxar
EXPOSE 8787
# Only meaningful for `serve` (healthy = /healthz 200: loop alive and recent). One-shot `plan`/`apply`
# containers never listen, so they would report unhealthy: harmless, or run them with --no-healthcheck.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=5).status == 200 else 1)"]
ENTRYPOINT ["pxar"]
CMD ["serve"]

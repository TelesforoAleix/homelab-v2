# Two stages: resolve and install dependencies with uv, then copy the ready venv into a slim
# runtime image that runs as an unprivileged user. Rebuilds reuse uv's cache.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY config ./config
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

FROM python:3.13-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin homelab
WORKDIR /app
COPY --from=builder --chown=homelab:homelab /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOMELAB_ROUTES_FILE=/app/config/routes.yaml
USER homelab
EXPOSE 8000
CMD ["uvicorn", "homelab.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

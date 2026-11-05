# Tools for one coding task; homelab-task-run decides everything else. Holds no secret.
# Same pinned base as homelab-context. Deliberate digest updates only.
FROM node:22-bookworm-slim@sha256:9f0dde62d10e7ff7a310ecaebd66225f6b46c17e6cb778449a77b05d3541bea2
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git libpq5 python3 python3-venv ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/bootstrap && /opt/bootstrap/bin/pip install --no-cache-dir uv==0.9.30
ENV PATH=/work/.venv/bin:/opt/bootstrap/bin:/usr/local/bin:/usr/bin:/bin \
    UV_PYTHON_INSTALL_DIR=/opt/python
# This uv release names the exact Python 3.13 it installs.
RUN uv python install 3.13
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.4.0 \
    && npm cache clean --force
# Pi 1.4.0 needs Node 22.19 or later; refuse a base that cannot run it.
RUN node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit(a > 22 || (a === 22 && b >= 19) ? 0 : 1)' \
    && test "$(pi --version)" = 1.4.0
ENV HOME=/home/task \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_LINK_MODE=copy \
    UV_PYTHON=3.13 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-managed \
    PI_OFFLINE=1 \
    PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0
WORKDIR /work

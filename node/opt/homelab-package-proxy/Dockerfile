# The forward proxy for a task's prepare step. Same pinned base as homelab-task; holds no secret.
FROM node:22-bookworm-slim@sha256:9f0dde62d10e7ff7a310ecaebd66225f6b46c17e6cb778449a77b05d3541bea2
RUN apt-get update \
    && apt-get install -y --no-install-recommends tinyproxy-bin \
    && rm -rf /var/lib/apt/lists/*
COPY tinyproxy.conf package-hosts /etc/tinyproxy/
USER node
EXPOSE 8888
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]

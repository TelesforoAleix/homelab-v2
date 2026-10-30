FROM node:22-bookworm-slim@sha256:9f0dde62d10e7ff7a310ecaebd66225f6b46c17e6cb778449a77b05d3541bea2
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/bootstrap && /opt/bootstrap/bin/pip install uv==0.9.30
ENV PATH=/opt/bootstrap/bin:/opt/serena/.venv/bin:/usr/local/bin:/usr/bin:/bin
RUN git clone https://github.com/oraios/serena.git /opt/serena && cd /opt/serena && git checkout --detach 5e4f5cd173c7ff3eed37ee3f1de6c266b937c5dc && uv sync --frozen --no-dev && rm -rf .git
RUN npm install -g pyright@1.1.403
RUN SERENA_HOME=/opt/context-deps serena download-ls-dependencies typescript
ENV SERENA_HOME=/tmp/serena HOME=/tmp PYTHONDONTWRITEBYTECODE=1
COPY serena_config.yml /opt/context/serena_config.yml
RUN /opt/serena/.venv/bin/python -c 'import yaml; p=yaml.safe_load(open("/opt/serena/src/serena/resources/project.template.yml")); p.update(project_name="context", language_servers=["python", "typescript"], read_only=True); yaml.safe_dump(p, open("/opt/context/project.yml", "w"))'
COPY start /opt/context/start
ENTRYPOINT ["/bin/sh", "/opt/context/start"]

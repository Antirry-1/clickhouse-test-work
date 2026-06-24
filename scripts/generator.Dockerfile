# Baked image for the data generator: deps (clickhouse-connect, numpy, pandas)
# are installed at build time so `make generate` no longer pip-installs each run.
# The generator code itself stays bind-mounted at runtime (./:/work:ro) — not copied in.
FROM python:3.12-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

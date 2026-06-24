# Superset + ClickHouse driver. The base image does NOT ship clickhouse-connect,
# which provides the `clickhousedb://` SQLAlchemy dialect Superset needs to read ClickHouse.
ARG SUPERSET_IMAGE_TAG=4.1.1
FROM apache/superset:${SUPERSET_IMAGE_TAG}

USER root
# clickhouse-connect provides the `clickhousedb://` SQLAlchemy dialect Superset uses.
# Pin SQLAlchemy < 2 so the driver does not drag in SQLAlchemy 2.x (Superset 4.x needs 1.4.x).
RUN pip install --no-cache-dir "clickhouse-connect==0.8.*" "sqlalchemy<2"
# curl is used by the container healthcheck.
RUN apt-get update -y && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
USER superset

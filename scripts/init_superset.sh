#!/usr/bin/env bash
# One-shot Superset bootstrap (no running server needed): migrate metadata DB,
# create the admin user, initialize roles, and register the ClickHouse connection.
# The dashboard itself is built by scripts/build_dashboard.py (REST API) once the
# web server is healthy. Safe to re-run.
set -euo pipefail

ADMIN="${SUPERSET_ADMIN:-admin}"
ADMIN_PW="${SUPERSET_ADMIN_PASSWORD:-admin}"
ADMIN_EMAIL="${SUPERSET_ADMIN_EMAIL:-admin@example.com}"

echo "[superset-init] 1/4 db upgrade"
superset db upgrade

echo "[superset-init] 2/4 create admin ($ADMIN)"
superset fab create-admin \
  --username "$ADMIN" --firstname Admin --lastname User \
  --email "$ADMIN_EMAIL" --password "$ADMIN_PW" || true

echo "[superset-init] 3/4 superset init (roles & permissions)"
superset init

echo "[superset-init] 4/4 register ClickHouse connection"
python /app/scripts/register_db.py

echo "[superset-init] done."

#!/usr/bin/env bash
# Apply the ClickHouse DDL (create database + table + view). Idempotent.
# Runs inside the clickhouse-server image (clickhouse-client is on PATH).
set -euo pipefail

CH_HOST="${CLICKHOUSE_HOST:-clickhouse}"
CH_PORT="${CLICKHOUSE_NATIVE_PORT:-9000}"
CH_USER="${CLICKHOUSE_USER:-default}"
CH_PASS="${CLICKHOUSE_PASSWORD:-clickhouse}"
DDL_FILE="${DDL_FILE:-/ddl/001_create_messages_mart.sql}"
DAILY_DDL_FILE="${DAILY_DDL_FILE:-/ddl/002_create_messages_daily_mv.sql}"

ch() {
  clickhouse-client --host "${CH_HOST}" --port "${CH_PORT}" \
    --user "${CH_USER}" --password "${CH_PASS}" "$@"
}

echo "[init_clickhouse] applying ${DDL_FILE} to ${CH_HOST}:${CH_PORT} ..."
ch --multiquery < "${DDL_FILE}"

echo "[init_clickhouse] applying ${DAILY_DDL_FILE} ..."
ch --multiquery < "${DAILY_DDL_FILE}"

# One-time backfill: a MATERIALIZED VIEW only captures inserts made AFTER it
# exists. On a fresh `up` the DDL above runs before the generator, so the MV
# captures everything — messages_daily is empty here and the backfill is a
# harmless no-op. On an already-populated base table the MV missed earlier
# inserts, so we backfill once. Guarded on empty so reruns never double-insert.
if [ "$(ch --query "SELECT count() FROM sms.messages_daily")" = "0" ]; then
  if [ "$(ch --query "SELECT count() FROM sms.messages_mart WHERE deleted_at IS NULL")" != "0" ]; then
    echo "[init_clickhouse] backfilling sms.messages_daily from existing base rows ..."
    ch --query "INSERT INTO sms.messages_daily
      SELECT toDate(sent_date), country, delivery_status, currency,
             countState(),
             countIfState(toUInt8(coalesce(delivery_status = 'DELIVRD', 0))),
             sumState(price)
      FROM sms.messages_mart
      WHERE deleted_at IS NULL
      GROUP BY 1, 2, 3, 4"
  fi
fi

echo "[init_clickhouse] verifying objects exist ..."
ch --query "EXISTS TABLE sms.messages_mart" | grep -q 1
ch --query "EXISTS sms.messages_mart_active" | grep -q 1
ch --query "EXISTS TABLE sms.messages_daily" | grep -q 1
ch --query "EXISTS sms.messages_daily_mv" | grep -q 1

echo "[init_clickhouse] OK — sms.messages_mart, sms.messages_mart_active, sms.messages_daily ready."

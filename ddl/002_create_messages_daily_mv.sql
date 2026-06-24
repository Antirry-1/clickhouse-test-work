-- ============================================================================
-- messages_daily — daily pre-aggregate of messages_mart (one row = one
--   (day, country, delivery_status, currency) bucket).
-- Lets dashboard-style queries read thousands of aggregate rows instead of 1M.
-- ENGINE / PARTITION BY / ORDER BY rationale: see 1/OPTIMIZATION_PLAN.md №3.1.
-- Idempotent: safe to run repeatedly (IF NOT EXISTS everywhere).
--
-- DESIGN NOTE — backfill: a MATERIALIZED VIEW only captures inserts made AFTER
-- it is created. On a fresh `docker compose up` this DDL runs BEFORE the
-- generator inserts any rows, so the MV captures all 1M rows automatically —
-- no backfill needed. The one-time backfill (see scripts/init_clickhouse.sh,
-- guarded on messages_daily being empty) only matters for an ALREADY-populated
-- base table (the current live stack), where the MV missed earlier inserts.
-- ============================================================================

CREATE DATABASE IF NOT EXISTS sms;

CREATE TABLE IF NOT EXISTS sms.messages_daily
(
    -- ── Group key ──────────────────────────────────────────────────────────
    `day`             Date                                COMMENT 'toDate(sent_date) — daily bucket',
    `country`         LowCardinality(Nullable(String))    COMMENT 'Destination country (as in base)',
    `delivery_status` LowCardinality(Nullable(String))    COMMENT 'Final delivery status (as in base)',
    `currency`        LowCardinality(Nullable(String))    COMMENT 'EUR / RUB / USD (as in base)',

    -- ── Aggregate states (merge with -Merge to read final values) ──────────
    `msgs`            AggregateFunction(count)                 COMMENT 'Total messages in bucket',
    `delivrd`         AggregateFunction(countIf, UInt8)        COMMENT 'Messages with delivery_status = DELIVRD',
    `revenue`         AggregateFunction(sum, Float32)          COMMENT 'Sum of price in bucket'
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(day)
ORDER BY (day, country, delivery_status, currency)
-- `country` / `delivery_status` / `currency` are LowCardinality(Nullable(String))
-- and appear in ORDER BY, so nullable sort keys must be allowed (same as base).
SETTINGS allow_nullable_key = 1;

-- MATERIALIZED VIEW: every insert into messages_mart that survives the
-- soft-delete filter (deleted_at IS NULL) feeds incremental aggregate states
-- into messages_daily. Dashboard reads thousands of rows instead of 1M.
CREATE MATERIALIZED VIEW IF NOT EXISTS sms.messages_daily_mv
TO sms.messages_daily AS
SELECT
    toDate(sent_date)                          AS day,
    country,
    delivery_status,
    currency,
    countState()                                                    AS msgs,
    -- delivery_status is LowCardinality(Nullable(String)); the equality is
    -- Nullable(UInt8). coalesce(...,0) -> plain UInt8 so the state matches the
    -- AggregateFunction(countIf, UInt8) column, and a NULL status counts as 0,
    -- exactly like base countIf(delivery_status='DELIVRD') (NULL predicate = not counted).
    countIfState(toUInt8(coalesce(delivery_status = 'DELIVRD', 0)))  AS delivrd,
    sumState(price)                                                 AS revenue
FROM sms.messages_mart
WHERE deleted_at IS NULL          -- soft-delete handled at ingest
GROUP BY day, country, delivery_status, currency;

-- ============================================================================
-- messages_mart — SMS data mart (one row = one SMS).
-- ENGINE / PARTITION BY / ORDER BY / index rationale: see docs/DESIGN.md.
-- Idempotent: safe to run repeatedly (IF NOT EXISTS everywhere).
-- ============================================================================

CREATE DATABASE IF NOT EXISTS sms;

CREATE TABLE IF NOT EXISTS sms.messages_mart
(
    -- ── Identifiers ────────────────────────────────────────────────────────
    `customer_id`        UInt32                              COMMENT 'Client id (5–10 clients)',
    `application_uuid`   UUID                                COMMENT 'Client application (1–4 per client)',
    `message_id`         UUID                                COMMENT 'Platform/vendor SMS id',

    -- ── Time & addressing ─────────────────────────────────────────────────
    `sent_date`          DateTime64(6)                       COMMENT 'SMS send timestamp (event time)',
    `sender`             LowCardinality(Nullable(String))    COMMENT 'Alpha sender name (brand)',
    `receiver`           LowCardinality(Nullable(String))    COMMENT 'Recipient MSISDN (see docs/DESIGN.md note on type)',
    `country`            LowCardinality(Nullable(String))    COMMENT 'Destination country',
    `segment_count`      UInt32                              COMMENT 'Number of SMS segments (1–14)',

    -- ── Delivery status ────────────────────────────────────────────────────
    `delivery_status`    LowCardinality(Nullable(String))    COMMENT 'Final delivery status (DELIVRD, UNDELIV, ...)',
    `attempt_number`     UInt8                               COMMENT 'Delivery attempt number (1–3)',
    `delivery_time`      UInt16                              COMMENT 'Delivery time, milliseconds (100–3000)',

    -- ── Billing & direction ────────────────────────────────────────────────
    `price`              Float32                             COMMENT 'Price for the client, in `currency`',
    `currency`           LowCardinality(Nullable(String))    COMMENT 'EUR / RUB / USD',
    `receiver_operator`  LowCardinality(Nullable(String))    COMMENT 'Recipient mobile operator',
    `direction`          UInt8                               COMMENT '0 = inbound, 1 = outbound',

    -- ── Metadata (soft delete) ─────────────────────────────────────────────
    `created_at`         DateTime64(6)                       COMMENT 'Row created at',
    `updated_at`         DateTime64(6)                       COMMENT 'Row last updated at',
    `deleted_at`         Nullable(DateTime64(6))             COMMENT 'Soft-delete time in source (NULL = active)',

    -- ── Skip indexes ───────────────────────────────────────────────────────
    -- API clients look up by message_id / application_uuid (equality), and those
    -- columns are NOT an efficient prefix of ORDER BY -> bloom filters let
    -- ClickHouse skip granules that cannot contain the value. (See docs/DESIGN.md.)
    INDEX idx_message_id      message_id      TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_application_uuid application_uuid TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(sent_date)
PRIMARY KEY (customer_id, sent_date)
ORDER BY (customer_id, sent_date, country, delivery_status, message_id)
-- `country` and `delivery_status` are LowCardinality(Nullable(String)) per the spec and
-- appear in ORDER BY, so nullable sort keys must be allowed. NULLs sort first. See docs/DESIGN.md.
SETTINGS index_granularity = 8192, allow_nullable_key = 1;

-- Analytics view: every dashboard chart reads this, so soft-deleted rows are
-- excluded BY CONSTRUCTION (impossible to forget `WHERE deleted_at IS NULL`).
-- The DQ dashboard reads the base table instead, to inspect deleted/abnormal rows.
CREATE VIEW IF NOT EXISTS sms.messages_mart_active AS
SELECT *
FROM sms.messages_mart
WHERE deleted_at IS NULL;

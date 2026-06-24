-- ============================================================================
-- dashboard_queries.sql — SQL за дашбордом «SMS Operations» (DQL: SELECT-запросы).
--
-- Сами чарты заданы декларативно в scripts/build_dashboard.py (viz_type + метрика
-- + groupby + фильтр) через REST API Superset; Superset компилирует из этого SQL
-- против датасета sms.messages_mart_active (вью = messages_mart без мягко удалённых,
-- WHERE deleted_at IS NULL). Здесь собран эквивалентный, копипастящийся SQL —
-- по одному блоку на каждый из 13 чартов + 6 метрик датасета.
--
-- Запуск всех запросов разом:
--   docker compose exec -T clickhouse clickhouse-client --user default \
--     --password clickhouse --multiquery < dql/dashboard_queries.sql
-- Или вставляйте блоки по одному в Superset → SQL Lab (база «ClickHouse SMS»).
--
-- Глобальные фильтры дашборда добавляют к этим запросам предикаты:
--   • «Customer»  → AND customer_id IN (...)
--   • «Sent date» → AND sent_date BETWEEN ... AND ...
-- Ниже фильтры опущены (полный период, все клиенты) — как чарт без выбранных фильтров.
-- ============================================================================


-- ─────────────────────────── Метрики датасета (6) ───────────────────────────
-- Те же выражения, что заданы на датасете в build_dashboard.py (METRICS).
-- Все агрегаты считаются по sms.messages_mart_active.
--   count_messages     = count(message_id)
--   revenue            = sum(price)
--   delivery_rate_pct  = round(100 * avg(delivery_status = 'DELIVRD'), 2)
--   avg_delivery_time  = round(avg(delivery_time), 0)
--   failed_rate_pct    = round(100 * countIf(delivery_status != 'DELIVRD') / count(), 2)
--   cost_per_delivered = round(sum(price) / countIf(delivery_status = 'DELIVRD'), 4)
SELECT
    count(message_id)                                              AS count_messages,
    round(sum(price), 2)                                           AS revenue,
    round(100 * avg(delivery_status = 'DELIVRD'), 2)               AS delivery_rate_pct,
    round(avg(delivery_time), 0)                                   AS avg_delivery_time,
    round(100 * countIf(delivery_status != 'DELIVRD') / count(), 2) AS failed_rate_pct,
    round(sum(price) / countIf(delivery_status = 'DELIVRD'), 4)    AS cost_per_delivered
FROM sms.messages_mart_active;


-- ═══════════════════════════ Секция: KPIs / North Star ══════════════════════
-- Пять Big Number — одно число по всей активной витрине.

-- 1. Delivery rate (big number) — доля доставленных (DELIVRD), %  (≈ 79.98)
SELECT round(100 * avg(delivery_status = 'DELIVRD'), 2) AS delivery_rate_pct
FROM sms.messages_mart_active;

-- 2. Revenue (total) (big number) — sum(price), смешанные валюты  (≈ 99.6k)
SELECT round(sum(price), 2) AS revenue
FROM sms.messages_mart_active;

-- 3. Failed rate (big number) — доля недоставленных, %  (≈ 20.02 = 100 − delivery_rate)
SELECT round(100 * countIf(delivery_status != 'DELIVRD') / count(), 2) AS failed_rate_pct
FROM sms.messages_mart_active;

-- 4. Avg delivery time (big number) — среднее время доставки, мс  (≈ 963)
SELECT round(avg(delivery_time), 0) AS avg_delivery_time
FROM sms.messages_mart_active;

-- 5. Cost per delivered (big number) — цена за доставленную SMS = sum(price) / кол-во DELIVRD
SELECT round(sum(price) / countIf(delivery_status = 'DELIVRD'), 4) AS cost_per_delivered
FROM sms.messages_mart_active;


-- ═══════════════════════════════ Секция: Trends ═════════════════════════════

-- 6. SMS by day (time-series line) — число SMS по дням (грануляция P1D)
SELECT toStartOfDay(sent_date) AS day,
       count(message_id)        AS count_messages
FROM sms.messages_mart_active
GROUP BY day
ORDER BY day;

-- 7. Revenue by currency (bar) — выручка по валютам
SELECT currency,
       sum(price) AS revenue
FROM sms.messages_mart_active
GROUP BY currency
ORDER BY revenue DESC;


-- ═══════════════════════════ Секция: Health by segment ══════════════════════

-- 8. Delivery rate by operator (bar, top 10) — доля доставки по оператору
SELECT receiver_operator,
       round(100 * avg(delivery_status = 'DELIVRD'), 2) AS delivery_rate_pct
FROM sms.messages_mart_active
GROUP BY receiver_operator
ORDER BY delivery_rate_pct DESC
LIMIT 10;

-- 9. Avg delivery time by operator (bar, top 10) — среднее время доставки по оператору, мс
SELECT receiver_operator,
       round(avg(delivery_time), 0) AS avg_delivery_time
FROM sms.messages_mart_active
GROUP BY receiver_operator
ORDER BY avg_delivery_time DESC
LIMIT 10;

-- 10. Delivery rate by country (bar, top 10) — доля доставки по стране
SELECT country,
       round(100 * avg(delivery_status = 'DELIVRD'), 2) AS delivery_rate_pct
FROM sms.messages_mart_active
GROUP BY country
ORDER BY delivery_rate_pct DESC
LIMIT 10;

-- 11. Failed statuses (bar, top 10) — недоставленные статусы (фильтр чарта: NOT IN ('DELIVRD'))
SELECT delivery_status,
       count(message_id) AS count_messages
FROM sms.messages_mart_active
WHERE delivery_status NOT IN ('DELIVRD')
GROUP BY delivery_status
ORDER BY count_messages DESC
LIMIT 10;


-- ════════════════════════════════ Секция: Mix ══════════════════════════════

-- 12. Top countries (bar, top 10) — топ стран по числу SMS
SELECT country,
       count(message_id) AS count_messages
FROM sms.messages_mart_active
GROUP BY country
ORDER BY count_messages DESC
LIMIT 10;

-- 13. Top operators (pie, top 10) — топ операторов по числу SMS
SELECT receiver_operator,
       count(message_id) AS count_messages
FROM sms.messages_mart_active
GROUP BY receiver_operator
ORDER BY count_messages DESC
LIMIT 10;


-- ─────────────────── Быстрее: тот же агрегат на пре-витрине ──────────────────
-- M002 добавил пре-агрегат sms.messages_daily (AggregatingMergeTree). Дневные/
-- группировочные запросы можно гнать по нему — он сканирует тысячи строк вместо 1M.
-- Пример эквивалента «SMS by day» (значения совпадают с запросом №6):
SELECT day,
       countMerge(msgs) AS count_messages
FROM sms.messages_daily
GROUP BY day
ORDER BY day;
-- Пример «Delivery rate (total)» через пре-агрегат (совпадает с запросом №1):
-- messages_daily готовое число доставленных (delivrd) и всего (msgs), можно агрегировать по дням и считать долю.

--   Если написать SELECT countIf(...) по колонке delivrd, будет ошибка типов: в колонке
--   лежат не сырые строки, а агрегатные состояния. Их умеет читать только соответствующий
--   -Merge (и комбинатор должен совпадать: countIfState ↔ countIfMerge, sumState ↔
--   sumMerge, countState ↔ countMerge).
SELECT round(100 * countIfMerge(delivrd) / countMerge(msgs), 2) AS delivery_rate_pct
FROM sms.messages_daily;

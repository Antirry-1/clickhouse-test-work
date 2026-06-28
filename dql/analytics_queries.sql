-- ============================================================================
-- analytics_queries.sql — аналитический SQL на витрине sms.messages_mart_active.
--
-- Оконные функции (SUM() OVER, ROW_NUMBER/RANK/DENSE_RANK, NTILE, LAG/LEAD,
-- PARTITION BY ... ORDER BY) и табличные выражения (CTE / WITH).
--
-- Это справочные запросы для SQL Lab / clickhouse-client (как dashboard_queries.sql),
-- они НЕ участвуют в авто-сборке дашборда. Все читают представление _active
-- (мягко удалённые строки уже исключены). Синтаксис — ClickHouse 24.8;
-- отличия от стандартного SQL (PostgreSQL) помечены в комментариях.
-- ============================================================================


-- ─── A. Накопленная выручка по дням (running total) ──────────────────────────
-- Выручка накопленным итогом: оконная агрегатная функция SUM() OVER (ORDER BY ...)
-- с явной рамкой.
WITH daily AS (
    SELECT toDate(sent_date) AS day,
           sum(price)        AS revenue
    FROM sms.messages_mart_active
    GROUP BY day
)
SELECT day,
       round(revenue, 2)                                         AS revenue,
       round(sum(revenue) OVER (ORDER BY day
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2) AS revenue_cumulative
FROM daily
ORDER BY day;


-- ─── B. Выручка по месяцам и прирост месяц-к-месяцу (MoM) ─────────────────────
-- Смещение назад (LAG). В ClickHouse оконный аналог — lagInFrame()
-- (требует рамку; берём всю партицию, тогда lagInFrame(x) = значение предыдущей строки).
WITH monthly AS (
    SELECT toStartOfMonth(sent_date) AS month,
           sum(price)                AS revenue,
           count()                   AS sms
    FROM sms.messages_mart_active
    GROUP BY month
),
with_prev AS (
    SELECT month,
           revenue,
           sms,
           lagInFrame(revenue) OVER w AS prev_revenue
    FROM monthly
    WINDOW w AS (ORDER BY month ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
)
SELECT month,
       round(revenue, 2)      AS revenue,
       round(prev_revenue, 2) AS prev_revenue,
       round(100 * (revenue - prev_revenue) / nullIf(prev_revenue, 0), 2) AS mom_growth_pct
FROM with_prev
ORDER BY month;


-- ─── C. Топ-3 страны по выручке в каждом месяце (PARTITION BY) ────────────────
-- ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ... DESC) — сортируем строки и
-- разбиваем на группы; здесь группа = месяц.
-- (RANK/DENSE_RANK дали бы то же место при равной выручке — с пропуском / без.)
WITH per_country AS (
    SELECT toStartOfMonth(sent_date) AS month,
           country,
           sum(price) AS revenue,
           count()    AS sms
    FROM sms.messages_mart_active
    GROUP BY month, country
)
SELECT month, country, round(revenue, 2) AS revenue, sms, rnk
FROM (
    SELECT *,
           row_number() OVER (PARTITION BY month ORDER BY revenue DESC) AS rnk
    FROM per_country
)
WHERE rnk <= 3
ORDER BY month, rnk;


-- ─── D. Тиры клиентов по выручке (NTILE) ─────────────────────────────────────
-- NTILE делит результирующий набор на равные по размеру группы.
-- 8 клиентов → 4 квартиля выручки (по 2 клиента).
WITH per_customer AS (
    SELECT customer_id,
           sum(price) AS revenue,
           count()    AS sms
    FROM sms.messages_mart_active
    GROUP BY customer_id
)
SELECT customer_id,
       round(revenue, 2) AS revenue,
       sms,
       ntile(4) OVER (ORDER BY revenue DESC) AS revenue_quartile,
       dense_rank() OVER (ORDER BY revenue DESC) AS revenue_rank
FROM per_customer
ORDER BY revenue DESC;

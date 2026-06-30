-- ============================================================================
-- cohort_lifecycle.sql — когортный анализ и «жизнь» клиента на витрине.
--
--   · этапы жизни клиента: новый → активный → замолчал → ушёл;
--   · как делаем когортный анализ: берём группу (когорту) → считаем метрику →
--     смотрим, как она меняется по времени.
--
-- «Клиент» здесь — это получатель (`receiver`, номер абонента). Берём именно его,
-- а не `customer_id`: разных получателей много, а customer_id всего 8 — на восьми
-- значениях когорты не построить. Запросы читают представление _active.
-- Синтаксис — ClickHouse 24.8.
--
-- ВАЖНО: данные ненастоящие — получателей раздаём случайно и поровну в пределах
-- страны за год, поэтому реального удержания (retention) в них нет. Запросы
-- показывают, КАК это считать, а не реальную картину по продукту.
-- ============================================================================


-- ─── 1. Когортный анализ: retention получателей по месяцу первого SMS ─────────
-- Шаги: когорта = месяц первого контакта; метрика = число активных
-- получателей; разбивка = число месяцев с первого контакта (month_index).
-- cohort_size берём оконной first_value() (месяц 0), retention_pct = доля от него.
WITH first_seen AS (
    SELECT receiver,
           toStartOfMonth(min(sent_date)) AS cohort_month
    FROM sms.messages_mart_active
    WHERE receiver IS NOT NULL
    GROUP BY receiver
),
activity AS (
    SELECT f.cohort_month                                              AS cohort_month,
           dateDiff('month', f.cohort_month,
                    toStartOfMonth(m.sent_date))                       AS month_index,
           m.receiver                                                  AS receiver
    FROM sms.messages_mart_active AS m
    INNER JOIN first_seen AS f USING (receiver)
),
cohort AS (
    SELECT cohort_month,
           month_index,
           uniqExact(receiver) AS active_receivers
    FROM activity
    GROUP BY cohort_month, month_index
)
SELECT cohort_month,
       month_index,
       active_receivers,
       first_value(active_receivers) OVER (PARTITION BY cohort_month
                                           ORDER BY month_index)        AS cohort_size,
       round(100 * active_receivers /
             first_value(active_receivers) OVER (PARTITION BY cohort_month
                                                 ORDER BY month_index), 1) AS retention_pct
FROM cohort
ORDER BY cohort_month, month_index;


-- ─── 2. Жизненный цикл получателей: new / active / inactive / churned ─────────
-- Классификация по числу SMS и дням с последнего SMS относительно последней даты
-- витрины. Пороги (60 / 90 дней) — это ПАРАМЕТРЫ: на реальных данных их задаёт
-- гистограмма интервалов между событиями клиента.
WITH per_receiver AS (
    SELECT receiver,
           count()        AS sms,
           max(sent_date) AS last_seen,
           dateDiff('day', max(sent_date),
                    (SELECT max(sent_date) FROM sms.messages_mart_active)) AS days_since_last
    FROM sms.messages_mart_active
    WHERE receiver IS NOT NULL
    GROUP BY receiver
)
SELECT multiIf(days_since_last > 90, 'churned',     -- >90 дней без SMS
               days_since_last > 60, 'inactive',    -- 60–90 дней
               sms = 1,             'new',           -- единственный SMS, недавно
                                    'active')        AS lifecycle_stage,
       count()                                       AS receivers,
       round(100 * count() / sum(count()) OVER (), 1) AS share_pct
FROM per_receiver
GROUP BY lifecycle_stage
ORDER BY receivers DESC;

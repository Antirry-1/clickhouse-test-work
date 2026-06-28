# dql/ — SQL за дашбордом «SMS Operations»

Параллельно [`ddl/`](../ddl/) (определения таблиц, DDL) здесь лежит **DQL** — `SELECT`-запросы,
которые стоят за 14 графиками дашборда **SMS Operations** (13 основных + когортный heatmap).

Два уровня «запросов»:
- **Метрики** (SQL-агрегаты на датасете) — `METRICS` в `build_dashboard.py`.
- **Чарты** (метрика + группировка + фильтр) — `chart_specs()` в `build_dashboard.py`.

Эти же определения экспортированы в YAML: [`superset/import_bundle/charts/`](../superset/import_bundle/charts/)
(по файлу на чарт) и метрики в
[`superset/import_bundle/datasets/ClickHouse_SMS/messages_mart_active.yaml`](../superset/import_bundle/datasets/ClickHouse_SMS/messages_mart_active.yaml).

## Карта чартов

Глобальные фильтры дашборда (`Customer` → `customer_id IN (...)`, `Sent date` → диапазон
`sent_date`) добавляются к каждому запросу автоматически.

| # | Чарт | Секция | Тип | Метрика / срез | YAML |
|---|---|---|---|---|---|
| 1 | SMS by day | Trends | line | `count(message_id)` по дню | `charts/SMS_by_day_1.yaml` |
| 2 | Delivery rate | KPIs | big number | `delivery_rate_pct` | `charts/Delivery_rate_2.yaml` |
| 3 | Revenue (total) | KPIs | big number | `sum(price)` | `charts/Revenue_total_3.yaml` |
| 4 | Top countries | Mix | bar | `count` по `country`, top 10 | `charts/Top_countries_4.yaml` |
| 5 | Top operators | Mix | pie | `count` по `receiver_operator`, top 10 | `charts/Top_operators_5.yaml` |
| 6 | Revenue by currency | Trends | bar | `sum(price)` по `currency` | `charts/Revenue_by_currency_6.yaml` |
| 7 | Failed statuses | Health | bar | `count` по `delivery_status`, `≠ DELIVRD` | `charts/Failed_statuses_7.yaml` |
| 8 | Delivery rate by operator | Health | bar | `delivery_rate_pct` по `receiver_operator` | `charts/Delivery_rate_by_operator_8.yaml` |
| 9 | Avg delivery time by operator | Health | bar | `avg_delivery_time` по `receiver_operator` | `charts/Avg_delivery_time_by_operator_9.yaml` |
| 10 | Delivery rate by country | Health | bar | `delivery_rate_pct` по `country` | `charts/Delivery_rate_by_country_10.yaml` |
| 11 | Failed rate | KPIs | big number | `failed_rate_pct` | `charts/Failed_rate_11.yaml` |
| 12 | Avg delivery time | KPIs | big number | `avg_delivery_time` | `charts/Avg_delivery_time_12.yaml` |
| 13 | Cost per delivered | KPIs | big number | `cost_per_delivered` | `charts/Cost_per_delivered_13.yaml` |
| 14 | Cohort retention | Cohort analysis | heatmap_v2 | retention % по `cohort_month` × `month_index` (виртуальный датасет `cohort_retention`) | `charts/Cohort_retention_14.yaml` |

Сами запросы по чартам 1–13 — в [`dashboard_queries.sql`](dashboard_queries.sql), чарт 14
(когортный) — запрос №1 в [`cohort_lifecycle.sql`](cohort_lifecycle.sql). Логика метрик и
delivery rate / revenue также описана в [`docs/DESIGN.md`](../docs/DESIGN.md) №7.

## Аналитический SQL (оконные функции, CTE, когортный анализ)

Помимо запросов дашборда здесь лежат два справочных файла с более продвинутым SQL.
Это reference-запросы для SQL Lab / `make ch-shell` (в авто-сборке дашборда не участвуют);
синтаксис — ClickHouse 24.8, отличия от стандартного SQL (PostgreSQL) помечены в комментариях.
Все запросы проверены на `clickhouse-local`.

- [`analytics_queries.sql`](analytics_queries.sql) — **оконные функции + CTE**:
  - **A** — накопленная выручка по дням, `sum() OVER (ORDER BY ... ROWS …)`;
  - **B** — прирост выручки месяц-к-месяцу (MoM), смещение назад `lagInFrame()` (аналог `LAG`);
  - **C** — топ-3 страны по выручке в каждом месяце, `row_number() OVER (PARTITION BY month …)`;
  - **D** — тиры клиентов по выручке, `ntile(4)` + `dense_rank()`.
- [`cohort_lifecycle.sql`](cohort_lifecycle.sql) — **когортный анализ и жизненный цикл**:
  - **1** — retention-когорты получателей по месяцу первого SMS (когорта → метрика → разбивка),
    `cohort_size` через `first_value() OVER (PARTITION BY cohort …)`; **этот запрос питает
    дашбордовый heatmap «Cohort retention»** (чарт #14) через виртуальный датасет `cohort_retention`;
  - **2** — классификация получателей `new / active / inactive / churned` по дням с последнего SMS.

> Данные синтетические: получатели разыгрываются равномерно за год, поэтому retention/lifecycle
> здесь иллюстрируют **технику**, а не реальную динамику (так и помечено в самих файлах).

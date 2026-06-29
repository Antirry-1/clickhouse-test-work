# SMS Operations — ClickHouse + Superset (тестовое задание Data Engineer)

Воспроизводимый стек для витрины SMS-данных `messages_mart`: **ClickHouse** (хранилище) +
**Apache Superset** (дашборд **SMS Operations**), генератор данных на **1 000 000 строк / 1 год**
и запуск одной командой.

> Исходное задание — в [`docs/ASSIGNMENT.md`](docs/ASSIGNMENT.md). Обоснование архитектуры — в
> [`docs/DESIGN.md`](docs/DESIGN.md). Руководство по проверке простым языком для нетехнического
> ревьюера — в [`docs/HUMAN_CHECK.md`](docs/HUMAN_CHECK.md).

![Дашборд SMS Operations](screenshots/sms_operations_dashboard.png)

---

## Что вы получаете
- ClickHouse с `sms.messages_mart` (MergeTree) + представление `sms.messages_mart_active` (без мягко удалённых).
- Дневной предагрегат `sms.messages_daily` (AggregatingMergeTree) + materialized view `sms.messages_daily_mv`,
  чтобы запросы дашборда читали тысячи строк-агрегатов вместо 1M — DDL в
  [`ddl/002_create_messages_daily_mv.sql`](ddl/002_create_messages_daily_mv.sql).
- **1 000 000** реалистичных SMS-строк за период **2025-01-01 … 2026-01-01**.
- Superset с подключением `ClickHouse SMS` и дашбордом **SMS Operations**:
  14 графиков (вкл. когортный heatmap) + 2 глобальных фильтра (Customer, Sent date).
- Объекты Superset, экспортированные в **YAML** (`superset/import_bundle/`) для хранения в git.

## Требования
- Docker с плагином Compose (`docker compose version`). Проверено на Docker 29.x / Compose v5.
- ~3 ГБ свободного диска и RAM для образа Superset. Python на хосте не нужен (всё работает в контейнерах).

---

## Быстрый старт (одна команда)

```bash
cp .env.example .env          # опционально; `make up` сделает это за вас
docker compose up -d --build
```

Это собирает образы и автоматически прогоняет весь конвейер по порядку:

1. `clickhouse` стартует → healthy
2. `clickhouse-init` применяет DDL: `001_create_messages_mart.sql` (таблица + представление) и
   `002_create_messages_daily_mv.sql` (дневной предагрегат + materialized view)
3. `generator` вставляет 1 000 000 строк (идемпотентно — пропускает, если уже заполнено)
4. `superset-init` мигрирует Superset, создаёт admin, регистрирует подключение к ClickHouse
5. веб-сервер `superset` стартует → healthy
6. `superset-dashboard` строит дашборд **SMS Operations** через API Superset

Первый запуск занимает несколько минут (загрузка образов + вставка 1M строк + сборка дашборда). Следить за прогрессом:

```bash
make logs        # или: docker compose logs -f
```

### Или пошагово (тот же результат, через Makefile)
```bash
make up           # собрать + запустить всё (также выполняет шаги выше)
make init         # (пере)создать таблицу + представление    -> идемпотентно
make generate     # сгенерировать данные                     -> пропустит, если уже ~1M
make smoke-test   # проверить весь стек
```

`make help` выводит все цели.

---

## Windows

Да — это работает на Windows, потому что **каждый значимый скрипт выполняется *внутри* Linux-контейнеров**,
а не на вашем хосте.

**Требования:** Docker Desktop (бэкенд WSL2). Держите проект по пути, доступному Docker
(например, в вашей папке пользователя). Включённый `.gitattributes` сохраняет shell-скрипты с LF при checkout —
иначе окончания строк CRLF сломали бы `bash` внутри контейнеров.

### Вариант A — чистый docker compose (`.env` не нужен — значения по умолчанию встроены)
```bash
docker compose up -d --build
docker compose ps
docker compose logs -f
docker compose exec clickhouse clickhouse-client --user default --password clickhouse --query "SELECT count() FROM sms.messages_mart"
docker compose down -v
```
Затем откройте Superset на http://localhost:8088 (admin / admin).

### Вариант B — `make` + `scripts/smoke_test.sh`
Им нужны bash + curl на стороне хоста, поэтому запускайте из **WSL2** или **Git Bash**:
`make up && make smoke-test`.

> Скрипты бутстрапа контейнеров (`init_clickhouse.sh`, `init_superset.sh`) и Python-скрипты
> генератора/дашборда всегда выполняются внутри Linux-контейнеров, поэтому ведут себя одинаково на
> Windows, macOS и Linux.

---

## Доступ

| Сервис | URL | Учётные данные |
|---|---|---|
| Superset | http://localhost:8088 | `admin` / `admin` |
| ClickHouse HTTP | http://localhost:8123 | пользователь `default` / пароль `clickhouse` |
| ClickHouse native | `localhost:9000` | те же |

**Открыть дашборд:** Superset → **Dashboards** → **SMS Operations**, или напрямую
http://localhost:8088/superset/dashboard/sms_operations/

Порты и учётные данные настраиваются в `.env` (см. `.env.example`).

---

## Запуск дашборда Superset

Дашборд **SMS Operations** собирается **автоматически** при поднятии стека
(`docker compose up -d --build` или `make up`, шаг 6) через REST API Superset
(`scripts/build_dashboard.py`). Отдельно его запускать не нужно.

Открыть дашборд:
1. Убедитесь, что стек запущен: `make ps` — сервис `superset` должен быть healthy.
2. Залогиньтесь в Superset на http://localhost:8088 — `admin` / `admin`.
3. **Dashboards → SMS Operations**, или напрямую
   http://localhost:8088/superset/dashboard/sms_operations/

Пересобрать/обновить дашборд вручную (идемпотентно — графики ищутся по имени и переиспользуются):
```bash
make dashboard        # или: docker compose run --rm superset-dashboard
```

Если дашборда нет или Superset показывает 0 строк — см. раздел **Устранение неполадок**.

---

## Проверка, что всё заработало

```bash
make smoke-test
```
Проверяет: контейнеры запущены, ClickHouse `/ping`, ~1 000 000 строк, период покрывает год,
запрос delivery-rate работает, Superset healthy, дашборд **SMS Operations** на месте. Ожидаемый хвост:

```
  ✅ row count 1000000 >= 99% of 1000000
  ✅ delivery rate = 80.03%
  ✅ dashboard 'SMS Operations' exists
✅ SMOKE TEST PASSED
```

Удобные однострочники:
```bash
make ps              # статус контейнеров
make rows            # SELECT count() FROM sms.messages_mart   -> 1000000
make period          # min/max sent_date
make delivery-rate   # доля DELIVRD по активным строкам        -> ~80%
make ch-shell        # интерактивный клиент ClickHouse
```

**Опционально — Jira при падении smoke-теста:**
```bash
make smoke-test-with-jira   # если тест упал, заводит issue в Jira; иначе ничего не делает
```
Включается только если в `.env` заполнены `JIRA_*` (см. `.env.example`). Стек работает и без этого —
интеграция не входит в `docker-compose.yml` и не нужна для запуска. Реальный `JIRA_API_TOKEN`
держите только в локальном `.env` (он в `.gitignore`), не коммитьте.

---

## Генератор данных

`scripts/generate_data.py` (Python, `clickhouse-connect`), пакетные HTTP-вставки, с фиксированным seed.

```bash
# по умолчанию: 1 000 000 строк, 2025-01-01..2026-01-01, пакет 50000, seed 42
make generate                         # идемпотентно (пропустит, если уже заполнено)
make regenerate                       # TRUNCATE + регенерация с нуля

# свой запуск (напрямую):
docker compose run --rm -e GEN_TRUNCATE=1 generator \
  bash -lc "pip install -q -r requirements.txt && \
            python scripts/generate_data.py --rows 200000 --start-date 2025-01-01 --end-date 2025-07-01 --batch-size 25000 --seed 7"
```
Флаги: `--rows --start-date --end-date --batch-size --seed --truncate` (откат к переменным окружения `GEN_*`).

---

## Аналитика данных и форматы хранения

Два дополнительных скрипта (обоснование — в [`docs/DESIGN.md`](docs/DESIGN.md), раздел 12):

```bash
make analyze      # DQ + описательная статистика по messages_mart (нужен поднятый стек)
                  #   средний/медианный чек SMS, std (n-1 vs n), квартили, выбросы по IQR,
                  #   сегменты по странам/клиентам, доля NULL/not_defined/soft-delete,
                  #   + Parquet-отчёт в reports/
make benchmark    # сравнение CSV/Parquet/Feather по размеру и скорости (БД не нужна)
make test         # самопроверки генератора и аналитики (БД не нужна)
```

Чистые функции аналитики покрыты самопроверкой без БД (`scripts/test_analyze.py`):
математика выбросов по IQR закреплена на проверенном вручную ряде `[1..9, 100]`.

---

## Пересоздать всё с нуля
```bash
make clean        # остановить контейнеры + удалить тома (данные ClickHouse + метаданные Superset)
make up           # пересобрать + перезаполнить + пересобрать дашборд
# или одной командой:
make reset        # = clean + up   (алиас: make rebuild)
```

---

## Объекты Superset в YAML (трекаются в git)
Дашборд создаётся через REST API (`scripts/build_dashboard.py`) и экспортируется в YAML:

```
superset/import_bundle/
├── metadata.yaml
├── databases/ClickHouse_SMS.yaml          # пароль замаскирован (XXXXXXXXXX)
├── datasets/ClickHouse_SMS/
│   ├── messages_mart_active.yaml           # физический датасет (13 основных чартов)
│   └── cohort_retention.yaml               # виртуальный SQL-датасет (когортный heatmap)
├── charts/*.yaml                           # 14 графиков (вкл. Cohort retention)
└── dashboards/SMS_Operations_1.yaml        # вкл. native_filter_configuration
```

Переэкспорт после редактирования дашборда в UI:
```bash
make superset-export
```
Импорт закоммиченного YAML в свежий Superset (альтернатива сборщику через API):
```bash
docker compose exec superset superset import-dashboards -p /app/superset_project/import_bundle -u admin
```

> **SQL за графиками.** Чарты заданы декларативно в `scripts/build_dashboard.py` (Superset сам
> компилирует SQL). Эквивалентный, копипастящийся SQL по **всем 13 графикам + 6 метрикам** собран в
> [`dql/dashboard_queries.sql`](dql/dashboard_queries.sql); как посмотреть фактический SQL в UI
> (чарт → ••• → «View query») — в [`dql/README.md`](dql/README.md).

---

## Ручная сборка дашборда (запасной вариант, если сборщик через API когда-нибудь упадёт)

Подключение + датасет всё равно создаются через `make superset-init`. Соберите графики вручную:

1. Superset → **Settings → Database Connections** → проверьте **ClickHouse SMS**
   (`clickhousedb://default:clickhouse@clickhouse:8123/sms`).
2. **Datasets → + Dataset** → база *ClickHouse SMS*, схема `sms`, таблица `messages_mart_active`.
   Отметьте `sent_date` как temporal; добавьте метрики `count_messages = count(message_id)`,
   `revenue = sum(price)`, `delivery_rate_pct = round(100*avg(delivery_status='DELIVRD'),2)`.
3. Создайте графики (все читают `messages_mart_active`, поэтому мягко удалённые строки уже исключены):

   | График | Тип | Ключевой SQL / конфиг |
   |---|---|---|
   | SMS by day | Time-series line | метрика `count(message_id)`, ось X `sent_date`, гранулярность Day |
   | Delivery rate | Big Number | метрика `round(100*avg(delivery_status='DELIVRD'),2)` |
   | Revenue (total) | Big Number | метрика `sum(price)` |
   | Top countries | Bar | метрика `count(message_id)`, группировка по `country`, лимит строк 10 |
   | Top operators | Pie | метрика `count(message_id)`, группировка по `receiver_operator`, лимит 10 |
   | Revenue by currency | Bar | метрика `sum(price)`, группировка по `currency` |
   | Failed statuses | Bar | метрика `count(message_id)`, группировка по `delivery_status`, фильтр `delivery_status NOT IN ('DELIVRD')` |

   Эквивалентный «сырой» SQL (SQL Lab) для двух KPI:
   ```sql
   -- delivery rate (доля доставленных)
   SELECT round(100*countIf(delivery_status='DELIVRD')/count(),2) FROM sms.messages_mart_active;
   -- revenue by currency (выручка по валютам)
   SELECT currency, sum(price) FROM sms.messages_mart_active GROUP BY currency ORDER BY 2 DESC;
   ```
4. Новый **Dashboard** «SMS Operations» → добавьте 13 графиков → **+ Add/Edit Filters**:
   - **Customer** → Value-фильтр по `customer_id`.
   - **Sent date** → фильтр по диапазону времени.

---

## Структура проекта
```
.
├── README.md                     ← начните отсюда (как запускать)
├── docker-compose.yml            ← весь стек
├── Makefile                      ← make help (Linux/macOS/WSL/Git Bash)
├── .gitattributes                ← принудительный LF, чтобы скрипты контейнеров работали на Windows
├── .env.example                  ← конфиг (скопируйте в .env; опционально — значения по умолчанию встроены)
├── requirements.txt
├── data_spec.json                ← спецификация полей / справочные данные (вход)
│
├── ddl/                          ← DDL (определения таблиц)
│   ├── 001_create_messages_mart.sql       (таблица messages_mart + представление _active)
│   └── 002_create_messages_daily_mv.sql   (дневной предагрегат + materialized view)
├── dql/                          ← DQL (SELECT-запросы за 13 графиками дашборда)
│   ├── dashboard_queries.sql              (рабочий SQL по всем чартам + метрикам)
│   ├── analytics_queries.sql              (оконные функции + CTE)
│   ├── cohort_lifecycle.sql               (когорты + жизненный цикл)
│   └── README.md                          (как сделаны графики и где смотреть их SQL)
├── scripts/                      ← все запускаемые скрипты
│   ├── generate_data.py   init_clickhouse.sh   init_superset.sh
│   ├── register_db.py     build_dashboard.py   smoke_test.sh
│   ├── analyze_data.py    ← DQ + описательная статистика
│   ├── format_benchmark.py ← сравнение CSV/Parquet/Feather
│   ├── jira_create_issue.py ← опционально: заводит Jira issue при падении smoke-теста
│   └── test_generate.py   test_analyze.py      ← самопроверки без БД
├── superset/
│   ├── Dockerfile   superset_config.py
│   └── import_bundle/            ← объекты Superset в YAML (db, 2 датасета, 14 графиков, дашборд)
├── screenshots/
│   └── sms_operations_dashboard.png
│
└── docs/                         ← документация для людей
    ├── DESIGN.md                 (движок / ключи / обоснование дашборда)
    ├── HUMAN_CHECK.md            (проверка простым языком)
    ├── ASSIGNMENT.md             (исходное задание)
    └── diagrams/architecture.md  (Mermaid)
```

---

## Устранение неполадок

| Симптом | Решение |
|---|---|
| `make up` бесконечно тянет образы | Первый запуск качает образы ClickHouse + Superset (~2 ГБ). Подождите; `make logs`. |
| Тег образа Superset `4.1.1` не найден | Поменяйте `SUPERSET_IMAGE_TAG` в `.env`, затем `make rebuild`. (Аналогично для `CLICKHOUSE_IMAGE_TAG`.) |
| Superset показывает 0 строк | Убедитесь, что генератор завершился: `make rows` должен вывести `1000000`; если 0, запустите `make generate`. |
| Дашборд отсутствует | `make dashboard` (пересобирает через API). Или импортируйте YAML (см. выше). |
| Порт уже занят | Поменяйте `SUPERSET_PORT` / `CLICKHOUSE_HTTP_PORT` в `.env`, затем `make rebuild`. |
| `clickhouse-init` падает на ключе сортировки | Убедитесь, что DDL сохраняет `allow_nullable_key = 1` (nullable-колонки в `ORDER BY`). |
| (Windows) ошибка скрипта `init`: `bash\r: bad interpreter` / `set: invalid option` | В `.sh` пробрался CRLF. Репозиторий поставляет `.gitattributes` для защиты — переклонируйте или выполните `git add --renormalize . && git checkout -- scripts`. Не сохраняйте `.sh` как CRLF в редакторе. |
| (Windows) `make: command not found` | Используйте `docker compose ...` напрямую (см. раздел **Windows**, Вариант A), или `make` из WSL2 / Git Bash. |
| Хочу начать с чистого листа | `make clean && make up` (или `docker compose down -v && docker compose up -d --build`). |

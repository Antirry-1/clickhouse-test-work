# Архитектура

Если открываете диаграмму через VS Code, установите расширение:
`mermaidchart.vscode-mermaid-chart`

## Стек и поток данных

```mermaid
flowchart LR
    subgraph host["Ваша машина (docker compose)"]
        direction TB
        gen["генератор (python)\ngenerate_data.py\n1M строк, сид"]
        subgraph ch["ClickHouse :8123 / :9000"]
            mart[("messages_mart\nMergeTree\nPARTITION BY toYYYYMM(sent_date)")]
            view[["messages_mart_active\nVIEW: deleted_at IS NULL"]]
        end
        subgraph sup["Superset :8088"]
            db["подключение к БД\nClickHouse SMS\nclickhousedb://"]
            ds["датасет\nmessages_mart_active"]
            dash["дашборд\nSMS Operations\n7 чартов + 2 фильтра"]
        end
    end
    api["API-клиенты\n(поиск по клиенту / по сообщению)"]
    user["Проверяющий / аналитик\n(браузер)"]

    gen -- "батчевый INSERT (HTTP)" --> mart
    mart --> view
    view --> ds
    db --> ds --> dash
    dash --> user
    mart -. "точечный поиск через bloom-filter\nskip-индексы" .-> api
```

## Оркестрация одноразовых джоб (что запускает `docker compose up -d`)

```mermaid
flowchart TD
    A["clickhouse\n(healthy)"] --> B["clickhouse-init\nDDL: таблица + вью"]
    B --> C["generator\n1M строк (идемпотентно)"]
    A --> D["superset-init\ndb upgrade + admin + регистрация БД"]
    D --> E["superset (web)\nhealthy"]
    E --> F["superset-dashboard\nсборка SMS Operations через API"]
    C --> F
```

## Почему именно эти компоненты
- **ClickHouse** — колоночная OLAP-СУБД, идеальна для `count()`/`sum()` по витрине событий на 1М строк.
- **Вью `messages_mart_active`** — единая «точка отсечения», исключающая soft-delete строки, так что
  каждый чарт дашборда корректен по построению.
- **Superset** — подключается по `clickhousedb://` (драйвер clickhouse-connect); дашборд + чарты +
  2 глобальных native-фильтра; объекты выгружены в YAML для git.
- **Два потребителя** — ключ сортировки обслуживает Superset (срезы по клиенту + диапазону времени),
  а bloom-filter skip-индексы обслуживают точечный поиск API (message_id, application_uuid).

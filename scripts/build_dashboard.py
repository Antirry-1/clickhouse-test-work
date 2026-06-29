#!/usr/bin/env python3
"""Строит дашборд «SMS Operations» в Superset через его REST API.

Идемпотентно: датасет / метрики / графики / дашборд ищутся по имени и переиспользуются.
Надёжно и удобно для отладки (понятные ошибки) — это основной автоматический путь.
Отслеживаемый в git YAML в superset/import_bundle/ — это экспорт того, что строит скрипт.

Окружение: SUPERSET_URL, SUPERSET_ADMIN, SUPERSET_ADMIN_PASSWORD, CLICKHOUSE_DB.
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

BASE = os.environ.get("SUPERSET_URL", "http://localhost:8088").rstrip("/")
ADMIN = os.environ.get("SUPERSET_ADMIN", "admin")
ADMIN_PW = os.environ.get("SUPERSET_ADMIN_PASSWORD", "admin")
SCHEMA = os.environ.get("CLICKHOUSE_DB", "sms")
DB_NAME = "ClickHouse SMS"
TABLE = "messages_mart_active"
DASH_TITLE = "SMS Operations"

# KPI-полоса North Star (верхний ряд). Цвет порога осмысленный:
# для KPI_GOOD выше = лучше (зелёный), для KPI_BAD выше = хуже (красный), остальные нейтральны.
KPI_ALL = ["Delivery rate", "Revenue (total)", "Failed rate",
           "Avg delivery time", "Cost per delivered"]
KPI_GOOD = "Delivery rate"
KPI_BAD = "Failed rate"

# Когортный анализ — отдельный ВИРТУАЛЬНЫЙ (SQL) датасет, т.к. retention считается
# оконными функциями и его не выразить простой метрикой на физической витрине.
# SQL = запрос №1 из dql/cohort_lifecycle.sql.
COHORT_TABLE = "cohort_retention"
COHORT_CHART = "Cohort retention"
COHORT_SQL = """\
WITH first_seen AS (
    SELECT receiver, toStartOfMonth(min(sent_date)) AS cohort_month
    FROM sms.messages_mart_active WHERE receiver IS NOT NULL GROUP BY receiver),
activity AS (
    SELECT f.cohort_month AS cohort_month,
           dateDiff('month', f.cohort_month, toStartOfMonth(m.sent_date)) AS month_index,
           m.receiver AS receiver
    FROM sms.messages_mart_active AS m INNER JOIN first_seen AS f USING (receiver)),
cohort AS (
    SELECT cohort_month, month_index, uniqExact(receiver) AS active_receivers
    FROM activity GROUP BY cohort_month, month_index)
SELECT cohort_month, month_index, active_receivers,
       round(100 * active_receivers /
             first_value(active_receivers)
                 OVER (PARTITION BY cohort_month ORDER BY month_index), 1) AS retention_pct
FROM cohort"""

s = requests.Session()


def wait_for_health(timeout=180):
    for _ in range(timeout // 3):
        try:
            if s.get(f"{BASE}/health", timeout=5).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    raise SystemExit(f"Superset not healthy at {BASE}")


def login():
    r = s.post(f"{BASE}/api/v1/security/login",
               json={"username": ADMIN, "password": ADMIN_PW,
                     "provider": "db", "refresh": True}, timeout=30)
    r.raise_for_status()
    token = r.json()["access_token"]
    s.headers.update({"Authorization": f"Bearer {token}"})
    csrf = s.get(f"{BASE}/api/v1/security/csrf_token/", timeout=30).json()["result"]
    s.headers.update({"X-CSRFToken": csrf, "Referer": BASE})


def api(method, path, **kw):
    r = s.request(method, f"{BASE}{path}", timeout=60, **kw)
    if not r.ok:
        print(f"  ! {method} {path} -> {r.status_code}: {r.text[:500]}", file=sys.stderr)
    r.raise_for_status()
    return r.json() if r.text else {}


def find_one(path, col, value):
    q = json.dumps({"filters": [{"col": col, "opr": "eq", "value": value}]})
    res = api("GET", f"{path}?q={q}").get("result", [])
    return res[0] if res else None


# --- база данных ----------------------------------------------------------
def get_database_id():
    res = api("GET", "/api/v1/database/").get("result", [])
    for d in res:
        if d["database_name"] == DB_NAME:
            return d["id"]
    raise SystemExit(f"Database '{DB_NAME}' not found — run register_db first.")


# --- датасет + метрики ----------------------------------------------------
METRICS = [
    {"metric_name": "count_messages", "expression": "count(message_id)",
     "verbose_name": "SMS count", "metric_type": "count"},
    {"metric_name": "revenue", "expression": "sum(price)", "verbose_name": "Revenue"},
    {"metric_name": "delivery_rate_pct",
     "expression": "round(100 * avg(delivery_status = 'DELIVRD'), 2)",
     "verbose_name": "Delivery rate %"},
    {"metric_name": "avg_delivery_time",
     "expression": "round(avg(delivery_time), 0)",
     "verbose_name": "Avg delivery time (ms)"},
    {"metric_name": "failed_rate_pct",
     "expression": "round(100 * countIf(delivery_status != 'DELIVRD') / count(), 2)",
     "verbose_name": "Failed rate %"},
    {"metric_name": "cost_per_delivered",
     "expression": "round(sum(price) / countIf(delivery_status = 'DELIVRD'), 4)",
     "verbose_name": "Cost per delivered"},
]


def ensure_dataset(db_id):
    existing = find_one("/api/v1/dataset/", "table_name", TABLE)
    if existing:
        ds_id = existing["id"]
        print(f"  dataset exists id={ds_id}")
    else:
        ds_id = api("POST", "/api/v1/dataset/",
                    json={"database": db_id, "schema": SCHEMA, "table_name": TABLE})["id"]
        print(f"  dataset created id={ds_id}")

    full = api("GET", f"/api/v1/dataset/{ds_id}")["result"]
    # Существующие колонки/метрики ОБЯЗАНЫ сохранить свой id, иначе Superset считает их
    # новыми вставками и отклоняет с "already exist". Принудительно делаем sent_date temporal.
    cols = []
    for c in full["columns"]:
        cols.append({"id": c["id"], "column_name": c["column_name"], "type": c.get("type"),
                     "is_dttm": c["column_name"] == "sent_date" or bool(c.get("is_dttm")),
                     "groupby": c.get("groupby", True), "filterable": c.get("filterable", True)})
    have = {m["metric_name"] for m in full.get("metrics", [])}
    metrics = [{"id": m["id"], "metric_name": m["metric_name"], "expression": m["expression"],
                "verbose_name": m.get("verbose_name")} for m in full.get("metrics", [])]
    for m in METRICS:
        if m["metric_name"] not in have:
            metrics.append({k: m[k] for k in ("metric_name", "expression", "verbose_name")})
    api("PUT", f"/api/v1/dataset/{ds_id}?override_columns=false",
        json={"main_dttm_col": "sent_date", "metrics": metrics, "columns": cols})
    print(f"  dataset metrics set: {[m['metric_name'] for m in metrics]}")
    return ds_id


# --- когортный датасет + чарт (виртуальный SQL-датасет) -------------------
def ensure_cohort_dataset(db_id):
    existing = find_one("/api/v1/dataset/", "table_name", COHORT_TABLE)
    if existing:
        ds_id = existing["id"]
        api("PUT", f"/api/v1/dataset/{ds_id}", json={"sql": COHORT_SQL})
        print(f"  cohort dataset exists id={ds_id}")
    else:
        ds_id = api("POST", "/api/v1/dataset/",
                    json={"database": db_id, "schema": SCHEMA,
                          "table_name": COHORT_TABLE, "sql": COHORT_SQL})["id"]
        print(f"  cohort dataset created id={ds_id}")
    full = api("GET", f"/api/v1/dataset/{ds_id}")["result"]
    cols = [{"id": c["id"], "column_name": c["column_name"], "type": c.get("type"),
             "is_dttm": c["column_name"] == "cohort_month" or bool(c.get("is_dttm")),
             "groupby": c.get("groupby", True), "filterable": c.get("filterable", True)}
            for c in full["columns"]]
    have = {m["metric_name"] for m in full.get("metrics", [])}
    metrics = [{"id": m["id"], "metric_name": m["metric_name"], "expression": m["expression"],
                "verbose_name": m.get("verbose_name")} for m in full.get("metrics", [])]
    for mn, expr, vn in (("retention_pct_max", "max(retention_pct)", "Retention %"),
                         ("active_receivers_max", "max(active_receivers)", "Active receivers")):
        if mn not in have:
            metrics.append({"metric_name": mn, "expression": expr, "verbose_name": vn})
    api("PUT", f"/api/v1/dataset/{ds_id}?override_columns=false",
        json={"metrics": metrics, "columns": cols})
    return ds_id


def cohort_chart_params(ds_id):
    # heatmap: x = месяц с первого контакта, y = когорта (месяц первого SMS), цвет = retention %.
    # month_index=0 — базовый месяц когорты (всегда 100%); heatmap_v2 рисует числовой 0 как
    # "<NULL>"-колонку. Отсекаем его фильтром: retention для 1..11 уже посчитан в SQL
    # относительно месяца 0 (first_value по полному окну), поэтому математика не страдает.
    return {
        "datasource": f"{ds_id}__table", "viz_type": "heatmap_v2",
        "x_axis": "month_index", "groupby": "cohort_month",
        "metric": "retention_pct_max",
        "row_limit": 10000,
        "adhoc_filters": [{"expressionType": "SIMPLE", "subject": "month_index",
                           "operator": ">", "comparator": "0", "clause": "WHERE"}],
        "sort_x_axis": "value_asc", "sort_y_axis": "value_asc",
        "normalize_across": "y", "legend_type": "continuous",
        "linear_color_scheme": "dark_blue", "show_values": True,  # одно-тоновый синий — в тему
        "value_bounds": [0, 100], "y_axis_format": "SMART_NUMBER",
    }


def ensure_cohort_chart(ds_id):
    params = cohort_chart_params(ds_id)
    existing = find_one("/api/v1/chart/", "slice_name", COHORT_CHART)
    body = {"slice_name": COHORT_CHART, "viz_type": params["viz_type"],
            "datasource_id": ds_id, "datasource_type": "table", "params": json.dumps(params)}
    if existing:
        cid = existing["id"]
        api("PUT", f"/api/v1/chart/{cid}", json=body)
        print(f"  cohort chart updated (id={cid})")
    else:
        cid = api("POST", "/api/v1/chart/", json=body)["id"]
        print(f"  cohort chart created (id={cid})")
    return cid


# --- графики --------------------------------------------------------------
def chart_specs(ds_id):
    dsrc = f"{ds_id}__table"
    base = {"datasource": dsrc, "row_limit": 10000, "adhoc_filters": []}
    return [
        ("SMS by day", "echarts_timeseries_line", {
            **base, "viz_type": "echarts_timeseries_line", "x_axis": "sent_date",
            "time_grain_sqla": "P1D", "metrics": ["count_messages"], "groupby": [],
            "x_axis_sort_asc": True, "show_legend": True}),
        ("Delivery rate", "big_number_total", {
            **base, "viz_type": "big_number_total", "metric": "delivery_rate_pct",
            "granularity_sqla": "sent_date", "subheader": "% delivered (DELIVRD)"}),
        ("Revenue (total)", "big_number_total", {
            **base, "viz_type": "big_number_total", "metric": "revenue",
            "granularity_sqla": "sent_date", "subheader": "sum(price), mixed currency"}),
        ("Top countries", "dist_bar", {
            **base, "viz_type": "dist_bar", "metrics": ["count_messages"],
            "groupby": ["country"], "row_limit": 10, "order_desc": True,
            "granularity_sqla": "sent_date"}),
        ("Top operators", "pie", {
            **base, "viz_type": "pie", "metric": "count_messages",
            "groupby": ["receiver_operator"], "row_limit": 10,
            "granularity_sqla": "sent_date"}),
        ("Revenue by currency", "dist_bar", {
            **base, "viz_type": "dist_bar", "metrics": ["revenue"],
            "groupby": ["currency"], "order_desc": True, "granularity_sqla": "sent_date"}),
        ("Failed statuses", "dist_bar", {
            **base, "viz_type": "dist_bar", "metrics": ["count_messages"],
            "groupby": ["delivery_status"], "row_limit": 10, "order_desc": True,
            "granularity_sqla": "sent_date",
            "adhoc_filters": [{"expressionType": "SIMPLE", "subject": "delivery_status",
                               "operator": "NOT IN", "comparator": ["DELIVRD"],
                               "clause": "WHERE"}]}),
        ("Delivery rate by operator", "dist_bar", {
            **base, "viz_type": "dist_bar", "metrics": ["delivery_rate_pct"],
            "groupby": ["receiver_operator"], "row_limit": 10, "order_desc": True,
            "granularity_sqla": "sent_date"}),
        ("Avg delivery time by operator", "dist_bar", {
            **base, "viz_type": "dist_bar", "metrics": ["avg_delivery_time"],
            "groupby": ["receiver_operator"], "row_limit": 10, "order_desc": True,
            "granularity_sqla": "sent_date"}),
        ("Delivery rate by country", "dist_bar", {
            **base, "viz_type": "dist_bar", "metrics": ["delivery_rate_pct"],
            "groupby": ["country"], "row_limit": 10, "order_desc": True,
            "granularity_sqla": "sent_date"}),
        ("Failed rate", "big_number_total", {
            **base, "viz_type": "big_number_total", "metric": "failed_rate_pct",
            "granularity_sqla": "sent_date", "subheader": "% not delivered"}),
        ("Avg delivery time", "big_number_total", {
            **base, "viz_type": "big_number_total", "metric": "avg_delivery_time",
            "granularity_sqla": "sent_date", "subheader": "ms"}),
        ("Cost per delivered", "big_number_total", {
            **base, "viz_type": "big_number_total", "metric": "cost_per_delivered",
            "granularity_sqla": "sent_date", "subheader": "price / delivered"}),
    ]


def ensure_charts(ds_id):
    ids = {}
    for name, viz, params in chart_specs(ds_id):
        existing = find_one("/api/v1/chart/", "slice_name", name)
        body = {"slice_name": name, "viz_type": viz, "datasource_id": ds_id,
                "datasource_type": "table", "params": json.dumps(params)}
        if existing:
            cid = existing["id"]
            api("PUT", f"/api/v1/chart/{cid}", json=body)
            print(f"  chart updated: {name} (id={cid})")
        else:
            cid = api("POST", "/api/v1/chart/", json=body)["id"]
            print(f"  chart created: {name} (id={cid})")
        ids[name] = cid
    return ids


# --- дашборд --------------------------------------------------------------
def position_json(chart_ids, cohort_chart_id=None):
    layout = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "parents": ["ROOT_ID"], "children": []},
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID",
                      "meta": {"text": DASH_TITLE}},
    }
    # Дерево метрик: каждая группа — это секция (markdown-заголовок) над одним или
    # несколькими рядами графиков. Каждый график отвечает на вопрос роли (Dashboard Canvas №2/№4).
    sections = [
        ("KPIs / North Star",
         [["Delivery rate", "Revenue (total)", "Failed rate",
           "Avg delivery time", "Cost per delivered"]]),
        ("Trends",
         [["SMS by day", "Revenue by currency"]]),
        ("Health by segment",
         [["Delivery rate by operator", "Avg delivery time by operator",
           "Delivery rate by country", "Failed statuses"]]),
        ("Mix",
         [["Top countries", "Top operators"]]),
    ]
    ridx = 0
    for si, (title, rows) in enumerate(sections):
        hdr_id = f"HEADER-{si}"
        layout["GRID_ID"]["children"].append(hdr_id)
        layout[hdr_id] = {"type": "MARKDOWN", "id": hdr_id,
                          "meta": {"width": 12, "height": 6,
                                   "code": f"### {title}"},
                          "parents": ["ROOT_ID", "GRID_ID"], "children": []}
        for row in rows:
            row_id = f"ROW-{ridx}"
            ridx += 1
            layout["GRID_ID"]["children"].append(row_id)
            layout[row_id] = {"type": "ROW", "id": row_id,
                              "meta": {"background": "BACKGROUND_TRANSPARENT"},
                              "parents": ["ROOT_ID", "GRID_ID"], "children": []}
            width = max(3, 12 // len(row))
            for name in row:
                cid = chart_ids[name]
                comp = f"CHART-{cid}"
                layout[row_id]["children"].append(comp)
                layout[comp] = {"type": "CHART", "id": comp,
                                "meta": {"chartId": cid, "width": width, "height": 50,
                                         "sliceName": name},
                                "parents": ["ROOT_ID", "GRID_ID", row_id], "children": []}
    # Когортная секция (один широкий heatmap) — добавляется, только если чарт создан.
    if cohort_chart_id:
        hdr_id = "HEADER-cohort"
        layout["GRID_ID"]["children"].append(hdr_id)
        layout[hdr_id] = {"type": "MARKDOWN", "id": hdr_id,
                          "meta": {"width": 12, "height": 6,
                                   "code": "### Cohort analysis"},
                          "parents": ["ROOT_ID", "GRID_ID"], "children": []}
        row_id = f"ROW-{ridx}"
        layout["GRID_ID"]["children"].append(row_id)
        layout[row_id] = {"type": "ROW", "id": row_id,
                          "meta": {"background": "BACKGROUND_TRANSPARENT"},
                          "parents": ["ROOT_ID", "GRID_ID"], "children": []}
        comp = f"CHART-{cohort_chart_id}"
        layout[row_id]["children"].append(comp)
        layout[comp] = {"type": "CHART", "id": comp,
                        "meta": {"chartId": cohort_chart_id, "width": 12, "height": 60,
                                 "sliceName": COHORT_CHART},
                        "parents": ["ROOT_ID", "GRID_ID", row_id], "children": []}
    return layout


def native_filters(ds_id, chart_ids):
    scope = {"rootPath": ["ROOT_ID"], "excluded": []}
    in_scope = list(chart_ids.values())
    return [
        {"id": "NATIVE_FILTER-customer", "name": "Customer", "filterType": "filter_select",
         "type": "NATIVE_FILTER",
         "targets": [{"datasetId": ds_id, "column": {"name": "customer_id"}}],
         "controlValues": {"multiSelect": True, "enableEmptyFilter": False,
                           "searchAllOptions": False, "inverseSelection": False},
         "defaultDataMask": {"filterState": {}, "extraFormData": {}},
         "scope": scope, "chartsInScope": in_scope, "tabsInScope": []},
        {"id": "NATIVE_FILTER-sentdate", "name": "Sent date", "filterType": "filter_time",
         "type": "NATIVE_FILTER", "targets": [{}],
         "controlValues": {}, "defaultDataMask": {"filterState": {}, "extraFormData": {}},
         "scope": scope, "chartsInScope": in_scope, "tabsInScope": []},
    ]


# Палитра темы «Operations Command Center» (современный светлый аналитический UI).
THEME = {"brand": "#6366F1", "brand2": "#22D3EE", "good": "#10B981", "bad": "#F43F5E",
         "ink": "#0F172A", "muted": "#64748B", "border": "#E6EAF2"}


def dashboard_css(chart_ids):
    """CSS-тема «Operations Command Center»: нейтральный холст-градиент, приподнятые
    карточки со скруглением, мягкой многослойной тенью и hover-lift, KPI-полоса с
    семантичными акцентами порога, «eyebrow»-заголовки секций.
    Селекторы .dashboard-chart-id-N стабильны между сборками — в отличие от
    сгенерированных Emotion-классов вида .css-13133f7. Под Superset 4.1 тема задаётся
    только через CSS (дизайн-токены/тёмная тема появились в 6.0; на canvas-графиках
    цвет осей CSS-ом не перекрасить, поэтому светлая тема — единственный надёжный путь)."""
    t = THEME

    def sel(name):  # акцент кладём на видимую карточку (внутренний holder)
        return f".dashboard-chart-id-{chart_ids[name]} .dashboard-component-chart-holder"

    parts = [f"""\
/* ===== SMS Operations — тема «Operations Command Center» ===== */

/* Холст: мягкая радиальная вуаль, чтобы карточки «парили» */
.dashboard-content, .grid-content {{
  background: radial-gradient(1200px 620px at 50% -8%,
    #F8FAFF 0%, #EDF1F8 55%, #E8EDF5 100%) !important;
}}

/* Карточки графиков: скругление, тонкая рамка, многослойная тень, hover-lift */
.dashboard-component-chart-holder {{
  background:#fff; border:1px solid {t['border']}; border-radius:14px;
  box-shadow:0 1px 2px rgba(16,24,40,.04), 0 12px 28px -16px rgba(16,24,40,.20);
  transition:box-shadow .18s ease, transform .18s ease; overflow:hidden;
}}
.dashboard-component-chart-holder:hover {{
  box-shadow:0 6px 14px rgba(16,24,40,.08), 0 22px 48px -20px rgba(16,24,40,.28);
  transform:translateY(-2px);
}}
.dashboard-component-chart-holder .header-title {{ font-weight:600; color:{t['ink']}; }}

/* BigNumber — центрируем и усиливаем число */
.superset-legacy-chart-big-number {{ align-items:center; }}
.superset-legacy-chart-big-number .header-line {{
  justify-content:center; font-weight:700; color:{t['ink']}; }}
.superset-legacy-chart-big-number .subheader-line {{
  justify-content:center; color:{t['muted']}; font-weight:500; }}

/* «Eyebrow»-заголовки секций вместо дефолтного h3.
   Markdown-блоки секций несут тот же класс .dashboard-component-chart-holder, что и
   графики (карточный стиль выше задел и их), но лежат внутри обёртки .dashboard-markdown.
   Снимаем у них карточный фон/тень → заголовок «парит» на холсте, карточки графиков целы. */
.dashboard-markdown .dashboard-component-chart-holder {{
  background:transparent !important; border:0 !important;
  box-shadow:none !important; overflow:visible; }}
.dashboard-markdown .dashboard-component-chart-holder:hover {{
  transform:none; box-shadow:none !important; }}
.dashboard-markdown h3 {{
  margin:16px 0 2px; padding:0; border:0;
  font:600 11px/1.5 'Inter', system-ui, sans-serif;
  text-transform:uppercase; letter-spacing:.14em; color:{t['muted']};
  display:flex; align-items:center; gap:10px;
}}
.dashboard-markdown h3::before {{
  content:""; width:12px; height:12px; border-radius:4px;
  background:linear-gradient(135deg, {t['brand']}, {t['brand2']});
  box-shadow:0 2px 6px -1px {t['brand']}66;
}}"""]

    kpis = [n for n in KPI_ALL if n in chart_ids]
    if kpis:
        group = ", ".join(sel(n) for n in kpis)
        parts.append(f"""/* KPI-полоса North Star — приподнятые карточки с акцентом-порогом */
{group} {{ border-top:3px solid {t['brand']}; border-radius:16px;
  box-shadow:0 1px 2px rgba(16,24,40,.04), 0 14px 34px -18px rgba(16,24,40,.22); }}""")
        # Цвет порога каскадом поверх базового бренд-акцента.
        if KPI_GOOD in chart_ids:
            parts.append(f"{sel(KPI_GOOD)} {{ border-top-color:{t['good']}; }} /* выше = лучше */")
        if KPI_BAD in chart_ids:
            parts.append(f"{sel(KPI_BAD)} {{ border-top-color:{t['bad']}; }} /* выше = хуже */")
    return "\n".join(parts)


def ensure_dashboard(ds_id, chart_ids, cohort_chart_id=None):
    # Native-фильтры остаются на 13 основных чартах (физический датасет); когортный heatmap
    # независим от глобальных фильтров (у него своя гранулярность — когорта × месяц).
    # Перекраска одно-серийных столбцов/линии в бренд-индиго (под тему), чтобы уйти от
    # дефолтного бирюзового монохрома. Pie остаётся категориальным (его слайсы — операторы).
    brand_labels = ["SMS count", "Revenue", "Delivery rate %", "Avg delivery time (ms)",
                    "Failed rate %", "Cost per delivered", "count_messages", "revenue"]
    label_colors = dict.fromkeys(brand_labels, THEME["brand"])
    # Пай «Top operators»: когерентная холодная палитра (индиго→sky→cyan→teal→violet) по
    # топ-10 операторам — слайсы пая красятся по label_colors их подписей. Столбчатые чарты
    # «… by operator» не затрагиваются (там оператор — категория оси X, не серия).
    label_colors.update({
        "Beeline": "#4F46E5", "MTS": "#6366F1", "All networks": "#818CF8",
        "Tele2": "#0EA5E9", "Scartel": "#38BDF8", "MegaFon": "#06B6D4",
        "Altel": "#22D3EE", "K-Cell": "#14B8A6", "Azercell": "#2DD4BF",
        "MobiUZ (MTS)": "#8B5CF6"})
    meta = {"native_filter_configuration": native_filters(ds_id, chart_ids),
            "cross_filters_enabled": True, "label_colors": label_colors}
    body = {"dashboard_title": DASH_TITLE, "slug": "sms_operations", "published": True,
            "css": dashboard_css(chart_ids),
            "position_json": json.dumps(position_json(chart_ids, cohort_chart_id)),
            "json_metadata": json.dumps(meta)}
    existing = find_one("/api/v1/dashboard/", "slug", "sms_operations")
    if existing:
        did = existing["id"]
        api("PUT", f"/api/v1/dashboard/{did}", json=body)
        print(f"  dashboard updated id={did}")
    else:
        did = api("POST", "/api/v1/dashboard/", json=body)["id"]
        print(f"  dashboard created id={did}")
    # привязываем графики к дашборду (включая когортный, если создан)
    all_ids = list(chart_ids.values()) + ([cohort_chart_id] if cohort_chart_id else [])
    for cid in all_ids:
        api("PUT", f"/api/v1/chart/{cid}", json={"dashboards": [did]})
    return did


def main():
    print(f"[build_dashboard] target {BASE}")
    wait_for_health()
    login()
    db_id = get_database_id()
    print(f"[build_dashboard] database id={db_id}")
    ds_id = ensure_dataset(db_id)
    chart_ids = ensure_charts(ds_id)
    # Когортный heatmap — необязательное дополнение. Если что-то пойдёт не так
    # с виртуальным датасетом/виз-типом, 13 основных чартов не должны пострадать.
    cohort_chart_id = None
    try:
        cohort_ds_id = ensure_cohort_dataset(db_id)
        cohort_chart_id = ensure_cohort_chart(cohort_ds_id)
    except Exception as e:  # noqa: BLE001 — изолируем дополнительный чарт от обязательных
        print(f"  ! cohort chart skipped (core dashboard unaffected): {e}", file=sys.stderr)
    did = ensure_dashboard(ds_id, chart_ids, cohort_chart_id)
    print(f"[build_dashboard] DONE. Open {BASE}/superset/dashboard/sms_operations/  (id={did})")


if __name__ == "__main__":
    main()

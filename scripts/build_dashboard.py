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
def position_json(chart_ids):
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


def ensure_dashboard(ds_id, chart_ids):
    meta = {"native_filter_configuration": native_filters(ds_id, chart_ids),
            "cross_filters_enabled": True}
    body = {"dashboard_title": DASH_TITLE, "slug": "sms_operations", "published": True,
            "position_json": json.dumps(position_json(chart_ids)),
            "json_metadata": json.dumps(meta)}
    existing = find_one("/api/v1/dashboard/", "slug", "sms_operations")
    if existing:
        did = existing["id"]
        api("PUT", f"/api/v1/dashboard/{did}", json=body)
        print(f"  dashboard updated id={did}")
    else:
        did = api("POST", "/api/v1/dashboard/", json=body)["id"]
        print(f"  dashboard created id={did}")
    # привязываем графики к дашборду
    for cid in chart_ids.values():
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
    did = ensure_dashboard(ds_id, chart_ids)
    print(f"[build_dashboard] DONE. Open {BASE}/superset/dashboard/sms_operations/  (id={did})")


if __name__ == "__main__":
    main()

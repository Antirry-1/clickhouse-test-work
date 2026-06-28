#!/usr/bin/env python3
"""DQ + описательная статистика по витрине sms.messages_mart (для владельца данных).

Что считается:
  - Описательная статистика: среднее, медиана, размах, выборочное (n-1, поправка
    Бесселя) и генеральное (n) стандартное отклонение, квартили Q1/Q3, межквартильный
    размах IQR и поиск выбросов по правилу IQR (см. `iqr_outliers`);
  - Сегменты: groupby(...).agg() с несколькими метриками — срез цены SMS по стране и по клиенту;
  - результат сохраняется в Parquet (`reports/analysis_<дата>.parquet`) — компактно,
    с типами, удобно архивировать.

Статистика считается в pandas на DataFrame, загруженном из витрины через
clickhouse-connect, а дальше — методы pandas.

DQ-метрики (доля NULL / `not_defined` / soft-delete) считаются по БАЗОВОЙ таблице
(включая мягко удалённые строки) — это взгляд владельца данных; описательная статистика —
по активным строкам (`deleted_at IS NULL`), как и дашборд.

Запуск:  python scripts/analyze_data.py            (нужен поднятый ClickHouse)
         python scripts/analyze_data.py --out-dir reports   (плюс сохранить Parquet)
Самопроверка чистых функций без БД:  python scripts/test_analyze.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

# Столбцы, которых достаточно для статистики + DQ (грузим базовую таблицу, чтобы
# видеть и мягко удалённые строки — это нужно DQ-срезу владельца данных).
LOAD_COLUMNS = [
    "customer_id", "country", "currency", "receiver_operator", "sender",
    "delivery_status", "segment_count", "price", "delivery_time", "deleted_at",
]
NULLABLE_COLUMNS = ["sender", "country", "currency", "receiver_operator"]
SENTINEL = "not_defined"


# ─── Описательная статистика ─────────────────────────────────────────────────
def describe_series(s: pd.Series) -> dict:
    """Центральные тенденции + разброс для числового ряда.

    std_sample — деление на (n-1), поправка Бесселя, pandas .std() по умолчанию;
    std_pop    — деление на n, .std(ddof=0). Разница важна для выборки vs генеральной."""
    s = s.dropna().astype("float64")
    q1, med, q3 = s.quantile(0.25), s.quantile(0.5), s.quantile(0.75)
    lo, hi = s.min(), s.max()
    return {
        "count": int(s.count()),
        "mean": float(s.mean()),
        "median": float(med),
        "min": float(lo),
        "max": float(hi),
        "range": float(hi - lo),          # размах = max - min
        "std_sample": float(s.std()),     # ddof=1 (n-1, Бессель)
        "std_pop": float(s.std(ddof=0)),  # ddof=0 (n)
        "q1": float(q1),
        "q3": float(q3),
        "iqr": float(q3 - q1),            # межквартильный размах
    }


def iqr_outliers(s: pd.Series) -> dict:
    """Выбросы по правилу IQR: x < Q1 - 1.5·IQR или x > Q3 + 1.5·IQR.
    Устойчиво к выбросам и не зависит от формы распределения."""
    s = s.dropna().astype("float64")
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    mask = (s < low) | (s > high)
    return {
        "low_fence": float(low),
        "high_fence": float(high),
        "n_outliers": int(mask.sum()),
        "share": float(mask.mean()) if len(s) else 0.0,
    }


# ─── Работа с сегментами и группами ──────────────────────────────────────────
def segment_stats(df: pd.DataFrame, by: str, value: str = "price") -> pd.DataFrame:
    """Несколько метрик на группу: count / avg / median / total — средний чек по `by`."""
    g = df.groupby(by, dropna=False)[value]
    out = g.agg(count="count", avg="mean", median="median", total="sum")
    return out.sort_values("count", ascending=False)


# ─── DQ-метрики (взгляд владельца данных, по базовой таблице) ─────────────────
def dq_metrics(df: pd.DataFrame, nullable_cols=NULLABLE_COLUMNS) -> tuple[pd.DataFrame, float]:
    """Доля настоящих NULL и сентинела `not_defined` по nullable-колонкам + доля soft-delete.
    `df` — БАЗОВАЯ таблица (с мягко удалёнными), чтобы DQ их видел."""
    n = len(df)
    rows = []
    for c in nullable_cols:
        null_share = float(df[c].isna().mean())
        nd_share = float((df[c] == SENTINEL).mean())
        rows.append({"column": c, "null_share": null_share, "not_defined_share": nd_share})
    deleted_share = float(df["deleted_at"].notna().mean()) if n else 0.0
    return pd.DataFrame(rows), deleted_share


# ─── Вывод отчёта ────────────────────────────────────────────────────────────
def _skew_note(d: dict) -> str:
    """Интерпретация: среднее заметно выше медианы ⇒ правый хвост —
    есть несколько крупных значений, которые «тянут» среднее вверх."""
    if d["median"] and d["mean"] > d["median"] * 1.05:
        return "среднее > медианы ⇒ правый перекос (крупные значения тянут среднее вверх)"
    if d["median"] and d["mean"] < d["median"] * 0.95:
        return "среднее < медианы ⇒ левый перекос"
    return "среднее ≈ медиане ⇒ распределение симметрично"


def print_report(df_all: pd.DataFrame) -> dict:
    """Печатает отчёт по образцу generator.report(); возвращает результаты для сохранения."""
    df_active = df_all[df_all["deleted_at"].isna()]
    print("[analyze] ── описательная статистика (активные строки) ──────────")
    print(f"[analyze]   активных строк: {len(df_active):,} из {len(df_all):,}")

    results: dict = {}
    for col, label in (("price", "цена SMS (средний чек)"), ("delivery_time", "время доставки, мс")):
        d = describe_series(df_active[col])
        out = iqr_outliers(df_active[col])
        results[col] = {**d, **{f"outlier_{k}": v for k, v in out.items()}}
        print(f"[analyze]   · {label}:")
        print(f"[analyze]       mean={d['mean']:.4g}  median={d['median']:.4g}  "
              f"min={d['min']:.4g}  max={d['max']:.4g}  range={d['range']:.4g}")
        print(f"[analyze]       std_sample(n-1)={d['std_sample']:.6g}  "
              f"std_pop(n)={d['std_pop']:.6g}  (сходятся на больших n)")
        print(f"[analyze]       Q1={d['q1']:.4g}  Q3={d['q3']:.4g}  IQR={d['iqr']:.4g}  "
              f"→ {_skew_note(d)}")
        print(f"[analyze]       выбросы IQR: {out['n_outliers']:,} "
              f"({100*out['share']:.2f}%) вне [{out['low_fence']:.4g}, {out['high_fence']:.4g}]")

    print("[analyze] ── сегменты: средняя/медианная цена SMS по странам (top-10) ──")
    by_country = segment_stats(df_active, "country", "price")
    results["price_by_country"] = by_country
    for c, r in by_country.head(10).iterrows():
        print(f"[analyze]     {str(c):<22} count={int(r['count']):>9,}  "
              f"avg={r['avg']:.4f}  median={r['median']:.4f}  total={r['total']:.1f}")

    print("[analyze] ── сегменты: выручка/средний чек по клиентам ──")
    by_customer = segment_stats(df_active, "customer_id", "price")
    results["price_by_customer"] = by_customer
    for c, r in by_customer.iterrows():
        print(f"[analyze]     customer {int(c)}  count={int(r['count']):>9,}  "
              f"avg={r['avg']:.4f}  total={r['total']:.1f}")

    print("[analyze] ── DQ (по базовой таблице, владелец данных) ──")
    dq, deleted_share = dq_metrics(df_all)
    results["dq"] = dq
    for _, r in dq.iterrows():
        print(f"[analyze]     {r['column']:<20} NULL={100*r['null_share']:.2f}%  "
              f"not_defined={100*r['not_defined_share']:.2f}%")
    print(f"[analyze]     soft-delete: {100*deleted_share:.2f}% строк")
    return results


def save_parquet(results: dict, out_dir: str) -> None:
    """Сохраняем срезы отчёта в Parquet с датой в имени — компактно, с типами."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    written = []
    # Сводка по числовым полям — одной таблицей.
    summary = pd.DataFrame({k: results[k] for k in ("price", "delivery_time")}).T
    for name, frame in (("summary", summary),
                        ("price_by_country", results["price_by_country"]),
                        ("price_by_customer", results["price_by_customer"]),
                        ("dq", results["dq"])):
        path = out / f"{name}_{today}.parquet"
        frame.to_parquet(path)
        written.append(path.name)
    print(f"[analyze] Parquet-отчёт сохранён в {out}/: {', '.join(written)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="DQ + descriptive statistics over messages_mart.")
    ap.add_argument("--out-dir", default=os.environ.get("ANALYZE_OUT"),
                    help="каталог для Parquet-отчёта; без флага отчёт только печатается")
    ap.add_argument("--limit", type=int, default=None,
                    help="ограничить число строк (для быстрой проверки)")
    args = ap.parse_args()

    import clickhouse_connect  # импорт здесь, чтобы чистые функции тестировались без драйвера

    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_HTTP_PORT", 8123)),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "clickhouse"),
        database=os.environ.get("CLICKHOUSE_DB", "sms"),
    )
    cols = ", ".join(LOAD_COLUMNS)
    sql = f"SELECT {cols} FROM sms.messages_mart"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    print(f"[analyze] загрузка витрины в pandas: {sql}")
    df_all = client.query_df(sql)
    if df_all.empty:
        print("[analyze] таблица пуста — сначала сгенерируйте данные (make generate)", file=sys.stderr)
        return 1

    results = print_report(df_all)
    if args.out_dir:
        save_parquet(results, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

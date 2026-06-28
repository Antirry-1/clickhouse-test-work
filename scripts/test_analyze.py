#!/usr/bin/env python3
"""Самопроверка чистых функций analyze_data (без тестового фреймворка и без ClickHouse).

Математика выбросов по IQR закреплена на проверенном вручную ряду
[1..9, 100]: Q1=3.25, Q3=7.75, IQR=4.5, забор выбросов [-3.5, 14.5] → один выброс (100);
плюс прогон сегментных/DQ-функций на пакете генератора.

Запуск:  python scripts/test_analyze.py     (код выхода 0 = все проверки прошли)
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

import analyze_data as a
import generate_data as g


def approx(x, y, eps=1e-9):
    return abs(x - y) < eps


def build_batch(seed=42, n=50000):
    start_dt = datetime.strptime("2025-01-01", "%Y-%m-%d")
    end_dt = datetime.strptime("2026-01-01", "%Y-%m-%d")
    span_us = int((end_dt - start_dt).total_seconds() * 1_000_000)
    rng = g.np.random.default_rng(seed)
    vals = g.load_spec_values()
    customers, cust_p = g.build_customers(rng)
    recv_pools = g.build_receiver_pools(list(g.COUNTRY_WEIGHTS.keys()), rng)
    return g.gen_batch(n, rng, start_dt, span_us, customers, cust_p, recv_pools, vals)


def main() -> int:
    failures = []

    def check(cond, msg):
        print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failures.append(msg)

    print("[test] describe_series on hand-checked [1..9, 100]")
    s = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 100], dtype="float64")
    d = a.describe_series(s)
    check(d["count"] == 10, "count = 10")
    check(approx(d["mean"], 14.5), f"mean = 14.5 (got {d['mean']})")
    check(approx(d["median"], 5.5), f"median = 5.5 (got {d['median']})")
    check(approx(d["min"], 1) and approx(d["max"], 100), "min=1, max=100")
    check(approx(d["range"], 99), f"range = 99 (got {d['range']})")
    check(approx(d["q1"], 3.25), f"Q1 = 3.25 (got {d['q1']})")
    check(approx(d["q3"], 7.75), f"Q3 = 7.75 (got {d['q3']})")
    check(approx(d["iqr"], 4.5), f"IQR = 4.5 (got {d['iqr']})")
    check(d["std_sample"] > d["std_pop"] > 0,
          f"std_sample(n-1) > std_pop(n) > 0 ({d['std_sample']:.4f} > {d['std_pop']:.4f})")

    print("[test] iqr_outliers on the same series")
    o = a.iqr_outliers(s)
    check(approx(o["low_fence"], -3.5), f"low fence = -3.5 (got {o['low_fence']})")
    check(approx(o["high_fence"], 14.5), f"high fence = 14.5 (got {o['high_fence']})")
    check(o["n_outliers"] == 1, f"exactly 1 outlier (100) (got {o['n_outliers']})")
    check(approx(o["share"], 0.1), f"outlier share = 0.1 (got {o['share']})")

    print("[test] skew interpretation")
    check("правый" in a._skew_note(d), "mean>median flagged as right-skew")

    print("[test] segment + DQ on a generator batch")
    df = build_batch()
    by_country = a.segment_stats(df, "country", "price")
    check(int(by_country["count"].sum()) == len(df), "country segment counts sum to N rows")
    by_customer = a.segment_stats(df, "customer_id", "price")
    check(len(by_customer) == 8, f"8 customer segments (got {len(by_customer)})")

    dq, deleted_share = a.dq_metrics(df)
    check(list(dq["column"]) == a.NULLABLE_COLUMNS, "DQ covers all nullable columns")
    check((dq["null_share"] >= 0).all() and (dq["null_share"] <= 1).all(), "null shares in [0,1]")
    check(dq.loc[dq["column"] == "sender", "null_share"].iloc[0] > 0, "sender has real NULLs")
    check(0.005 <= deleted_share <= 0.03, f"soft-delete share ~1.5% (got {deleted_share:.4f})")

    pstats = a.describe_series(df["price"])
    check(0.0 <= pstats["median"] <= 1.0 and 0.0 <= pstats["mean"] <= 1.0,
          "price mean/median within spec range [0,1]")

    print()
    if failures:
        print(f"[test] FAILED ({len(failures)} check(s))")
        return 1
    print("[test] all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

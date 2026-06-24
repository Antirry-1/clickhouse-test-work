#!/usr/bin/env python3
"""Самопроверка numpy-векторизованного генератора (без тестового фреймворка).

Проверяет жёсткие контракты Фазы 8 на DataFrame-пакете в памяти, поэтому работает
без подключения к ClickHouse:
  - тот же seed  -> побайтово идентичный пакет
  - другой seed  -> другой пакет
  - доля DELIVRD в [0.75, 0.85]
  - множество валют — подмножество {RUB, USD, EUR, None}
  - цены в [0, 1]; sent_date внутри периода; присутствуют настоящие NULL

Запуск:  python scripts/test_generate.py     (код выхода 0 = все проверки прошли)
"""
from __future__ import annotations

from datetime import datetime

import generate_data as g


def build(seed, n=50000, start="2025-01-01", end="2026-01-01"):
    """Строит статические структуры + один пакет для заданного seed (без БД)."""
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    span_us = int((end_dt - start_dt).total_seconds() * 1_000_000)
    rng = g.np.random.default_rng(seed)
    vals = g.load_spec_values()
    customers, cust_p = g.build_customers(rng)
    recv_pools = g.build_receiver_pools(list(g.COUNTRY_WEIGHTS.keys()), rng)
    df = g.gen_batch(n, rng, start_dt, span_us, customers, cust_p, recv_pools, vals)
    return df, start_dt, end_dt


def main() -> int:
    failures = []

    def check(cond, msg):
        print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failures.append(msg)

    print("[test] determinism")
    df_a, start_dt, end_dt = build(42)
    df_b, _, _ = build(42)
    df_c, _, _ = build(43)
    check(df_a.equals(df_b), "same seed -> identical batch")
    check(not df_a.equals(df_c), "different seed -> different batch")

    print("[test] column order / schema")
    check(list(df_a.columns) == g.COLUMNS, "columns match COLUMNS order")

    print("[test] distributions")
    n = len(df_a)
    delivrd_share = (df_a["delivery_status"] == "DELIVRD").mean()
    check(0.75 <= delivrd_share <= 0.85,
          f"DELIVRD share {delivrd_share:.4f} in [0.75, 0.85]")

    cur_set = set(df_a["currency"].dropna().unique())
    check(cur_set <= {"RUB", "USD", "EUR"},
          f"currency set {sorted(cur_set)} subset of RUB/USD/EUR(+None)")
    check(df_a["currency"].isna().any(), "currency has real NULLs")

    rub_share = (df_a["currency"] == "RUB").mean()
    check(rub_share > (df_a["currency"] == "USD").mean()
          and rub_share > (df_a["currency"] == "EUR").mean(),
          f"RUB is the most common currency ({rub_share:.4f})")

    print("[test] value ranges")
    check(df_a["price"].between(0.0, 1.0).all(), "price in [0, 1]")
    check(df_a["segment_count"].between(1, 14).all(), "segment_count in [1, 14]")
    check(df_a["attempt_number"].between(1, 3).all(), "attempt_number in [1, 3]")
    check(df_a["delivery_time"].between(100, 3000).all(), "delivery_time in [100, 3000]")
    check(df_a["direction"].isin([0, 1]).all(), "direction in {0, 1}")
    check((df_a["sent_date"] >= start_dt).all() and (df_a["sent_date"] < end_dt).all(),
          "sent_date inside [start, end)")

    print("[test] soft-delete + nullable columns")
    deleted_share = df_a["deleted_at"].notna().mean()
    check(0.005 <= deleted_share <= 0.03,
          f"soft-delete share {deleted_share:.4f} ~1.5%")
    check(df_a["sender"].isna().any(), "sender has real NULLs")
    check(df_a["country"].isna().any(), "country has real NULLs")
    check(df_a["receiver_operator"].isna().any(), "receiver_operator has real NULLs")

    print("[test] customers")
    check(df_a["customer_id"].nunique() == 8, "8 distinct customers")

    print()
    if failures:
        print(f"[test] FAILED ({len(failures)} check(s))")
        return 1
    print(f"[test] all checks passed (n={n})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

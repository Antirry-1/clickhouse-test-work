#!/usr/bin/env python3
"""Сравнение форматов хранения CSV / Parquet / Feather на данных витрины.

Для одной и той же таблицы замеряем размер файла и время записи/чтения в трёх форматах.

Ожидаемо: Parquet/Feather в разы быстрее CSV и в 3–4 раза компактнее;
CSV — для обмена и Excel, Parquet — для хранилищ и аналитики (форматов вроде ClickHouse),
Feather — для быстрых промежуточных файлов в пайплайне.

Данные берутся из генератора витрины (gen_batch) — реальная схема messages_mart,
поэтому БД для замера не нужна. Файлы пишутся во временный каталог и удаляются.

Запуск:  python scripts/format_benchmark.py --rows 200000
"""
from __future__ import annotations

import argparse
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import generate_data as g


def make_dataframe(rows: int, seed: int = 42):
    """Реалистичный пакет витрины через генератор (без БД) — та же схема, что в ClickHouse."""
    start_dt = datetime.strptime("2025-01-01", "%Y-%m-%d")
    end_dt = datetime.strptime("2026-01-01", "%Y-%m-%d")
    span_us = int((end_dt - start_dt).total_seconds() * 1_000_000)
    rng = g.np.random.default_rng(seed)
    vals = g.load_spec_values()
    customers, cust_p = g.build_customers(rng)
    recv_pools = g.build_receiver_pools(list(g.COUNTRY_WEIGHTS.keys()), rng)
    return g.gen_batch(rows, rng, start_dt, span_us, customers, cust_p, recv_pools, vals)


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def benchmark(df, tmp: Path) -> dict:
    """Замер размера и времени записи/чтения для CSV / Parquet / Feather."""
    import pandas as pd  # noqa: F401  (pd.read_* используются ниже)

    paths = {"CSV": tmp / "mart.csv", "Parquet": tmp / "mart.parquet", "Feather": tmp / "mart.feather"}
    writers = {
        "CSV": lambda: df.to_csv(paths["CSV"], index=False),
        "Parquet": lambda: df.to_parquet(paths["Parquet"], index=False),
        "Feather": lambda: df.to_feather(paths["Feather"]),
    }
    readers = {
        "CSV": lambda: pd.read_csv(paths["CSV"]),
        "Parquet": lambda: pd.read_parquet(paths["Parquet"]),
        "Feather": lambda: pd.read_feather(paths["Feather"]),
    }
    res = {}
    for fmt in ("CSV", "Parquet", "Feather"):
        w = _timed(writers[fmt])
        r = _timed(readers[fmt])
        size_mb = os.path.getsize(paths[fmt]) / 1024 / 1024
        res[fmt] = {"size_mb": size_mb, "write_s": w, "read_s": r}
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="CSV/Parquet/Feather size & speed benchmark on mart data.")
    ap.add_argument("--rows", type=int, default=int(os.environ.get("BENCH_ROWS", 200_000)))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    print(f"[benchmark] строим {args.rows:,} строк витрины (схема messages_mart)…")
    df = make_dataframe(args.rows, args.seed)

    with tempfile.TemporaryDirectory() as d:
        res = benchmark(df, Path(d))

    print(f"[benchmark] таблица: {len(df):,} строк × {len(df.columns)} столбцов")
    print(f"[benchmark] {'формат':<9} {'размер,МБ':>10} {'запись,с':>10} {'чтение,с':>10}")
    for fmt in ("CSV", "Parquet", "Feather"):
        m = res[fmt]
        print(f"[benchmark] {fmt:<9} {m['size_mb']:>10.1f} {m['write_s']:>10.2f} {m['read_s']:>10.2f}")

    csv, pq = res["CSV"], res["Parquet"]
    if pq["read_s"] > 0 and pq["size_mb"] > 0:
        print(f"[benchmark] Parquet: чтение в {csv['read_s']/pq['read_s']:.1f}× быстрее CSV, "
              f"файл в {csv['size_mb']/pq['size_mb']:.1f}× меньше.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

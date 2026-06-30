#!/usr/bin/env python3
"""Убирает мусорный ряд-дубль из выгруженного дашборда.

`superset export-dashboards` (4.1.x) при каждом экспорте дописывает в position
лишний ряд `ROW-N-<random>`, дублирующий все чарты дашборда. Живой дашборд
чист (через REST API position_json без этого ряда) — артефакт появляется только
в выгрузке, причём хэш ряда меняется от запуска к запуску. Этот шаг вызывается из
`make superset-export` сразу после распаковки бандла, чтобы git-артефакт
соответствовал реальному дашборду.

Запуск:  python3 scripts/clean_export.py [bundle_dir]
"""
from __future__ import annotations

import glob
import os
import sys

import yaml


def clean_position(pos):
    """Удаляет ряды ROW-N-* и их дочерние чарты из дерева position (мутирует pos).
    Возвращает (число_рядов, число_дубль-чартов)."""
    junk_rows = [k for k in pos if k.startswith("ROW-N-")]
    removed_charts = 0
    for rk in junk_rows:
        for child in pos[rk].get("children", []) or []:
            if child in pos:
                del pos[child]
                removed_charts += 1
        del pos[rk]
        grid = pos.get("GRID_ID", {}).get("children", [])
        if rk in grid:
            grid.remove(rk)
    return len(junk_rows), removed_charts


def clean_dashboard(path):
    d = yaml.safe_load(open(path, encoding="utf-8"))
    pos = (d or {}).get("position")
    if not pos:
        return 0
    n_rows, n_charts = clean_position(pos)
    if not n_rows:
        return 0
    # Инвариант: после очистки все ссылки в дереве разрешаются.
    for k, v in pos.items():
        if isinstance(v, dict):
            for ch in v.get("children", []) or []:
                assert ch in pos, f"dangling child {ch} in {k}"
    yaml.safe_dump(d, open(path, "w", encoding="utf-8"),
                   allow_unicode=True, sort_keys=False, width=4096)
    print(f"  cleaned {os.path.basename(path)}: -{n_rows} junk row(s), -{n_charts} dup chart(s)")
    return n_rows


def main():
    bundle = sys.argv[1] if len(sys.argv) > 1 else "superset/import_bundle"
    total = sum(clean_dashboard(p) for p in glob.glob(f"{bundle}/dashboards/*.yaml"))
    print(f"[clean_export] removed {total} export-injected row(s)")


if __name__ == "__main__":
    main()

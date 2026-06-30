#!/usr/bin/env python3
"""Самопроверка clean_export.clean_position (без Superset и файлов).

Запуск:  python scripts/test_clean_export.py     (код выхода 0 = все проверки прошли)
"""
from __future__ import annotations

import clean_export as c


def test_removes_junk_keeps_legit():
    pos = {
        "GRID_ID": {"children": ["ROW-0", "ROW-N-XYZ123"]},
        "ROW-0": {"children": ["CHART-2"]},
        "CHART-2": {"meta": {"chartId": 2}},                 # настоящий чарт
        "ROW-N-XYZ123": {"children": ["CHART-DUP1", "CHART-DUP2"]},
        "CHART-DUP1": {"meta": {"chartId": 2}},               # дубль
        "CHART-DUP2": {"meta": {"chartId": 3}},               # дубль
    }
    assert c.clean_position(pos) == (1, 2)
    assert "ROW-N-XYZ123" not in pos
    assert "CHART-DUP1" not in pos and "CHART-DUP2" not in pos
    assert pos["GRID_ID"]["children"] == ["ROW-0"]
    assert "CHART-2" in pos                                    # настоящий чарт сохранён


def test_noop_when_clean():
    pos = {"GRID_ID": {"children": ["ROW-0"]}, "ROW-0": {"children": []}}
    assert c.clean_position(pos) == (0, 0)
    assert pos["GRID_ID"]["children"] == ["ROW-0"]


if __name__ == "__main__":
    test_removes_junk_keeps_legit()
    test_noop_when_clean()
    print("OK: clean_export проверки пройдены")

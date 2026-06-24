#!/usr/bin/env python3
"""Генерация реалистичных SMS-строк для sms.messages_mart и пакетная вставка в ClickHouse.

По умолчанию: 1 000 000 строк за период 2025-01-01 .. 2026-01-01 (соответствует data_spec.json),
8 клиентов (по 1–4 application_uuid каждый), с фиксированным seed для воспроизводимости.

Подключение читается из окружения (CLICKHOUSE_HOST / _HTTP_PORT / _DB / _USER / _PASSWORD);
объём/период/seed — из флагов CLI (которые откатываются к переменным окружения GEN_*).

Идемпотентно: пропускает вставку, если в таблице уже >= целевого числа строк
(если не передан --truncate / GEN_TRUNCATE=1).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import clickhouse_connect
import numpy as np
import pandas as pd

# --- справочные данные ------------------------------------------------------
# Базовые списки значений берутся из data_spec.json, если он есть (держит генератор
# в синхроне со спецификацией задания); константы ниже — это запасной вариант + веса
# и маппинги для реалистичности, которых в спецификации нет.

SPEC_PATH = Path(__file__).resolve().parent.parent / "data_spec.json"

FALLBACK = {
    "sender": ["Google", "Steam", "Amazon", "PayPal", "Uber", "WhatsApp", "Meta",
               "Grab", "Airbnb", "Netflix", "LinkedIn", "Outlook", "Twilio", "DHL",
               "Vodafone", "monitoring", "Spotify", "Bolt", "not_defined"],
    "country": ["Russian Federation", "Kazakhstan", "Azerbaijan", "Kyrgyzstan",
                "Tajikistan", "Belarus", "Uzbekistan", "Armenia", "Georgia",
                "Israel", "Moldova", "Abkhazia", "not_defined"],
    "delivery_status": ["DELIVRD", "UNDELIV", "EXPIRED", "NO ROUTES", "SENT",
                        "ROUTE FAILED", "REJECTD", "VND CHN NOT BND",
                        "SUBMIT_RESP TIMEOUT", "UNKNOWN"],
    "currency": ["EUR", "RUB", "USD"],
    "receiver_operator": ["MTS", "Beeline", "MegaFon", "Azercell", "MKS", "Tele2",
                          "All networks", "MEGACOM(Alfa Telecom)", "Indigo Tajikistan",
                          "Beeline (Sky Mobile)", "K-Cell", "O!(Nurtelecom)", "Scartel",
                          "MobiUZ (MTS)", "velcom", "Altel", "not_defined"],
}


def load_spec_values() -> dict:
    vals = dict(FALLBACK)
    try:
        spec = json.loads(SPEC_PATH.read_text())
        for key in ("sender", "country", "delivery_status", "currency", "receiver_operator"):
            v = spec.get("fields", {}).get(key, {}).get("values")
            if v:
                vals[key] = v
    except (OSError, ValueError):
        print("[generate] data_spec.json not read; using built-in value lists", file=sys.stderr)
    return vals


# Веса/маппинги для реалистичности (в спецификации их нет).
SENDER_NULL_RATE = 0.01           # настоящий NULL (отдельно от сентинела "not_defined")
COUNTRY_NULL_RATE = 0.006
OPERATOR_NULL_RATE = 0.008
CURRENCY_NULL_RATE = 0.004
DELETED_RATE = 0.015              # доля строк с меткой мягкого удаления

# Взвешенный состав стран (упор на СНГ). Ключи должны быть в списке стран из спецификации.
COUNTRY_WEIGHTS = {
    "Russian Federation": 42, "Kazakhstan": 14, "Uzbekistan": 9, "Belarus": 8,
    "Azerbaijan": 6, "Armenia": 5, "Georgia": 4, "Kyrgyzstan": 4, "Tajikistan": 3,
    "Israel": 2, "Moldova": 2, "Abkhazia": 1, "not_defined": 1,
}

# Правдоподобные операторы по стране (подмножество списка операторов из спецификации).
COUNTRY_OPERATORS = {
    "Russian Federation": ["MTS", "Beeline", "MegaFon", "Tele2", "Scartel"],
    "Kazakhstan": ["K-Cell", "Beeline", "Altel", "Tele2"],
    "Azerbaijan": ["Azercell", "Beeline"],
    "Kyrgyzstan": ["MEGACOM(Alfa Telecom)", "Beeline (Sky Mobile)", "O!(Nurtelecom)"],
    "Tajikistan": ["Indigo Tajikistan", "MKS", "Beeline"],
    "Belarus": ["velcom", "MTS", "All networks"],
    "Uzbekistan": ["MobiUZ (MTS)", "Beeline", "All networks"],
    "Armenia": ["Beeline", "MTS", "All networks"],
    "Georgia": ["MTS", "All networks"],
    "Israel": ["All networks"],
    "Moldova": ["All networks", "MTS"],
    "Abkhazia": ["All networks"],
    "not_defined": ["not_defined", "All networks"],
}

# Международные телефонные префиксы (для реалистичных получателей MSISDN).
COUNTRY_PREFIX = {
    "Russian Federation": "7", "Kazakhstan": "7", "Azerbaijan": "994",
    "Kyrgyzstan": "996", "Tajikistan": "992", "Belarus": "375", "Uzbekistan": "998",
    "Armenia": "374", "Georgia": "995", "Israel": "972", "Moldova": "373",
    "Abkhazia": "7840", "not_defined": "0",
}

# Базовая цена по стране (в валюте строки, держится в пределах диапазона спецификации [0, 1]).
COUNTRY_BASE_PRICE = {
    "Russian Federation": 0.030, "Kazakhstan": 0.045, "Azerbaijan": 0.060,
    "Kyrgyzstan": 0.050, "Tajikistan": 0.055, "Belarus": 0.040, "Uzbekistan": 0.050,
    "Armenia": 0.048, "Georgia": 0.052, "Israel": 0.075, "Moldova": 0.044,
    "Abkhazia": 0.035, "not_defined": 0.040,
}

STATUS_WEIGHTS = {
    "DELIVRD": 80, "SENT": 5, "UNDELIV": 5, "EXPIRED": 3, "REJECTD": 3,
    "NO ROUTES": 1.5, "ROUTE FAILED": 1.0, "VND CHN NOT BND": 0.6,
    "SUBMIT_RESP TIMEOUT": 0.5, "UNKNOWN": 0.4,
}
CURRENCY_WEIGHTS = {"RUB": 55, "USD": 25, "EUR": 20}
SEGMENT_POP = list(range(1, 15))
SEGMENT_WEIGHTS = [45, 25, 12, 6, 4, 2, 1.5, 1, 0.8, 0.6, 0.4, 0.3, 0.2, 0.2]
ATTEMPT_POP = [1, 2, 3]
ATTEMPT_WEIGHTS = [85, 12, 3]


def build_receiver_pools(countries, rng, per_country=8000):
    """Ограниченный пул получателей на страну (реальный трафик попадает в одних и тех же
    абонентов, что также держит словарь LowCardinality(receiver) в норме — см. docs/DESIGN.md).
    Каждый пул — numpy-массив строк MSISDN, чтобы gen_batch индексировал его векторно."""
    pools = {}
    for c in countries:
        prefix = COUNTRY_PREFIX.get(c, "0")
        digits = rng.integers(0, 10, size=(per_country, 9))
        bodies = digits.astype("U1").view("U9").reshape(per_country)
        pools[c] = np.char.add("+" + prefix, bodies)
    return pools


def build_customers(rng, n_customers=8):
    """n клиентов, у каждого 1–4 детерминированных application UUID; вес по размеру.
    Возвращает пары (customer_id, application_uuid) и нормализованный вектор
    вероятностей для numpy `choice`."""
    pairs, weights = [], []
    for i in range(n_customers):
        customer_id = 1001 + i
        n_apps = int(rng.integers(1, 5))         # 1–4 включительно
        size = int(rng.choice([1, 1, 2, 3, 5]))  # часть клиентов шлёт намного больше
        for a in range(n_apps):
            app_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"app-{customer_id}-{a}")
            pairs.append((customer_id, app_uuid))
            weights.append(size)
    p = np.array(weights, dtype=float)
    p /= p.sum()
    return pairs, p


COLUMNS = [
    "customer_id", "application_uuid", "message_id", "sent_date", "sender",
    "receiver", "country", "segment_count", "delivery_status", "attempt_number",
    "delivery_time", "price", "currency", "receiver_operator", "direction",
    "created_at", "updated_at", "deleted_at",
]


def _probs(weights):
    """Нормализует последовательность весов в вектор вероятностей для numpy `choice`."""
    p = np.asarray(weights, dtype=float)
    return p / p.sum()


def gen_batch(n, rng, start_dt, span_us, customers, cust_p, recv_pools, vals):
    """Строит `n` строк полностью векторно на numpy; возвращает pandas DataFrame в
    порядке COLUMNS, готовый к колоночной вставке. Без построчного цикла Python."""
    country_pop = np.array(list(COUNTRY_WEIGHTS.keys()), dtype=object)
    status_pop = np.array(list(STATUS_WEIGHTS.keys()), dtype=object)
    cur_pop = np.array(list(CURRENCY_WEIGHTS.keys()), dtype=object)
    senders = np.array(vals["sender"], dtype=object)
    seg_pop = np.array(SEGMENT_POP)
    attempt_pop = np.array(ATTEMPT_POP)

    # --- розыгрыш категориальных колонок (каждая — одним вызовом numpy) ----------
    cust_idx = rng.choice(len(customers), size=n, p=cust_p)
    cust_arr = np.array(customers, dtype=object)              # (m, 2): (id, uuid)
    customer_id = cust_arr[cust_idx, 0].astype(np.uint32)
    application_uuid = cust_arr[cust_idx, 1]

    country_idx = rng.choice(len(country_pop), size=n, p=_probs(list(COUNTRY_WEIGHTS.values())))
    countries = country_pop[country_idx]
    status_idx = rng.choice(len(status_pop), size=n, p=_probs(list(STATUS_WEIGHTS.values())))
    statuses = status_pop[status_idx]
    cur_idx = rng.choice(len(cur_pop), size=n, p=_probs(list(CURRENCY_WEIGHTS.values())))
    currencies = cur_pop[cur_idx]
    senders_col = senders[rng.choice(len(senders), size=n)]
    segments = seg_pop[rng.choice(len(seg_pop), size=n, p=_probs(SEGMENT_WEIGHTS))]
    attempts = attempt_pop[rng.choice(len(attempt_pop), size=n, p=_probs(ATTEMPT_WEIGHTS))]
    directions = rng.choice([1, 0], size=n, p=_probs([90, 10]))

    # --- метки времени (векторная арифметика datetime64[us]) --------------------
    start = np.datetime64(start_dt, "us")
    offsets_us = rng.integers(0, span_us, size=n, dtype=np.int64)
    sent = start + offsets_us.astype("timedelta64[us]")
    created = sent + rng.integers(50, 2001, size=n).astype("timedelta64[ms]")
    updated = created + rng.integers(0, 601, size=n).astype("timedelta64[s]")
    # мягкое удаление: только ~DELETED_RATE строк получают deleted_at, остальные остаются NaT
    del_mask = rng.random(n) < DELETED_RATE
    deleted = np.full(n, np.datetime64("NaT", "us"), dtype="datetime64[us]")
    deleted[del_mask] = (updated[del_mask]
                         + rng.integers(1, 86401, size=del_mask.sum()).astype("timedelta64[s]"))

    # --- delivery_time коррелирует с исходом (np.select по маскам статусов) ------
    mu = np.select(
        [statuses == "DELIVRD", np.isin(statuses, ["SENT", "UNKNOWN"])],
        [700.0, 1500.0], default=2200.0)
    sigma = np.where(statuses == "DELIVRD", 350.0, 600.0)
    delivery_time = np.clip(rng.normal(mu, sigma).astype(np.int32), 100, 3000).astype(np.uint16)

    # --- price = base(страна) * segments * джиттер, обрезано до [0, 1] -----------
    base = np.array([COUNTRY_BASE_PRICE.get(c, 0.04) for c in country_pop])[country_idx]
    price = np.round(np.clip(base * segments * rng.uniform(0.8, 1.3, size=n), 0.0, 1.0), 4)
    price = price.astype(np.float32)

    # --- оператор согласован со страной (+ редкий NULL) -------------------------
    operator = np.empty(n, dtype=object)
    for c in country_pop:
        m = countries == c
        ops = COUNTRY_OPERATORS.get(c, ["All networks"])
        operator[m] = np.array(ops, dtype=object)[rng.choice(len(ops), size=int(m.sum()))]
    operator[rng.random(n) < OPERATOR_NULL_RATE] = None

    # --- получатель берётся из ограниченного пула по стране ---------------------
    receiver = np.empty(n, dtype=object)
    for c in country_pop:
        m = countries == c
        pool = recv_pools[c]
        receiver[m] = pool[rng.integers(0, len(pool), size=int(m.sum()))]

    # --- маски NULL (настоящие NULL в nullable-колонках) ------------------------
    sender_col = senders_col.copy()
    sender_col[rng.random(n) < SENDER_NULL_RATE] = None
    country_val = countries.copy()
    country_val[rng.random(n) < COUNTRY_NULL_RATE] = None
    currency_val = currencies.copy()
    currency_val[rng.random(n) < CURRENCY_NULL_RATE] = None

    # --- message_id: случайные UUID v4 (единственная часть, что остаётся построчной)
    bits = rng.integers(0, 1 << 64, size=(n, 2), dtype=np.uint64)
    ints = (bits[:, 0].astype(object) << 64) | bits[:, 1].astype(object)
    message_id = [uuid.UUID(int=int(x), version=4) for x in ints]

    return pd.DataFrame({
        "customer_id": customer_id,
        "application_uuid": application_uuid,
        "message_id": message_id,
        "sent_date": sent,
        "sender": sender_col,
        "receiver": receiver,
        "country": country_val,
        "segment_count": segments.astype(np.uint32),
        "delivery_status": statuses,
        "attempt_number": attempts.astype(np.uint8),
        "delivery_time": delivery_time,
        "price": price,
        "currency": currency_val,
        "receiver_operator": operator,
        "direction": directions.astype(np.uint8),
        "created_at": created,
        "updated_at": updated,
        "deleted_at": deleted,
    }, columns=COLUMNS)


def report(client) -> None:
    """Печатает сводку проверки, чтобы генератор сам отчитался о результате
    (число строк, период, доля DELIVRD, число мягко удалённых, состав валют)."""
    total = int(client.command("SELECT count() FROM sms.messages_mart"))
    first, last = client.query("SELECT min(sent_date), max(sent_date) "
                               "FROM sms.messages_mart").result_rows[0]
    deleted = int(client.command("SELECT count() FROM sms.messages_mart "
                                 "WHERE deleted_at IS NOT NULL"))
    delivrd = client.command("SELECT round(100*countIf(delivery_status='DELIVRD')"
                             "/count(),2) FROM sms.messages_mart_active")
    print("[generate] ── verification ──────────────────────────────")
    print(f"[generate]   rows:          {total:,}")
    print(f"[generate]   period:        {first} .. {last}")
    print(f"[generate]   DELIVRD share: {delivrd}% (active rows)")
    print(f"[generate]   soft-deleted:  {deleted:,} ({100*deleted/total:.2f}%)")
    print("[generate]   currency mix (active):")
    for cur, n in client.query("SELECT ifNull(currency,'<NULL>'), count() "
                               "FROM sms.messages_mart_active GROUP BY 1 "
                               "ORDER BY 2 DESC").result_rows:
        print(f"[generate]     {cur:<8} {n:,}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate SMS rows into ClickHouse.")
    ap.add_argument("--rows", type=int, default=int(os.environ.get("GEN_ROWS", 1_000_000)))
    ap.add_argument("--start-date", default=os.environ.get("GEN_START", "2025-01-01"))
    ap.add_argument("--end-date", default=os.environ.get("GEN_END", "2026-01-01"))
    ap.add_argument("--batch-size", type=int, default=int(os.environ.get("GEN_BATCH", 50_000)))
    ap.add_argument("--seed", type=int, default=int(os.environ.get("GEN_SEED", 42)))
    ap.add_argument("--truncate", action="store_true",
                    default=os.environ.get("GEN_TRUNCATE", "").lower() in ("1", "true", "yes"))
    args = ap.parse_args()

    start_dt = datetime.strptime(args.start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(args.end_date, "%Y-%m-%d")
    span_us = int((end_dt - start_dt).total_seconds() * 1_000_000)
    if span_us <= 0:
        print("end-date must be after start-date", file=sys.stderr)
        return 2

    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_HTTP_PORT", 8123)),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "clickhouse"),
        database=os.environ.get("CLICKHOUSE_DB", "sms"),
    )

    if args.truncate:
        print("[generate] TRUNCATE sms.messages_mart")
        client.command("TRUNCATE TABLE IF EXISTS sms.messages_mart")

    # У command() широкий union-тип возврата; запрос count() всегда возвращает скалярный int.
    existing = int(client.command("SELECT count() FROM sms.messages_mart"))  # type: ignore[arg-type]
    if existing >= args.rows:
        print(f"[generate] table already has {existing:,} rows (>= target {args.rows:,}); "
              f"skipping. Use --truncate / GEN_TRUNCATE=1 to force.")
        report(client)
        return 0
    to_insert = args.rows - existing
    print(f"[generate] target={args.rows:,} existing={existing:,} -> inserting {to_insert:,} "
          f"rows over {args.start_date}..{args.end_date}, "
          f"batch={args.batch_size:,}, seed={args.seed}")

    rng = np.random.default_rng(args.seed)
    vals = load_spec_values()
    customers, cust_p = build_customers(rng)
    recv_pools = build_receiver_pools(list(COUNTRY_WEIGHTS.keys()), rng)
    print(f"[generate] {len(customers)} (customer, application) pairs across "
          f"{len({c for c, _ in customers})} customers")

    done = 0
    while done < to_insert:
        n = min(args.batch_size, to_insert - done)
        batch = gen_batch(n, rng, start_dt, span_us, customers, cust_p, recv_pools, vals)
        client.insert_df("messages_mart", batch)
        done += n
        print(f"[generate] inserted {done:,}/{to_insert:,}", flush=True)

    total = int(client.command("SELECT count() FROM sms.messages_mart"))  # type: ignore[arg-type]
    print(f"[generate] DONE. messages_mart now has {total:,} rows.")
    report(client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

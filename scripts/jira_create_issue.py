#!/usr/bin/env python3
"""Создание задачи в Jira Cloud из диагностики проекта (например, при падении smoke-теста).

Опциональная интеграция — стек работает и без неё. Используется в
`make smoke-test-with-jira`: если smoke-тест падает, его лог заводится как задача в Jira.

Только стандартная библиотека (urllib), Basic Auth по email + API-токену. Конфиг берётся из .env:
  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY, JIRA_ISSUE_TYPE (по умолчанию Bug)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def adf_text(text: str) -> dict:
    """Обернуть простой текст в Atlassian Document Format (обязателен для поля `description`)."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text[:30000] or " "}]}
        ],
    }


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Не задана обязательная переменная окружения: {name} (укажите её в .env)")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать задачу в Jira из диагностики проекта.")
    parser.add_argument("--summary", help="Заголовок задачи")
    parser.add_argument("--file", help="Файл лога/отчёта для описания задачи")
    parser.add_argument("--body", help="Текст описания задачи (используется, если нет --file)")
    parser.add_argument("--selftest", action="store_true", help="Офлайн-самопроверка и выход")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()
    if not args.summary:
        parser.error("--summary обязателен")

    base_url = require_env("JIRA_BASE_URL").rstrip("/")
    email = require_env("JIRA_EMAIL")
    token = require_env("JIRA_API_TOKEN")
    project_key = require_env("JIRA_PROJECT_KEY")
    issue_type = os.getenv("JIRA_ISSUE_TYPE", "Bug")

    if args.file:
        body = Path(args.file).read_text(encoding="utf-8", errors="replace")
    else:
        body = args.body or "Детали не предоставлены."

    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": args.summary,
            "issuetype": {"name": issue_type},
            "description": adf_text(body),
            "labels": ["sms-operations", "auto-created", "data-quality"],
        }
    }

    auth = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    req = urllib.request.Request(
        f"{base_url}/rest/api/3/issue",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"Ошибка Jira API {exc.code}: {exc.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Запрос к Jira не удался: {exc.reason}", file=sys.stderr)
        return 1

    print(f"Создана задача в Jira: {data.get('key')}")
    return 0


def _selftest() -> int:
    doc = adf_text("hello")
    assert doc["content"][0]["content"][0]["text"] == "hello"
    assert adf_text("x" * 50000)["content"][0]["content"][0]["text"].__len__() == 30000
    assert adf_text("")["content"][0]["content"][0]["text"] == " "  # ADF не принимает пустой текст
    print("самопроверка пройдена")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

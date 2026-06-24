#!/usr/bin/env python3
"""Идемпотентно регистрирует подключение к базе ClickHouse в Superset.

Запускается внутри контейнера Superset (Superset импортируем). Использует ФИКСИРОВАННЫЙ
uuid, чтобы импортируемый бандл дашборда (который ссылается на БД по этому uuid) переиспользовал
это подключение и не спрашивал пароль.
"""
import os

# Должен совпадать с databases/clickhouse_sms.yaml в импортируемом бандле.
DB_UUID = "1a1a1a1a-0000-4000-8000-000000000001"
DB_NAME = "ClickHouse SMS"


def main() -> int:
    host = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
    port = os.environ.get("CLICKHOUSE_HTTP_PORT", "8123")
    user = os.environ.get("CLICKHOUSE_USER", "default")
    pw = os.environ.get("CLICKHOUSE_PASSWORD", "clickhouse")
    dbname = os.environ.get("CLICKHOUSE_DB", "sms")
    uri = f"clickhousedb://{user}:{pw}@{host}:{port}/{dbname}"

    from superset.app import create_app

    app = create_app()
    with app.app_context():
        import uuid as _uuid

        from superset.models.core import Database

        # Настоящая библиотека `superset` (стоит только в контейнере Superset) совпадает
        # по имени с локальной папкой superset/ этого репозитория, поэтому mypy не находит `db`.
        from superset import db  # type: ignore[attr-defined]

        existing = (
            db.session.query(Database)
            .filter((Database.uuid == _uuid.UUID(DB_UUID)) | (Database.database_name == DB_NAME))
            .first()
        )
        if existing:
            existing.database_name = DB_NAME
            existing.uuid = _uuid.UUID(DB_UUID)
            existing.set_sqlalchemy_uri(uri)
            print(f"[register_db] updated existing connection '{DB_NAME}'")
        else:
            d = Database(database_name=DB_NAME)
            d.uuid = _uuid.UUID(DB_UUID)
            d.set_sqlalchemy_uri(uri)
            db.session.add(d)
            print(f"[register_db] created connection '{DB_NAME}'")
        db.session.commit()

        # Прогоняем подключение, чтобы ошибки всплыли здесь, а не в UI.
        try:
            d = db.session.query(Database).filter_by(uuid=_uuid.UUID(DB_UUID)).first()
            with d.get_sqla_engine() as engine:
                n = engine.connect().exec_driver_sql(
                    "SELECT count() FROM sms.messages_mart"
                ).scalar()
            print(f"[register_db] connection OK — sms.messages_mart has {n} rows")
        except Exception as exc:  # noqa: BLE001 — сообщить и продолжить
            print(f"[register_db] WARNING: connection test failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

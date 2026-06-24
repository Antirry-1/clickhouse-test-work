"""Конфигурация Superset для демо SMS Operations.

Демо-уровень: метаданные в SQLite на именованном томе, без Celery/Redis.
Достаточно, чтобы собрать и посмотреть дашборд SMS Operations; НЕ для продакшена.
"""
import os

# --- Безопасность -----------------------------------------------------------
SECRET_KEY = os.environ.get(
    "SUPERSET_SECRET_KEY", "CHANGE_ME_local_dev_secret_key_0123456789abcdef"
)

# --- База метаданных --------------------------------------------------------
# SQLite на смонтированном томе. Superset пишет предупреждение "не рекомендуется"; для демо ок.
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"
SQLALCHEMY_TRACK_MODIFICATIONS = False

# --- Флаги функций ----------------------------------------------------------
FEATURE_FLAGS = {
    "DASHBOARD_NATIVE_FILTERS": True,   # два глобальных фильтра (customer_id, sent_date)
    "DASHBOARD_CROSS_FILTERS": True,
    "EMBEDDED_SUPERSET": False,
}

# Удобство для демо: ослабляем CSRF, чтобы импорт ассетов + регистрация БД в init шли headless.
# Включите обратно для любого нелокального развёртывания.
WTF_CSRF_ENABLED = False
TALISMAN_ENABLED = False

# Позволяем скриптам импорта/регистрации и сгенерированной витрине свободно подключаться (демо).
PREVENT_UNSAFE_DB_CONNECTIONS = False

# Щедрые лимиты строк, чтобы графики дашборда не обрезались на таблице в 1M строк.
ROW_LIMIT = 100000
SQL_MAX_ROW = 1000000

# --- Кэширование результатов запросов ---------------------------------------
# Демо-уровень: внутрипроцессный Flask-Caching SimpleCache, чтобы повторные запросы
# графиков/фильтров не пересчитывались в ClickHouse при каждом рендере
# (OPTIMIZATION_PLAN №3.2 / DESIGN №11).
# Корректный для продакшена/мультиворкера выбор — RedisCache: gunicorn запускает --workers=2,
# поэтому SimpleCache — на процесс (у каждого воркера свой кэш) — для демо приемлемо,
# но общий кэш между воркерами требует Redis (CACHE_TYPE=RedisCache, CACHE_REDIS_URL=...).
CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300}
# DATA_CACHE_CONFIG — самый важный: он кэширует результаты запросов графиков.
DATA_CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300}
# Состояние native-фильтров + данные формы Explore: больший таймаут, чтобы два глобальных
# фильтра (customer_id, sent_date) работали плавно в течение сессии.
FILTER_STATE_CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 86400}
EXPLORE_FORM_DATA_CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 86400}

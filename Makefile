# SMS Operations mart — Makefile.
# Thin wrapper over docker compose. `make help` lists everything.
# Requires: docker (with the compose plugin). Copy .env.example -> .env first (auto-created by `make up`).

SHELL := /bin/bash
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help env up down clean reset rebuild ps logs init generate regenerate \
        superset-init dashboard superset-export smoke-test smoke-test-with-jira rows period delivery-rate ch-shell open \
        lint analyze benchmark test analytics-sql

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example if missing
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")

up: env ## Build images and start the whole stack (DDL + 1M rows + Superset dashboard run automatically)
	$(COMPOSE) up -d --build
	@echo "Stack starting. ClickHouse: http://localhost:8123  |  Superset: http://localhost:8088 (admin/admin)"
	@echo "First boot pulls images + inserts 1M rows + builds the dashboard — watch: make logs"

down: ## Stop and remove containers (keeps data volumes)
	$(COMPOSE) down

clean: ## Stop and remove containers AND volumes (full reset)
	$(COMPOSE) down -v

reset: clean up ## Full reset: wipe containers + volumes, then bring the whole stack back up

rebuild: clean up ## Alias of reset (wipe + rebuild from scratch)

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Tail logs of all services
	$(COMPOSE) logs -f

init: env ## Create database + table + view in ClickHouse (idempotent)
	$(COMPOSE) run --rm clickhouse-init

generate: env ## Generate data (skips if table already has >= target rows)
	$(COMPOSE) run --rm generator

regenerate: env ## Truncate and regenerate data from scratch
	$(COMPOSE) run --rm -e GEN_TRUNCATE=1 generator

superset-init: env ## (Re)run Superset migration + admin + ClickHouse connection
	$(COMPOSE) run --rm superset-init

dashboard: env ## Build/refresh the SMS Operations dashboard via the Superset REST API
	$(COMPOSE) run --rm superset-dashboard

superset-export: ## Export the live Superset dashboard to git-tracked YAML (superset/import_bundle/)
	$(COMPOSE) exec -T superset superset export-dashboards -f /tmp/sms_dash.zip
	$(COMPOSE) cp superset:/tmp/sms_dash.zip /tmp/sms_dash.zip
	rm -rf superset/import_bundle && mkdir -p superset/import_bundle
	cd superset/import_bundle && unzip -o -q /tmp/sms_dash.zip && \
	  top=$$(ls -d */ | head -1) && mv "$$top"* . && rmdir "$$top"
	python3 scripts/clean_export.py   # срезаем ряд-дубль, который экспортёр Superset дописывает в position
	@echo "Exported to superset/import_bundle/"

smoke-test: ## Verify the stack: containers up, ~1M rows, delivery-rate query works
	bash scripts/smoke_test.sh

smoke-test-with-jira: env ## Run smoke test; file a Jira issue if it fails (optional, needs JIRA_* in .env)
	@mkdir -p reports
	@set -o pipefail; \
	  if bash scripts/smoke_test.sh 2>&1 | tee reports/smoke_test.log; then \
	    echo "smoke test passed — no Jira issue created"; \
	  else \
	    python scripts/jira_create_issue.py --summary "SMS Operations smoke test failed" --file reports/smoke_test.log; \
	    exit 1; \
	  fi

analytics-sql: ## Run dql/analytics_queries.sql + cohort_lifecycle.sql against ClickHouse (stack must be up)
	@for f in dql/analytics_queries.sql dql/cohort_lifecycle.sql; do \
	  echo "=== $$f ==="; \
	  $(COMPOSE) exec -T clickhouse clickhouse-client --user $${CLICKHOUSE_USER:-default} \
	    --password $${CLICKHOUSE_PASSWORD:-clickhouse} --multiquery < "$$f" || exit 1; \
	done

analyze: env ## DQ + descriptive statistics over messages_mart (stack must be up); writes Parquet to reports/
	@mkdir -p reports
	$(COMPOSE) run --rm -v "$(PWD)/reports:/work/reports" --entrypoint bash generator \
	  -lc "python scripts/analyze_data.py --out-dir reports"

benchmark: env ## CSV/Parquet/Feather size & speed benchmark on mart data (no DB needed)
	$(COMPOSE) run --rm --no-deps --entrypoint bash generator \
	  -lc "python scripts/format_benchmark.py $(if $(ROWS),--rows $(ROWS),)"

test: env ## Run generator + analysis self-checks in the generator image (no DB needed)
	$(COMPOSE) run --rm --no-deps --entrypoint bash generator \
	  -lc "python scripts/test_generate.py && python scripts/test_analyze.py"

lint: ## Lint + type-check the Python scripts (pip install -r requirements-dev.txt first)
	ruff check .
	mypy

rows: ## Print row count in messages_mart
	@$(COMPOSE) exec -T clickhouse clickhouse-client --user $${CLICKHOUSE_USER:-default} --password $${CLICKHOUSE_PASSWORD:-clickhouse} \
	  --query "SELECT count() AS rows FROM sms.messages_mart"

period: ## Print min/max sent_date in the table
	@$(COMPOSE) exec -T clickhouse clickhouse-client --user $${CLICKHOUSE_USER:-default} --password $${CLICKHOUSE_PASSWORD:-clickhouse} \
	  --query "SELECT min(sent_date), max(sent_date) FROM sms.messages_mart"

delivery-rate: ## Print overall delivery rate (DELIVRD / total), soft-deleted excluded
	@$(COMPOSE) exec -T clickhouse clickhouse-client --user $${CLICKHOUSE_USER:-default} --password $${CLICKHOUSE_PASSWORD:-clickhouse} \
	  --query "SELECT round(100*countIf(delivery_status='DELIVRD')/count(),2) AS delivery_rate_pct FROM sms.messages_mart_active"

ch-shell: ## Open an interactive ClickHouse client
	$(COMPOSE) exec clickhouse clickhouse-client --user $${CLICKHOUSE_USER:-default} --password $${CLICKHOUSE_PASSWORD:-clickhouse}

open: ## Print the Superset URL
	@echo "Superset: http://localhost:$${SUPERSET_PORT:-8088}  (login admin / admin)"

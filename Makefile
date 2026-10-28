# iris-signal-harvester — developer and reviewer entry points.
#
#   make demo      <- start here. Offline, no credentials. See the target for exactly what it does.
#
# Requirements: GNU make and Docker (Compose v2) for up/demo/harvest/report/down.
#               Python 3.11+ for install/verify/test/lint/typecheck.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
VENV    ?= .venv
PYTHON  := $(VENV)/bin/python
IRIS    := $(VENV)/bin/iris-harvest

# Run the harvester container as the invoking user so ./reports stays writable by you.
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)

# Host-side view of the compose database, used only by test-integration. Same defaults as
# docker-compose.yml. make does not read .env: if you changed IRIS_POSTGRES_* or IRIS_DB_HOST_PORT
# in .env, also export them in your shell for this target.
PGUSER := $(or $(IRIS_POSTGRES_USER),iris)
PGPASS := $(or $(IRIS_POSTGRES_PASSWORD),iris_local)
PGDB   := $(or $(IRIS_POSTGRES_DB),iris)
PGPORT := $(or $(IRIS_DB_HOST_PORT),5433)

# Source for `make harvest`: oparl | fixture | ratsdokumente
SOURCE ?= oparl
# Extra arguments for `make harvest` / `make report`, e.g. ARGS="--since 2026-10-01 --limit 200"
ARGS ?=

.PHONY: help install up verify demo test test-integration harvest report lint typecheck format down reset

help: ## List targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-17s %s\n", $$1, $$2}'

install: ## Create .venv and install the package with dev dependencies
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

up: ## Start PostGIS and apply migrations
	@mkdir -p reports
	$(COMPOSE) up -d --wait db
	$(COMPOSE) run --rm -T migrate

verify: ## Probe the briefed source (needs network); host Python, writes the evidence log
	@test -x $(IRIS) || { echo "No $(IRIS). Run 'make install' first."; exit 1; }
	$(IRIS) verify-source

# Which committed corpus cassette `make demo` replays. corpus_degraded exercises the
# failure path (rejection histogram, FAIL verdict) instead of the clean one.
DEMO_SCENARIO ?= corpus_main

demo: ## Offline end-to-end: PostGIS, migrations, harvest twice over fixtures, print the re-run delta
	@mkdir -p reports
	@echo ">> demo: resetting the local compose database (volume iris-pgdata) for a clean first pass"
	$(COMPOSE) down --volumes --remove-orphans
	$(COMPOSE) build harvester
	$(COMPOSE) up -d --wait db
	$(COMPOSE) run --rm -T migrate
	@echo
	@echo ">> demo: PASS 1 of 2 - first load from the committed 761-paper corpus (no network)"
	@IRIS_SOURCE=fixture $(COMPOSE) run --rm -T \
	  -e IRIS_FIXTURE_DIR=/app/tests/fixtures/cassettes/corpus \
	  harvester harvest --scenario $(DEMO_SCENARIO) --gazetteer config/gazetteer_corpus.json \
	  || echo ">> pass 1 exited $$? (the exit code follows the quality-gate verdict; the delta below is what the demo asserts)"
	@echo
	@echo ">> demo: PASS 2 of 2 - identical input, must change nothing"
	@IRIS_SOURCE=fixture $(COMPOSE) run --rm -T \
	  -e IRIS_FIXTURE_DIR=/app/tests/fixtures/cassettes/corpus \
	  harvester harvest --scenario $(DEMO_SCENARIO) --gazetteer config/gazetteer_corpus.json \
	  || echo ">> pass 2 exited $$? (the exit code follows the quality-gate verdict; the delta below is what the demo asserts)"
	@echo
	@COMPOSE="$(COMPOSE)" scripts/demo_delta.sh
	@echo
	@echo ">> demo: invariant queries (sql/validation_queries.sql)"
	@IRIS_SOURCE=fixture $(COMPOSE) run --rm -T harvester validate
	@echo
	@echo ">> demo complete. Reports are in ./reports ; the database is still up (make down to stop)."

test: ## Full pytest suite; integration tests run only when IRIS_DB_DSN is set, else they skip
	$(PYTHON) -m pytest

test-integration: up ## Full suite including integration tests, against the compose PostGIS
	IRIS_DB_DSN="postgresql://$(PGUSER):$(PGPASS)@127.0.0.1:$(PGPORT)/$(PGDB)" $(PYTHON) -m pytest

harvest: up ## Incremental run against SOURCE (default oparl, live network). ARGS="--since ... --limit ..."
	IRIS_SOURCE=$(SOURCE) $(COMPOSE) run --rm -T harvester harvest $(ARGS)

report: up ## Regenerate the quality report for the latest run (ARGS="--run-id <uuid>" for a specific one)
	$(COMPOSE) run --rm -T harvester report $(ARGS)

lint: ## ruff check + ruff format --check
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

typecheck: ## mypy --strict on src (strictness is set in pyproject.toml)
	$(PYTHON) -m mypy

format: ## Apply ruff formatting and safe autofixes
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

down: ## Stop the stack; keeps the database volume
	$(COMPOSE) down --remove-orphans

reset: ## Stop the stack and DELETE the database volume
	$(COMPOSE) down --volumes --remove-orphans

# --- dashboard (appended; read-only Streamlit UI, see ui/README.md) ---
.PHONY: dashboard
dashboard: ## Read-only Streamlit dashboard on http://localhost:8501. Uses IRIS_DB_DSN when set, else the committed sample run.
	@$(PYTHON) -c "import streamlit" 2>/dev/null || { echo "Run: $(PYTHON) -m pip install -r requirements.txt"; exit 1; }
	@test -n "$$IRIS_DB_DSN" \
	  && echo ">> dashboard: IRIS_DB_DSN is set - reading the live database (read-only)" \
	  || echo ">> dashboard: no IRIS_DB_DSN - falling back to the committed SYNTHETIC sample run (banner shown in-app)"
	$(PYTHON) -m streamlit run streamlit_app.py

VENV ?= .venv
CORE ?= ../airflow-dq-agent
PORT ?= 8101

.PHONY: help setup browser-install db run test lint typecheck check
.DEFAULT_GOAL := help

help:
	@echo 'make setup CORE=/path/to/airflow-dq-agent; make browser-install'
	@echo 'make db (terminal 1); make run (terminal 2, localhost only)'
	@echo 'make check (tests including Chromium, lint, formatting, typecheck)'

setup:
	uv venv "$(VENV)"
	uv pip install --python "$(VENV)/bin/python" -e '.[dev]' -e '$(CORE)'

browser-install:
	"$(VENV)/bin/playwright" install chromium

db:
	"$(VENV)/bin/python" scripts/dev_pg.py

run:
	"$(VENV)/bin/uvicorn" invoice_triage.app:app --host 127.0.0.1 --port $(PORT)

test:
	"$(VENV)/bin/python" -m pytest -q --tb=short '--core-checkout=$(CORE)'

lint:
	"$(VENV)/bin/ruff" check src tests scripts
	"$(VENV)/bin/ruff" format --check src tests scripts

typecheck:
	"$(VENV)/bin/mypy" src tests scripts

check: test lint typecheck

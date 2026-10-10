# ---------------------------------------------------------------------------
# Healthcare Operations Analytics Platform
#
#   make install     create the venv and install dependencies
#   make data        regenerate the synthetic raw CSVs
#   make pipeline    rebuild the DuckDB analytical store from data/raw
#   make api         run the API with reload on http://127.0.0.1:8000
#   make test        run the test suite
#   make check       byte-compile every module (fast syntax gate)
#   make reset       delete the analytical store so the next pipeline rebuilds
#   make clean       remove caches and build artefacts
#
# Every target is idempotent: running `make pipeline` twice produces the same
# store, because the generator is seeded (HOA_SYNTHETIC_SEED).
# ---------------------------------------------------------------------------

SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV        := .venv
PYTHON      := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
UVICORN     := $(VENV)/bin/uvicorn

BACKEND     := backend
RAW_DIR     := data/raw
STORE       := data/processed/healthcare_ops.duckdb

HOST        ?= 127.0.0.1
PORT        ?= 8000

.PHONY: help install data pipeline api test check reset clean

help:
	@echo "Healthcare Operations Analytics Platform"
	@echo ""
	@echo "  make install    Create $(VENV) and install dependencies"
	@echo "  make data       Regenerate synthetic raw CSVs into $(RAW_DIR)"
	@echo "  make pipeline   Build the analytical store at $(STORE)"
	@echo "  make api        Serve the API on http://$(HOST):$(PORT)"
	@echo "  make test       Run the test suite"
	@echo "  make check      Byte-compile every module"
	@echo "  make reset      Delete the analytical store"
	@echo "  make clean      Remove caches and build artefacts"

# --- Setup -----------------------------------------------------------------

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

install: $(VENV)/bin/activate
	$(PIP) install -r $(BACKEND)/requirements.txt
	@echo "Installed. Next: make data && make pipeline"

# --- Data ------------------------------------------------------------------

data:
	$(PYTHON) scripts/generate_data.py

pipeline:
	cd $(BACKEND) && ../$(PYTHON) -c "from app.pipeline import rebuild_database; o = rebuild_database(); print(o.to_dict()['status'], o.accepted_rows, 'rows accepted,', o.rejected_rows, 'quarantined')"

reset:
	rm -f $(STORE) $(STORE).wal $(STORE).tmp
	@echo "Removed $(STORE). Run 'make pipeline' to rebuild."

# --- Run -------------------------------------------------------------------

api:
	cd $(BACKEND) && ../$(UVICORN) main:app --host $(HOST) --port $(PORT) --reload

# --- Quality ---------------------------------------------------------------

test:
	$(PYTHON) -m pytest $(BACKEND) -q

check:
	$(PYTHON) -m compileall -q $(BACKEND)/app $(BACKEND)/main.py scripts
	@echo "All modules compile."

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage coverage.xml
	@echo "Cleaned caches."

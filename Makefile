# AAAC — M3 (evaluation) targets. See CLAUDE.md §4.
#
# Everything an examiner needs is here: `make check` for the code, and
# `make verify-testbed` before any experiment run.

PY       := .venv/bin/python
PIP      := .venv/bin/pip
SEEDS    ?= 1,2,3,4,5
RUN_ID   ?= dev
MODE     ?= none
RESULTS  ?= results

.DEFAULT_GOAL := help

.PHONY: help venv install test lint typecheck check fmt \
        build up down ps logs verify-testbed \
        population experiment figures report clean clean-results

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- environment -----------------------------------------------------------

venv: ## Create .venv on Python 3.11 (the contracted version, §3.1)
	python3.11 -m venv .venv
	$(PIP) install --quiet --upgrade pip

install: venv ## Install the package and dev tooling
	$(PIP) install --quiet -e ".[dev]"

# --- code quality ----------------------------------------------------------

test: ## Run the unit tests (no Redis, no network — §3.10 rule 4)
	$(PY) -m pytest

lint: ## ruff
	.venv/bin/ruff check src tests

fmt: ## ruff --fix
	.venv/bin/ruff check --fix src tests

typecheck: ## mypy (non-strict, §3.1)
	.venv/bin/mypy

check: lint typecheck test ## Everything above

# --- testbed ---------------------------------------------------------------

build: ## Build the container images
	docker compose build

up: ## Start the testbed (origin, redis, iperf, shaped clients)
	docker compose up -d --build
	@echo "Waiting for the origin to answer /origin/health ..."
	@for i in $$(seq 1 60); do \
	    curl -sf http://127.0.0.1:8002/origin/health >/dev/null && break; \
	    sleep 0.5; \
	done
	@curl -s http://127.0.0.1:8002/origin/health; echo

down: ## Stop the testbed and remove volumes
	docker compose down -v

ps: ## Show container status
	docker compose ps

logs: ## Tail all container logs
	docker compose logs -f --tail=100

verify-testbed: ## THE GATE (§4.2). Measure achieved rate/RTT/loss inside each client
	$(PY) -m aaac.evaluation.testbed.verify \
	    --json-out $(RESULTS)/testbed-verification-$(RUN_ID).json

# --- experiment ------------------------------------------------------------

population: ## Generate the replayable client population for SEEDS
	$(PY) -m aaac.evaluation.population --seeds $(SEEDS) --out $(RESULTS)

experiment: verify-testbed ## Run all modes x SEEDS (verifies the testbed first)
	$(PY) -m aaac.evaluation.experiment --seeds $(SEEDS) --results $(RESULTS)

figures: ## Regenerate every figure from the event logs
	$(PY) -m aaac.evaluation.plots --results $(RESULTS)

report: ## Generate results/report-$(RUN_ID).md from the event logs
	$(PY) -m aaac.evaluation.report --run-id $(RUN_ID) --results $(RESULTS)

# --- housekeeping ----------------------------------------------------------

clean: ## Remove caches and build artefacts (leaves results/ alone)
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist src/*.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

clean-results: ## Delete generated run output. Asks first — these are measurements.
	@echo "This deletes event logs and figures under $(RESULTS)/."
	@printf "Type 'yes' to confirm: " && read ans && [ "$$ans" = "yes" ]
	rm -f $(RESULTS)/*.jsonl $(RESULTS)/*.json $(RESULTS)/report-*.md
	rm -rf $(RESULTS)/figures

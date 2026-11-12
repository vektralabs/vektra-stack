# ==========================================================================
# Vektra RAG platform - operator Makefile
# REQ-018: Makefile targets for MVP workflow
# ==========================================================================

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

# Configurable variables
VEKTRA_PORT    ?= 8000
BASE_URL       ?= http://localhost:$(VEKTRA_PORT)
COMPOSE        := docker compose
HEALTH_TIMEOUT ?= 120

# Colors (disabled when output is not a terminal)
ifneq ($(TERM),)
  GREEN  := \033[32m
  YELLOW := \033[33m
  RED    := \033[31m
  CYAN   := \033[36m
  BOLD   := \033[1m
  RESET  := \033[0m
else
  GREEN  :=
  YELLOW :=
  RED    :=
  CYAN   :=
  BOLD   :=
  RESET  :=
endif

# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

.PHONY: help up down health ingest query demo logs test test-lms-moodle test-assess-ui test-assess-e2e lint reindex batch-ingest eval-retrieval eval-e2e eval-assess assess-keys test-chaos-assess

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { \
		printf "  $(CYAN)%-12s$(RESET) %s\n", $$1, $$2 \
	}' $(MAKEFILE_LIST)

up: ## Start the Docker Compose stack
	@printf "$(BOLD)Starting Vektra stack...$(RESET)\n"
	$(COMPOSE) up -d
	@printf "$(GREEN)Stack started.$(RESET) Health: $(BASE_URL)/health\n"

down: ## Stop the Docker Compose stack
	@printf "$(BOLD)Stopping Vektra stack...$(RESET)\n"
	$(COMPOSE) down
	@printf "$(GREEN)Stack stopped.$(RESET)\n"

health: ## Check /health (set VEKTRA_API_KEY for detailed output)
	@scripts/health.sh

ingest: ## Ingest a file: make ingest FILE=path/to/doc.pdf [NS=default]
	$(if $(FILE),,$(error FILE is required. Usage: make ingest FILE=path/to/doc.pdf))
	@scripts/ingest.sh "$(FILE)" "$(or $(NS),default)"

query: ## Run a query: make query Q="your question" [CID=conversation_id]
	$(if $(Q),,$(error Q is required. Usage: make query Q="your question"))
	@scripts/query.sh "$(Q)" "$(CID)"

demo: ## Full MVP demo: up -> health -> ingest sample -> query (REQ-027)
	@scripts/demo.sh

logs: ## Tail Docker Compose logs
	$(COMPOSE) logs -f

test: ## Run unit tests with coverage
	uv run pytest \
		vektra-shared/tests/ \
		vektra-admin/tests/ \
		vektra-core/tests/ \
		vektra-ingest/tests/ \
		vektra-index/tests/ \
		vektra-analytics/tests/ \
		vektra-learn/tests/ \
		vektra-assess/tests/ \
		vektra-app/tests/ \
		-v --tb=short -m "not integration"

# The Moodle LMS provider is not a workspace member (ADR-0033 D6): it is installed only by the
# opt-in group, and its suites run with the platform beside it. UV_NO_SYNC: the suites' own
# `uv run alembic` would otherwise sync the venv back to the default groups mid-run.
test-lms-moodle: ## Moodle LMS provider (adapters/): its tests, the conformance kit, the joint suites (needs Docker)
	uv sync --frozen --group lms-moodle
	UV_NO_SYNC=1 uv run pytest adapters/vektra-lms-moodle/tests -v --tb=short \
		--cov=vektra_lms_moodle --cov-report=term-missing --cov-fail-under=95

test-assess-ui: ## Assess discussion UI: no-HTML-sink lint, type check, build, vitest (needs Node 22)
	cd vektra-assess/ui && npm ci --ignore-scripts && npm run lint && npm run typecheck && npm run build && npm test

test-assess-e2e: ## Assess discussion UI in Chromium: Playwright + axe, two origins, fake model (needs Docker, Node 22)
	cd vektra-assess/ui && npm ci --ignore-scripts && npx playwright install chromium
	VEKTRA_ASSESS_E2E_REQUIRED=1 uv run pytest vektra-assess/tests/integration/test_ui_e2e.py -m integration -v

# The assess chaos suite (plan assess-hardening-release): real worker processes killed, Postgres
# restarted, links blackholed; every scenario ends with tests/chaos/assess/invariants.py. The gate
# fails on a skipped case, on fewer cases or invariant reports than declared, or on any violation.
# This target is the one definition of the chaos run: the CI chaos jobs call `make test-chaos-assess`.
CHAOS_DIR_OUT ?= test-results/assess-chaos
CHAOS_MIN_CASES ?= 71
CHAOS_MIN_SCENARIOS ?= 27
test-chaos-assess: ## Assess chaos suite + invariant gate (needs Docker); report in test-results/assess-chaos
	rm -rf $(CHAOS_DIR_OUT) && mkdir -p $(CHAOS_DIR_OUT)
	CHAOS_REPORT_DIR=$(CHAOS_DIR_OUT) uv run pytest tests/chaos/assess vektra-assess/tests/integration/test_chaos_*.py -v --tb=short \
		--junitxml=$(CHAOS_DIR_OUT)/junit.xml; status=$$?; \
	uv run python tests/chaos/assess/gate.py $(CHAOS_DIR_OUT)/junit.xml $(CHAOS_DIR_OUT) \
		$(CHAOS_MIN_CASES) $(CHAOS_MIN_SCENARIOS) && exit $$status

# --provider-secret: the credentials file the Moodle LMS provider declares (ADR-0033 D5), empty
assess-keys: ## Generate a complete set of assess keyrings in ./secrets/assess/ (mode 0400, never overwrites)
	uv run python -m vektra_assess.keys generate --out ./secrets/assess --provider-secret assess_ws_tokens

lint: ## Run linters (ruff + mypy + import-linter)
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy \
		vektra-shared/src/vektra_shared \
		vektra-admin/src/vektra_admin \
		vektra-core/src/vektra_core \
		vektra-ingest/src/vektra_ingest \
		vektra-index/src/vektra_index \
		vektra-analytics/src/vektra_analytics \
		vektra-learn/src/vektra_learn \
		vektra-assess/src/vektra_assess \
		vektra-app/src/vektra_app \
		testkit/src/vektra_testkit \
		adapters/vektra-lms-moodle/src/vektra_lms_moodle
	uv run lint-imports

reindex: ## Create reindex job (skeleton): make reindex VER=2 [NS=default]
	$(if $(VER),,$(error VER is required. Usage: make reindex VER=2 [NS=default]))
	@scripts/reindex.sh "$(VER)" "$(or $(NS),default)"

batch-ingest: ## Batch ingest files: make batch-ingest DIR=path/to/docs [NS=default]
	$(if $(DIR),,$(error DIR is required. Usage: make batch-ingest DIR=path/to/docs))
	@scripts/batch-ingest.sh "$(DIR)" "$(or $(NS),default)"

eval-retrieval: ## Run retrieval-only evaluation (no LLM calls)
	uv run python scripts/eval_retrieval.py $(EVAL_ARGS)

eval-e2e: ## Run end-to-end RAG evaluation (with LLM calls)
	uv run python scripts/eval_e2e.py $(EVAL_ARGS)

eval-assess: ## Assess discussion eval (staging tier, real model + judge); exits non-zero unless PASS
	uv run python tests/eval/eval_assess.py $(EVAL_ARGS)

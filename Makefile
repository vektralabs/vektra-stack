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

.PHONY: help up down health ingest query demo logs test lint reindex batch-ingest eval-retrieval eval-e2e

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
		vektra-app/tests/ \
		-v --tb=short -m "not integration"

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
		vektra-learn/src/vektra_learn
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

# Thin wrappers around the real Python commands. Keep in sync with the
# Commands table in CLAUDE.md (same commands, two audiences: humans/CI use
# `make`, agents use the table directly).

.PHONY: setup dev test test-one lint format build typecheck clean

setup: ## Install dependencies
	pip install -e ".[dev]"

dev: ## Run the dev server locally
	uvicorn app.main:app --reload --port $${PORT:-8420}

test: ## Run the full test suite
	pytest

test-one: ## Run a single test — usage: make test-one T=path/or/name
	pytest $(T)

lint: ## Run the linter
	ruff check .

format: ## Auto-format the codebase
	ruff format .

typecheck: ## Run the type checker
	@echo "No type-checker configured yet (consider mypy/pyright if this grows)."

build: ## Build the Docker image
	docker build -t mygymapp-sync-server .

clean: ## Remove build artifacts and caches
	rm -rf .pytest_cache .ruff_cache **/__pycache__ *.egg-info

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "%-12s %s\n", $$1, $$2}'

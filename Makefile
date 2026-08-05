# Stack-agnostic entry points. Every target here is a thin wrapper around
# the real per-language command — fill those in once the stack is chosen,
# and keep this in sync with the Commands table in CLAUDE.md (same commands,
# two audiences: humans/CI use `make`, agents use the table directly).
#
# Delete targets that don't apply to this project (e.g. no `build` for a
# script-only repo); don't leave fake commands behind just to fill the list.

.PHONY: setup dev test test-one lint format build typecheck clean

setup: ## Install dependencies
	@echo "TODO: wire up dependency install, e.g. npm ci / poetry install / go mod download"

dev: ## Run the dev server/app locally
	@echo "TODO: wire up dev server command"

test: ## Run the full test suite
	@echo "TODO: wire up test command"

test-one: ## Run a single test — usage: make test-one T=path/or/name
	@echo "TODO: wire up single-test command, using \$$T"

lint: ## Run the linter
	@echo "TODO: wire up lint command"

format: ## Auto-format the codebase
	@echo "TODO: wire up formatter command"

typecheck: ## Run the type checker
	@echo "TODO: wire up type-check command"

build: ## Produce a production build/artifact
	@echo "TODO: wire up build command"

clean: ## Remove build artifacts and caches
	@echo "TODO: wire up clean command"

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "%-12s %s\n", $$1, $$2}'

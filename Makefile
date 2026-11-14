.PHONY: help lint format compose-lint test-compose verify verify-all clean-all

LABS := redis postgres clickhouse mongodb cassandra neo4j vault linux chaos
LAB ?= redis

help:
	@echo "Systems Under Stress - Root Workspace Targets:"
	@echo "  make lint         Check code formatting and linting (ruff)"
	@echo "  make format       Auto-format Python files (ruff)"
	@echo "  make compose-lint Validate Docker Compose configurations across all labs"
	@echo "  make verify       Run end-to-end verification for LAB=<name> (default: redis)"
	@echo "  make verify-all   Run end-to-end verification across all labs"
	@echo "  make clean-all    Tear down and wipe named volumes across all labs"

lint:
	uvx ruff check .
	uvx ruff format --check .

format:
	uvx ruff check --fix .
	uvx ruff format .

compose-lint:
	@for d in $(LABS); do \
		echo "Validating $$d/docker-compose.yml..."; \
		(cd $$d && docker compose --profile "*" config -q) || exit 1; \
	done
	@echo "Auditing compose safety invariants..."
	@uv run scripts/check_invariants.py
	@echo "All compose configurations and invariants valid."

test-compose: compose-lint

verify:
	$(MAKE) -C $(LAB) verify

verify-all:
	@for d in $(LABS); do \
		echo "=== Verifying $$d ==="; \
		$(MAKE) -C $$d verify || exit 1; \
	done

clean-all:
	@for d in $(LABS); do \
		echo "Cleaning $$d..."; \
		(cd $$d && docker compose --profile "*" down -v --remove-orphans); \
	done

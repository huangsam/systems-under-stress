.PHONY: help lint format compose-lint test-compose clean-all

help:
	@echo "Systems Under Stress - Root Workspace Targets:"
	@echo "  make lint         Check code formatting and linting (ruff + ktlint)"
	@echo "  make format       Auto-format Python and Kotlin files (ruff + ktlint)"
	@echo "  make compose-lint Validate Docker Compose configurations across all labs"
	@echo "  make clean-all    Tear down and wipe named volumes across all labs"

lint:
	uvx ruff check .
	uvx ruff format --check .
	ktlint "**/*.main.kts"

format:
	uvx ruff check --fix .
	uvx ruff format .
	ktlint --format "**/*.main.kts"

compose-lint:
	@for d in redis postgres clickhouse mongodb cassandra neo4j vault linux; do \
		echo "Validating $$d/docker-compose.yml..."; \
		(cd $$d && docker compose --profile "*" config -q) || exit 1; \
	done
	@echo "All compose configurations valid."

test-compose: compose-lint

clean-all:
	@for d in redis postgres clickhouse mongodb cassandra neo4j vault linux; do \
		echo "Cleaning $$d..."; \
		(cd $$d && docker compose --profile "*" down -v --remove-orphans); \
	done

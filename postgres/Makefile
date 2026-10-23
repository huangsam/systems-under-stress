# Makefile for PostgreSQL Labs
EXP ?= isolation
SCRIPT ?= 01_isolation

.PHONY: help up down clean soft-reset run run-kt psql logs

help:
	@echo "PostgreSQL Experiments Harness"
	@echo "=============================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start container for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
	@echo "  make soft-reset             Re-seed tables via init.sql (fast reset)"
	@echo "  make down EXP=<profile>     Stop and wipe volume for specific profile"
	@echo "  make clean                  Stop all profiles and wipe all lab volumes"
	@echo "  make psql EXP=<profile>     Open interactive psql terminal inside container"
	@echo "  make logs EXP=<profile>     Follow container logs"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - isolation       (Standard settings, testing write skew & dirty reads)"
	@echo "  - locks           (deadlock_timeout=100ms, testing row lock contention)"
	@echo "  - work-mem        (work_mem=4MB, testing sort spill-to-disk)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	docker compose exec -T $(EXP) psql -U postgres -d lab_db -f /docker-entrypoint-initdb.d/init.sql
	@echo "==> PostgreSQL schema re-seeded."

run:
	uv run experiments/$(SCRIPT).py

run-kt:
	kotlin experiments/$(SCRIPT).main.kts

psql:
	docker compose exec -it $(EXP) psql -U postgres -d lab_db

logs:
	docker compose logs -f $(EXP)

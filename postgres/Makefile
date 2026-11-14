# Makefile for PostgreSQL Labs
EXP ?= isolation
SCRIPT ?= 01_isolation

TARGET_SVC = $(if $(filter replication,$(EXP)),postgres-primary,$(EXP))

.PHONY: help up down clean soft-reset run psql logs verify

help:
	@echo "PostgreSQL Experiments Harness"
	@echo "=============================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start container for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
	@echo "  make verify                 Run end-to-end verification across all profiles"
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
	@echo "  - replication     (Physical streaming replication, testing standby query conflicts)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	docker compose exec -T $(TARGET_SVC) psql -U postgres -d lab_db -f /docker-entrypoint-initdb.d/init.sql
	@echo "==> PostgreSQL schema re-seeded."

run:
	uv run experiments/$(SCRIPT).py

psql:
	docker compose exec -it $(TARGET_SVC) psql -U postgres -d lab_db

logs:
	docker compose logs -f $(TARGET_SVC)

VERIFY_CASES = \
	isolation:00_crash_course,01_isolation \
	locks:02_locks \
	work-mem:03_work_mem \
	replication:04_replication_conflicts

verify:
	@set -e; \
	for spec in $(VERIFY_CASES); do \
		p=$${spec%%:*}; scripts=$${spec#*:}; \
		echo "==> Verifying postgres [$$p] -> $$scripts"; \
		$(MAKE) up EXP=$$p; \
		rc=0; \
		for s in $$(echo "$$scripts" | tr ',' ' '); do \
			echo "--> Running $$s (profile=$$p)"; \
			$(MAKE) run EXP=$$p SCRIPT=$$s || { rc=$$?; break; }; \
		done; \
		$(MAKE) down EXP=$$p || true; \
		[ $$rc -eq 0 ] || exit $$rc; \
	done

# Makefile for Network Chaos Experiments
EXP ?= chaos
SCRIPT ?= 00_crash_course

.PHONY: help up down clean soft-reset run logs verify

help:
	@echo "Network Chaos Experiments Harness"
	@echo "================================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start container for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
	@echo "  make verify                 Run end-to-end verification across all profiles"
	@echo "  make soft-reset             Reset all active toxics and flush upstream datastore"
	@echo "  make down EXP=<profile>     Stop and wipe container for specific profile"
	@echo "  make clean                  Stop all profiles and wipe all lab containers"
	@echo "  make logs EXP=<profile>     Follow container logs"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - chaos                     (Toxiproxy sidecar + Redis upstream)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	@curl -s -X POST http://127.0.0.1:8474/reset > /dev/null || true
	@docker compose exec -T redis-upstream redis-cli FLUSHALL > /dev/null 2>&1 || true
	@echo "==> Toxics reset and upstream flushed."

run:
	uv run experiments/$(SCRIPT).py

logs:
	docker compose logs -f $(EXP)

VERIFY_CASES = \
	chaos:00_crash_course,01_pool_starvation,02_retry_storms_vs_backoff,03_half_open_tcp_hangs

verify:
	@set -e; \
	for spec in $(VERIFY_CASES); do \
		p=$${spec%%:*}; scripts=$${spec#*:}; \
		echo "==> Verifying chaos [$$p] -> $$scripts"; \
		$(MAKE) up EXP=$$p; \
		rc=0; \
		for s in $$(echo "$$scripts" | tr ',' ' '); do \
			echo "--> Running $$s (profile=$$p)"; \
			$(MAKE) run EXP=$$p SCRIPT=$$s || { rc=$$?; break; }; \
		done; \
		$(MAKE) down EXP=$$p || true; \
		[ $$rc -eq 0 ] || exit $$rc; \
	done

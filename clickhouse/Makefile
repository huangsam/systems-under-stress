# Makefile for ClickHouse Limits & Internals Lab
EXP ?= parts-limit
SCRIPT ?= 01_parts_explosion

.PHONY: help up down clean soft-reset run client logs verify

help:
	@echo "ClickHouse Limit Testing Harness"
	@echo "================================"
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start container for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
	@echo "  make verify                 Run end-to-end verification across all profiles"
	@echo "  make soft-reset             Drop all experimental tables (fast reset)"
	@echo "  make client EXP=<profile>   Open interactive clickhouse-client inside container"
	@echo "  make logs EXP=<profile>     Follow container logs"
	@echo "  make down EXP=<profile>     Stop and wipe volume for target profile"
	@echo "  make clean                  Stop all profiles and wipe all lab volumes"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - parts-limit    (Standard engine, testing un-batched inserts & parts explosion)"
	@echo "  - memory-cap     (max_memory_usage=64MB, testing analytical memory saturation & disk spill)"
	@echo "  - compression    (Columnar codecs NONE vs LZ4 vs Delta+ZSTD & LowCardinality)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	docker compose exec -T $(EXP) clickhouse-client --query "DROP TABLE IF EXISTS parts_test; DROP TABLE IF EXISTS clickstream_events;"
	@echo "==> ClickHouse experiment tables dropped."

run:
	uv run experiments/$(SCRIPT).py

client:
	docker compose exec -it $(EXP) clickhouse-client

logs:
	docker compose logs -f $(EXP)

VERIFY_CASES = \
	parts-limit:00_crash_course,01_parts_explosion \
	memory-cap:02_memory_limit \
	compression:03_codec_compression

verify:
	@set -e; \
	for spec in $(VERIFY_CASES); do \
		p=$${spec%%:*}; scripts=$${spec#*:}; \
		echo "==> Verifying clickhouse [$$p] -> $$scripts"; \
		$(MAKE) up EXP=$$p; \
		rc=0; \
		for s in $$(echo "$$scripts" | tr ',' ' '); do \
			echo "--> Running $$s (profile=$$p)"; \
			$(MAKE) run EXP=$$p SCRIPT=$$s || { rc=$$?; break; }; \
		done; \
		$(MAKE) down EXP=$$p || true; \
		[ $$rc -eq 0 ] || exit $$rc; \
	done

# Makefile for Apache Kafka (KRaft) Limits Lab
EXP ?= single-broker
SCRIPT ?= 00_crash_course

.PHONY: help up down clean soft-reset run logs verify

help:
	@echo "Kafka (KRaft) Experiments Harness"
	@echo "================================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start container(s) for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
	@echo "  make verify                 Run end-to-end verification across all profiles"
	@echo "  make soft-reset             Delete all user topics in-engine (sub-second reset)"
	@echo "  make down EXP=<profile>     Stop and wipe volume(s) for specific profile"
	@echo "  make clean                  Stop all profiles and wipe all lab volumes"
	@echo "  make logs EXP=<profile>     Follow container logs"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - single-broker (1 KRaft node, 512MB RAM - Exp 00, 01, 02, 03)"
	@echo "  - multi-broker  (3 KRaft nodes, 384MB RAM each - Exp 04 ISR & failover)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	@uv run -q --with confluent-kafka python -c 'from confluent_kafka.admin import AdminClient; \
	[AdminClient({"bootstrap.servers": bs}).delete_topics([t for t in AdminClient({"bootstrap.servers": bs}).list_topics(timeout=2).topics if not t.startswith("__")], operation_timeout=5) \
	for bs in ("127.0.0.1:9092", "127.0.0.1:19092") if True]' 2>/dev/null || true
	@echo "==> Kafka user topics deleted."

run:
	uv run experiments/$(SCRIPT).py

logs:
	docker compose --profile $(EXP) logs -f

VERIFY_CASES = \
	single-broker:00_crash_course,01_rebalance_storms,02_offset_commit_pitfalls,03_partition_skew_and_lag \
	multi-broker:04_acks_and_insync_replicas

verify:
	@set -e; \
	for spec in $(VERIFY_CASES); do \
		p=$${spec%%:*}; scripts=$${spec#*:}; \
		echo "==> Verifying kafka [$$p] -> $$scripts"; \
		$(MAKE) up EXP=$$p; \
		rc=0; \
		for s in $$(echo "$$scripts" | tr ',' ' '); do \
			echo "--> Running $$s (profile=$$p)"; \
			$(MAKE) run EXP=$$p SCRIPT=$$s || { rc=$$?; break; }; \
		done; \
		$(MAKE) down EXP=$$p || true; \
		[ $$rc -eq 0 ] || exit $$rc; \
	done

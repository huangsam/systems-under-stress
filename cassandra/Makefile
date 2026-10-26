# Makefile for Cassandra Operational Limits Lab
EXP ?= tombstones
SCRIPT ?= 01_tombstone_threshold

.PHONY: help up down clean soft-reset run cqlsh nodetool logs

help:
	@echo "Cassandra Operational Limits Lab Harness"
	@echo "========================================"
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start container(s) for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run Python test script via 'uv run experiments/<name>.py'"
	@echo "  make soft-reset             Truncate lab tables (fast reset without cold-boot penalty)"
	@echo "  make down EXP=<profile>     Stop and wipe volumes for specific profile"
	@echo "  make clean                  Stop all profiles and wipe all lab volumes"
	@echo "  make cqlsh                  Open interactive cqlsh shell"
	@echo "  make nodetool CMD=<cmd>     Run nodetool command (default: status)"
	@echo "  make logs EXP=<profile>     Follow container logs"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - tombstones           (Single-node: 768M RAM, 512M heap, 10,000 tombstone threshold)"
	@echo "  - cluster-consistency  (3-node cluster: RF=3, PACELC ONE vs QUORUM vs ALL)"
	@echo "  - wide-partitions      (Single-node: wide partition hot-spotting test)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	@echo "==> Executing in-engine soft reset (TRUNCATE)..."
	docker compose exec -T cassandra-tombstones cqlsh -e "TRUNCATE lab_tombstones.sensor_telemetry;" 2>/dev/null || \
	docker compose exec -T cassandra-node1 cqlsh -e "TRUNCATE lab_consistency.accounts;" 2>/dev/null || \
	cqlsh 127.0.0.1 9042 -e "TRUNCATE lab_tombstones.sensor_telemetry;" 2>/dev/null || \
	@echo "==> Soft reset completed (tables truncated)."

cqlsh:
	docker compose exec -it cassandra-tombstones cqlsh 2>/dev/null || \
	docker compose exec -it cassandra-node1 cqlsh 2>/dev/null || \
	cqlsh 127.0.0.1 9042

nodetool:
	docker compose exec -it cassandra-tombstones nodetool $${CMD:-status} 2>/dev/null || \
	docker compose exec -it cassandra-node1 nodetool $${CMD:-status} 2>/dev/null || \
	nodetool $${CMD:-status}

run:
	CASS_DRIVER_NO_EXTENSIONS=1 uv run --python 3.11 experiments/$(SCRIPT).py

logs:
	docker compose --profile $(EXP) logs -f

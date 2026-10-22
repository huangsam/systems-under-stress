# Makefile for Redis Experiments
# Default profile is eviction-lru
EXP ?= eviction-lru
SCRIPT ?= 01_eviction

.PHONY: help up down clean soft-reset run monitor logs

help:
	@echo "Redis Experiments Harness"
	@echo "========================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start container for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
	@echo "  make monitor                Stream live Redis memory and eviction metrics"
	@echo "  make soft-reset             Flush all keys in-memory (sub-second reset)"
	@echo "  make down EXP=<profile>     Stop and wipe volume for specific profile"
	@echo "  make clean                  Stop all profiles and wipe all lab volumes"
	@echo "  make logs EXP=<profile>     Follow container logs"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - eviction-lru         (16MB maxmemory, allkeys-lru)"
	@echo "  - eviction-noeviction  (16MB maxmemory, noeviction - tests OOM limit)"
	@echo "  - eviction-lfu         (16MB maxmemory, allkeys-lfu)"
	@echo "  - stampede             (64MB maxmemory)"
	@echo "  - persistence-rdb      (RDB snapshot only - tests crash data loss)"
	@echo "  - persistence-aof      (AOF everysec - up to 1s data loss on crash)"
	@echo "  - persistence-always   (AOF always - 0 data loss, synchronous fsync)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	docker compose exec -T $(EXP) redis-cli FLUSHALL || uv run -s -c 'import redis; redis.Redis(host="127.0.0.1", port=6379).flushall()'
	@echo "==> Redis keys flushed."

run:
	uv run experiments/$(SCRIPT).py

run-kt:
	kotlin experiments/$(SCRIPT).main.kts

monitor:
	uv run experiments/monitor.py

logs:
	docker compose logs -f $(EXP)

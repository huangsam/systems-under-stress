# Makefile for Linux Kernel Primitives & Container Limits Experiments
# Default profile is pid-limit
EXP ?= pid-limit
SCRIPT ?= 01_fork_bomb_cgroup

.PHONY: help up down clean run sh logs ps

help:
	@echo "Linux Kernel & Container Limits Lab"
	@echo "==================================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>                 Start container for profile with cgroup limits"
	@echo "  make run EXP=<profile> SCRIPT=<name>  Run experiment script inside container"
	@echo "  make sh EXP=<profile>                 Open interactive shell inside container"
	@echo "  make logs EXP=<profile>               Follow container logs"
	@echo "  make down EXP=<profile>               Stop and remove container for profile"
	@echo "  make clean                            Stop all profiles and wipe any lab containers"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - pid-limit       (pids.limit: 40, memory: 64M, cpus: 0.5)"
	@echo "  - fd-exhaustion   (ulimit -n 64, memory: 64M, cpus: 0.5)"
	@echo "  - oom-killer      (memory: 32M, memswap: 32M - no swap)"
	@echo ""
	@echo "Experiments:"
	@echo "  - 01_fork_bomb_cgroup   (cgroups v2 pids.max ceiling & fork exhaustion)"
	@echo "  - 02_fd_limits          (ulimit -n 64 & /proc/self/fd inspection)"
	@echo "  - 03_oom_killer         (4MB chunks under 32M ceiling, OOM SIGKILL / exit 137)"

up:
	docker compose --profile $(EXP) up -d --build --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

sh:
	docker compose --profile $(EXP) exec $(EXP) /bin/bash

run:
	docker compose --profile $(EXP) exec -T $(EXP) uv run /lab/experiments/$(SCRIPT).py

logs:
	docker compose --profile $(EXP) logs -f $(EXP)

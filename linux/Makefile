# Makefile for Linux Kernel Primitives & Container Limits Experiments
# Select the matching container profile from SCRIPT unless EXP is explicit.
SCRIPT ?= 01_fork_bomb_cgroup
PROFILE_00_crash_course = pid-limit
PROFILE_01_fork_bomb_cgroup = pid-limit
PROFILE_02_fd_limits = fd-exhaustion
PROFILE_03_oom_killer = oom-killer
EXP ?= $(or $(PROFILE_$(SCRIPT)),pid-limit)

.PHONY: help up down clean run sh logs ps verify

help:
	@echo "Linux Kernel & Container Limits Lab"
	@echo "==================================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>                 Start container for profile with cgroup limits"
	@echo "  make run SCRIPT=<name>                Run script in its matching profile container"
	@echo "  make run EXP=<profile> SCRIPT=<name>  Override the script's matching profile"
	@echo "  make verify                           Run end-to-end verification across all profiles"
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
	@echo "  - 00_crash_course       (kernel runtime, /proc/self/status, POSIX limits & cgroups v2)"
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
	docker compose --profile $(EXP) exec -T $(EXP) python3 /lab/experiments/$(SCRIPT).py

logs:
	docker compose --profile $(EXP) logs -f $(EXP)

VERIFY_CASES = \
	pid-limit:00_crash_course,01_fork_bomb_cgroup \
	fd-exhaustion:02_fd_limits \
	oom-killer:03_oom_killer

verify:
	@set -e; \
	for spec in $(VERIFY_CASES); do \
		p=$${spec%%:*}; scripts=$${spec#*:}; \
		echo "==> Verifying linux [$$p] -> $$scripts"; \
		$(MAKE) up EXP=$$p; \
		rc=0; \
		for s in $$(echo "$$scripts" | tr ',' ' '); do \
			echo "--> Running $$s (profile=$$p)"; \
			$(MAKE) run EXP=$$p SCRIPT=$$s || { rc=$$?; break; }; \
		done; \
		$(MAKE) down EXP=$$p || true; \
		[ $$rc -eq 0 ] || exit $$rc; \
	done

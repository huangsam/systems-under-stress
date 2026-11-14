# Makefile for HashiCorp Vault Limits Lab
EXP ?= dynamic-creds
SCRIPT ?= 01_dynamic_db_creds
CMD ?= status

ifeq ($(EXP),token-lifecycle)
SERVICE = vault-token
else ifeq ($(EXP),transit-crypto)
SERVICE = vault-transit
else
SERVICE = vault-dynamic
endif

.PHONY: help up down clean run vault-cli logs verify

help:
	@echo "HashiCorp Vault Experiments Harness"
	@echo "==================================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start containers for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
	@echo "  make verify                 Run end-to-end verification across all profiles"
	@echo "  make down EXP=<profile>     Stop and wipe volume for specific profile"
	@echo "  make clean                  Stop all profiles and wipe all lab volumes"
	@echo "  make vault-cli CMD=<args>   Run vault CLI inside the active container (default: status)"
	@echo "  make logs                   Follow logs for running containers"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - dynamic-creds   (Vault + Postgres for dynamic DB creds & lease revocation)"
	@echo "  - token-lifecycle (Vault standalone for token TTL & max_ttl boundary)"
	@echo "  - transit-crypto  (Vault Transit EaaS throughput & zero-downtime key rotation)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

run:
	uv run experiments/$(SCRIPT).py

vault-cli:
	docker compose exec -it $(SERVICE) vault $(CMD)

logs:
	docker compose logs -f

VERIFY_CASES = \
	dynamic-creds:00_crash_course,01_dynamic_db_creds \
	token-lifecycle:02_token_renewal \
	transit-crypto:03_transit_throughput

verify:
	@set -e; \
	for spec in $(VERIFY_CASES); do \
		p=$${spec%%:*}; scripts=$${spec#*:}; \
		echo "==> Verifying vault [$$p] -> $$scripts"; \
		$(MAKE) up EXP=$$p; \
		rc=0; \
		for s in $$(echo "$$scripts" | tr ',' ' '); do \
			echo "--> Running $$s (profile=$$p)"; \
			$(MAKE) run EXP=$$p SCRIPT=$$s || { rc=$$?; break; }; \
		done; \
		$(MAKE) down EXP=$$p || true; \
		[ $$rc -eq 0 ] || exit $$rc; \
	done

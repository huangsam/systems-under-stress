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

.PHONY: help up down clean run vault-cli logs

help:
	@echo "HashiCorp Vault Experiments Harness"
	@echo "==================================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start containers for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
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

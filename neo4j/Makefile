# Makefile for Neo4j Labs
EXP ?= traversal-depth
SCRIPT ?= 01_unbounded_path

.PHONY: help up down clean soft-reset run cypher-shell logs

help:
	@echo "Neo4j Experiments Harness"
	@echo "========================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start container for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
	@echo "  make soft-reset EXP=<prof>  Wipe all nodes and relationships (sub-second reset)"
	@echo "  make down EXP=<profile>     Stop and wipe volume for specific profile"
	@echo "  make clean                  Stop all profiles and wipe all lab volumes"
	@echo "  make cypher-shell EXP=<prof> Open interactive cypher-shell inside container"
	@echo "  make logs EXP=<profile>     Follow container logs"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - traversal-depth    (Unbounded path combinatorial explosion vs shortestPath)"
	@echo "  - supernode          (Dense relationship pointer chasing & lock contention)"
	@echo "  - compare-sql        (Neo4j + Postgres: Multi-hop Cypher vs WITH RECURSIVE SQL)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	docker compose exec -T $(EXP) cypher-shell -u neo4j -p password123 "MATCH (n) DETACH DELETE n;"
	@echo "==> Neo4j database wiped clean via DETACH DELETE."

run:
	uv run experiments/$(SCRIPT).py

cypher-shell:
	docker compose exec -it $(EXP) cypher-shell -u neo4j -p password123

logs:
	docker compose logs -f $(EXP)

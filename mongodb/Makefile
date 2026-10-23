# Makefile for MongoDB Limits Lab
EXP ?= unindexed-sort
SCRIPT ?= 01_sort_memory_limit

.PHONY: help up down clean soft-reset run run-kt mongosh logs

help:
	@echo "MongoDB Experiments Harness"
	@echo "==========================="
	@echo "Usage:"
	@echo "  make up EXP=<profile>       Start container for profile and wait until healthy"
	@echo "  make run SCRIPT=<name>      Run test script via 'uv run experiments/<name>.py'"
	@echo "  make soft-reset             Drop 'lab_db' database for fast reset without restarting"
	@echo "  make down EXP=<profile>     Stop and wipe volume for specific profile"
	@echo "  make clean                  Stop all profiles and wipe all lab volumes"
	@echo "  make mongosh EXP=<profile>  Open interactive mongosh terminal inside container"
	@echo "  make logs EXP=<profile>     Follow container logs"
	@echo ""
	@echo "Available Profiles:"
	@echo "  - unindexed-sort  (Tests the 100MB RAM sort limit vs indexed & allowDiskUse)"
	@echo "  - durability      (Tests write durability w:0, w:1, j:True across container SIGKILL)"
	@echo "  - document-limits (Tests unbounded array growth hitting the 16MB BSON document cap)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	docker compose exec -T $(EXP) mongosh lab_db --eval 'db.dropDatabase()' || uv run -s -c 'from pymongo import MongoClient; MongoClient("mongodb://127.0.0.1:27017/").drop_database("lab_db")'
	@echo "==> MongoDB 'lab_db' dropped."

run:
	uv run experiments/$(SCRIPT).py

run-kt:
	kotlin experiments/$(SCRIPT).main.kts

mongosh:
	docker compose exec -it $(EXP) mongosh lab_db

logs:
	docker compose logs -f $(EXP)

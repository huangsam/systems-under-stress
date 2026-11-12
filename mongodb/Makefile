# Makefile for MongoDB Limits Lab
EXP ?= unindexed-sort
SCRIPT ?= 01_sort_memory_limit

.PHONY: help up down clean soft-reset run mongosh logs

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
	@echo "  - replica-set     (3-node cluster: election failover, w:majority durability, oplog exhaustion)"

up:
	docker compose --profile $(EXP) up -d --wait

down:
	docker compose --profile $(EXP) down -v

clean:
	docker compose --profile "*" down -v

soft-reset:
	@uv run -q --with pymongo python -c 'import pymongo; c = pymongo.MongoClient("mongodb://127.0.0.1:27017,127.0.0.1:27018,127.0.0.1:27019/?replicaSet=rs0&serverSelectionTimeoutMS=1000"); c.drop_database("lab_db")' 2>/dev/null || \
	docker compose exec -T $(EXP) mongosh lab_db --eval 'db.dropDatabase()' 2>/dev/null || \
	uv run -q --with pymongo python -c 'import pymongo; pymongo.MongoClient("mongodb://127.0.0.1:27017/?serverSelectionTimeoutMS=1000").drop_database("lab_db")' 2>/dev/null || true
	@echo "==> MongoDB 'lab_db' dropped."

run:
	uv run experiments/$(SCRIPT).py

mongosh:
	docker compose exec -it $(EXP) mongosh lab_db

logs:
	docker compose logs -f $(EXP)

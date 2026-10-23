# Systems Under Stress

[![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/huangsam/systems-under-stress/ci.yml)](https://github.com/huangsam/systems-under-stress/actions)
[![License](https://img.shields.io/github/license/huangsam/systems-under-stress)](https://github.com/huangsam/systems-under-stress/blob/main/LICENSE)

A hands-on, empirical systems engineering lab suite exploring the **hard operational boundaries, failure modes, and internal mechanics** of 8 foundational infrastructure technologies using Docker containers and Python/Kotlin workloads.

> **Philosophy**: Instead of generating terabytes of data to observe how systems break, we **squeeze the container and engine resource limits** (`mem_limit`, `pids.max`, `work_mem`, `maxmemory`, `max_memory_usage`) so we can deterministically reproduce production failure signatures - OOM kills, cache stampedes, write skew, tombstone aborts, and parts explosions - in seconds on a laptop.

---

## Lab Matrix (24 Reproducible Experiments Across 8 Engines)

| Engine | Experiments | Primary Failure Signatures Reproduced |
| :--- | :--- | :--- |
| [**Redis**](./redis/) | Eviction (`maxmemory`), Cache Stampede, Crash Durability | `OOM command not allowed`, Thundering herd (100x), `SIGKILL` data loss |
| [**PostgreSQL**](./postgres/) | Isolation Levels, Row Lock Contention, Sort `work_mem` | Write skew under `REPEATABLE READ`, Deadlock `40P01`, Disk spill |
| [**ClickHouse**](./clickhouse/) | Parts Explosion, Analytical Memory Cap, Columnar Codecs | `Code: 252 TOO_MANY_PARTS`, `Code: 241 MEMORY_LIMIT_EXCEEDED` |
| [**MongoDB**](./mongodb/) | In-Memory Sort Limit, Write Concern Crash, Document Size | Sort exceeded 100MB (Code 96), Unjournaled crash loss, 16MB BSON cap |
| [**Cassandra**](./cassandra/) | Tombstone Threshold, PACELC Consistency, Partition Skew | `ReadFailureException` (10k tombstones), Quorum node loss, Partition GC |
| [**Neo4j**](./neo4j/) | Unbounded Traversal, Supernodes, Cypher vs Recursive SQL | $\mathcal{O}(b^d)$ path explosion, 50k-edge lock contention, Recursive joins |
| [**Vault**](./vault/) | Dynamic DB Credentials, Token Leases, Transit EaaS | Synchronous `DROP ROLE` cascade, `max_ttl` expiration, Crypto HTTP batching |
| [**Linux**](./linux/) | Fork Bomb (`pids.max`), File Descriptors, OOM Killer | `EAGAIN` (`pids.max: 40`), `EMFILE` fd exhaustion, Exit Code 137 (`SIGKILL`) |

---

## Quick Start (Universal CLI Workflow)

Every lab directory shares the exact same `Makefile` interface:

```bash
cd <technology>   # e.g., cd redis, cd postgres, cd clickhouse

# 1. See available profiles and experiments for this engine
make help

# 2. Spin up the target experiment profile and wait until healthy
make up EXP=<profile>

# 3. Execute the self-driving workload script (Dual Runner Architecture)
make run SCRIPT=<script_name>       # Python runner via uv (PEP 723 inline dependencies)
make run-kt SCRIPT=<script_name>    # Kotlin runner via kotlin (.main.kts JVM script)

# 4. Fast in-memory reset (FLUSHALL / TRUNCATE / DROP) without container restart
make soft-reset

# 5. Tear down all profiles and wipe all named volumes for a 100% cold reset
make clean
```

### Dual Runner Architecture (Python & Kotlin)

Every database lab (`redis`, `postgres`, `clickhouse`, `mongodb`, `cassandra`, `neo4j`, `vault`) supports dual first-class implementations:
1. **Python (`.py`) via `uv`**: Zero-overhead execution with inline PEP 723 metadata.
2. **Kotlin Script (`.main.kts`) via `kotlin`**: Native JVM ecosystem execution utilizing `@file:DependsOn` resolution with mature official drivers (DataStax 4.x Cassandra, Jedis 5.x Redis, PostgreSQL JDBC 42.7, ClickHouse JDBC 0.6, MongoDB Sync 5.1, Neo4j Bolt 5.20, and Vault HTTP API).

> **Note on `linux/` Engine**: The Linux Kernel lab deliberately remains pure Python because its workloads execute directly inside tightly restricted 32MB cgroup micro-containers (`mem_limit: 32m`, `pids.max: 40`) where the JVM cannot boot.

---

## Prerequisites

- **Docker** (Docker Desktop, OrbStack, or Colima) with Compose v2
- **[`uv`](https://docs.astral.sh/uv/)** (`brew install uv`) for zero-setup Python script execution
- **[`kotlin`](https://kotlinlang.org/)** (`brew install kotlin`) for optional Kotlin `.main.kts` dual execution (JDK 21+)

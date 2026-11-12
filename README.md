# Systems Under Stress

[![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/huangsam/systems-under-stress/ci.yml)](https://github.com/huangsam/systems-under-stress/actions)
[![License](https://img.shields.io/github/license/huangsam/systems-under-stress)](https://github.com/huangsam/systems-under-stress/blob/main/LICENSE)

A hands-on, empirical systems engineering lab suite exploring the **hard operational boundaries, failure modes, and internal mechanics** of 9 foundational infrastructure technologies using Docker containers and Python workloads.

> **Philosophy**: Instead of generating terabytes of data to observe how systems break, we **squeeze the container and engine resource limits** (`deploy.resources.limits`, `pids.max`, `work_mem`, `maxmemory`, `max_memory_usage`) so we can deterministically reproduce production failure signatures - OOM kills, cache stampedes, write skew, tombstone aborts, and parts explosions - in seconds on a laptop.

---

## Lab Matrix (38 Reproducible Scripts Across 9 Labs: 9 Crash Courses + 29 Stress Experiments)

| Engine / Lab | Experiments | Primary Failure Signatures Reproduced |
| :--- | :--- | :--- |
| [**Redis**](./redis/) | Crash Course, Eviction (`maxmemory`), Cache Stampede, Crash Durability | `OOM command not allowed`, Thundering herd (100x), `SIGKILL` data loss |
| [**PostgreSQL**](./postgres/) | Crash Course, Isolation Levels, Row Locks, Sort `work_mem`, Streaming Replication | Write skew (`REPEATABLE READ`), Deadlock `40P01`, Disk spill, Standby query conflict `40001` |
| [**ClickHouse**](./clickhouse/) | Crash Course, Parts Explosion, Analytical Memory Cap, Columnar Codecs | `Code: 252 TOO_MANY_PARTS`, `Code: 241 MEMORY_LIMIT_EXCEEDED` |
| [**MongoDB**](./mongodb/) | Crash Course, In-Memory Sort Limit, Write Concern Crash, Document Size, Replica Set Elections | Sort exceeded 100MB (Code 96), Unjournaled crash loss, 16MB BSON cap, Stepdown failover loss, Oplog exhaustion (`RECOVERING`) |
| [**Cassandra**](./cassandra/) | Crash Course, Tombstone Threshold, PACELC Consistency, Partition Skew | `ReadFailureException` (10k tombstones), Quorum node loss, Partition GC |
| [**Neo4j**](./neo4j/) | Crash Course, Unbounded Traversal, Supernodes, Cypher vs Recursive SQL | $\mathcal{O}(b^d)$ path explosion, 50k-edge lock contention, Recursive joins |
| [**Vault**](./vault/) | Crash Course, Dynamic DB Credentials, Token Leases, Transit EaaS | Synchronous `DROP ROLE` cascade, `max_ttl` expiration, Crypto HTTP batching |
| [**Linux**](./linux/) | Crash Course, Fork Bomb (`pids.max`), File Descriptors, OOM Killer | `EAGAIN` (`pids.max: 40`), `EMFILE` fd exhaustion, Exit Code 137 (`SIGKILL`) |
| [**Chaos / Toxiproxy**](./chaos/) | Crash Course, Latency Injection, Connection Pool Starvation, Silent Drops | Pool timeout (`No connection available`), Retry storms, Half-open TCP hangs |

---

## Quick Start (Universal CLI Workflow)

Every lab directory shares a common `Makefile` interface (`make help` lists the exact targets and profiles per lab; see [`AGENTS.md`](./AGENTS.md#3-per-lab-exceptions) for lab-specific execution notes):

```bash
cd <technology>   # e.g., cd redis, cd postgres, cd clickhouse

# 1. See available profiles and experiments for this engine
make help

# 2. Spin up the target experiment profile and wait until healthy
make up EXP=<profile>

# 3. Execute the baseline crash course or a self-driving stress workload
make run SCRIPT=00_crash_course
make run SCRIPT=<script_name>

# 4. Fast in-engine reset (FLUSHALL / TRUNCATE / DROP) without container restart (stateful labs)
make soft-reset

# 5. Tear down a specific profile or wipe all profiles and named volumes (100% cold reset)
make down EXP=<profile>
make clean
```

---

## Prerequisites

- **Docker** (Docker Desktop, OrbStack, or Colima) with Compose v2
- **[`uv`](https://docs.astral.sh/uv/)** (`brew install uv`) for zero-setup Python script execution

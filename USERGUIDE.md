# Systems Under Stress User Guide

This guide explains how to use `systems-under-stress` as an interactive **failure-mode flight simulator** for mastering database internals, distributed systems trade-offs, and production debugging.

---

## 1. The Study Loop: Predict → Break → Explain → Mitigate

Avoid running `make run` passively. Before executing an experiment, work through these four steps:

1. **Predict (2 min):** Open the lab's `README.md` and read only `### 1. Hypothesis & Objective` and `### 2. Configuration & Limits` for the experiment. Write down the error code, bottleneck, or metric shift you expect.
2. **Break:** Spin up the profile and run the script:
   ```bash
   cd <lab>
   make up EXP=<profile>
   make run SCRIPT=<script_name>
   ```
   *(In [`redis/`](./redis/), open a second terminal and run `make monitor` to watch memory fragmentation and key evictions in real time.)*
3. **Explain Out Loud (60 sec):** Explain *why* it failed and *why* the default configuration didn't save it, using the underlying mechanism (e.g., MVCC snapshot isolation, `work_mem` disk spill, `pids.max` returning `EAGAIN`).
4. **Mitigate:** Check `### 4. Observed Metrics & Failure Signatures` and `### 5. Production & Systems Takeaways` in the lab `README.md`, and inspect the recovery phase of the script.

> **Tip:** Use `make soft-reset` between runs of the same profile for sub-second state resets without restarting containers (stateful labs), and `make clean` when switching labs to reclaim Docker volumes.

---

## 2. Learning Tracks by Failure Domain

Rather than studying one engine in isolation, group experiments by the failure domain they expose:

### Track A: Caching, Backpressure & Gray Failures
*Focus: Backend resilience, cache design, and cascading failure prevention*

- **Redis:**
  - [`01_eviction.py`](./redis/experiments/01_eviction.py) — `noeviction` OOM (`OOM command not allowed`) vs LRU eviction under `maxmemory`
  - [`02_stampede.py`](./redis/experiments/02_stampede.py) — Cache stampede / thundering herd (100x backend load) and singleflight mitigation
  - [`03_persistence.py`](./redis/experiments/03_persistence.py) — RDB/AOF durability trade-offs vs `SIGKILL` data loss
- **Toxiproxy (Chaos):**
  - [`01_pool_starvation.py`](./chaos/experiments/01_pool_starvation.py) — Slow downstream latency starving fixed connection pools (`No connection available`)
  - [`02_retry_storms_vs_backoff.py`](./chaos/experiments/02_retry_storms_vs_backoff.py) — Naive retries amplifying outages vs exponential backoff with jitter
  - [`03_half_open_tcp_hangs.py`](./chaos/experiments/03_half_open_tcp_hangs.py) — Silent packet drops and why client timeouts are mandatory

### Track B: Distributed Data, Consistency & Replication
*Focus: Transaction isolation, consensus, and failover behavior*

- **PostgreSQL:**
  - [`01_isolation.py`](./postgres/experiments/01_isolation.py) — Write skew under `REPEATABLE READ` vs `SERIALIZABLE` (SSI)
  - [`02_locks.py`](./postgres/experiments/02_locks.py) — Row lock contention and deadlock detection (`40P01`)
  - [`04_replication_conflicts.py`](./postgres/experiments/04_replication_conflicts.py) — Streaming replication standby query conflicts (`40001`)
- **Cassandra:**
  - [`02_consistency_levels.py`](./cassandra/experiments/02_consistency_levels.py) — PACELC trade-offs and `QUORUM` behavior under node loss
- **MongoDB:**
  - [`02_write_concern_crash.py`](./mongodb/experiments/02_write_concern_crash.py) — Unjournaled `w:1` crash data loss vs `w:majority`
  - [`04_replica_set_elections.py`](./mongodb/experiments/04_replica_set_elections.py) — Primary stepdown failover loss and oplog exhaustion (`RECOVERING`)

### Track C: Storage Engines, Query Execution & Data Modeling
*Focus: How on-disk layouts and query planners fail under load*

- **PostgreSQL:** [`03_work_mem.py`](./postgres/experiments/03_work_mem.py) — Sort `work_mem` exhaustion and temp disk spill
- **ClickHouse:**
  - [`01_parts_explosion.py`](./clickhouse/experiments/01_parts_explosion.py) — Unbatched inserts triggering `Code: 252 TOO_MANY_PARTS`
  - [`02_memory_limit.py`](./clickhouse/experiments/02_memory_limit.py) — Analytical `GROUP BY` hitting `Code: 241 MEMORY_LIMIT_EXCEEDED`
  - [`03_codec_compression.py`](./clickhouse/experiments/03_codec_compression.py) — Columnar codecs (`Delta`, `Gorilla`, `ZSTD`) vs raw storage
- **Cassandra:**
  - [`01_tombstone_threshold.py`](./cassandra/experiments/01_tombstone_threshold.py) — Delete-heavy workloads hitting `ReadFailureException` (10k tombstones)
  - [`03_partition_skew.py`](./cassandra/experiments/03_partition_skew.py) — Unbounded partition growth, hot spots, and GC pressure
- **MongoDB:**
  - [`01_sort_memory_limit.py`](./mongodb/experiments/01_sort_memory_limit.py) — Unindexed in-memory sort exceeding 100MB (Code 96)
  - [`03_document_size_cap.py`](./mongodb/experiments/03_document_size_cap.py) — Unbounded embedded arrays hitting the 16MB BSON cap
- **Neo4j:**
  - [`01_unbounded_path.py`](./neo4j/experiments/01_unbounded_path.py) — $\mathcal{O}(b^d)$ path explosion on unbounded traversals
  - [`02_supernode.py`](./neo4j/experiments/02_supernode.py) — 50k-edge supernode lock contention
  - [`03_cypher_vs_recursive_sql.py`](./neo4j/experiments/03_cypher_vs_recursive_sql.py) — Graph traversal vs recursive SQL joins

### Track D: OS Limits, Container Isolation & Secrets Infrastructure
*Focus: Kernel resource caps, production diagnostics, and credential lifecycles*

- **Linux:**
  - [`01_fork_bomb_cgroup.py`](./linux/experiments/01_fork_bomb_cgroup.py) — Process exhaustion hitting `pids.max` (`EAGAIN`)
  - [`02_fd_limits.py`](./linux/experiments/02_fd_limits.py) — File descriptor leaks hitting `EMFILE`
  - [`03_oom_killer.py`](./linux/experiments/03_oom_killer.py) — Cgroup memory exhaustion and Exit Code 137 (`SIGKILL`)
- **Vault:**
  - [`01_dynamic_db_creds.py`](./vault/experiments/01_dynamic_db_creds.py) — Synchronous `DROP ROLE` cascade on lease revocation
  - [`02_token_renewal.py`](./vault/experiments/02_token_renewal.py) — Lease renewal hitting `max_ttl` expiration
  - [`03_transit_throughput.py`](./vault/experiments/03_transit_throughput.py) — Encryption-as-a-Service HTTP overhead vs batching

---

## 3. Distilling Experiments into Mental Models

Summarize each completed lab in ~45 seconds using the **Constraint → Failure Signature → Root Cause → Mitigation** pattern:

- **Chaos / Latency:** *"When a downstream service degrades with high latency instead of failing fast, fixed-size connection pools starve (`No connection available`), and naive retries create a retry storm. The fix is strict client timeouts, bounded pools, and exponential backoff with jitter."*
- **Postgres Isolation:** *"Under `REPEATABLE READ`, Postgres uses MVCC snapshots, which prevents dirty and non-repeatable reads but still allows write skew across two rows. Preventing that requires `SERIALIZABLE` (SSI) or explicit `SELECT ... FOR UPDATE`."*

---

## 4. Suggested 2-Week Cadence (30–45 min/day)

| Days | Focus | Labs |
| :--- | :--- | :--- |
| **1–3** | Track A: Caching & Gray Failures | `redis/`, `chaos/` |
| **4–6** | Track B: Consistency & Replication | `postgres/`, `mongodb/`, `cassandra/` (`02`) |
| **7–9** | Track C: Storage & Query Execution | `clickhouse/`, `cassandra/` (`01`, `03`), `neo4j/` |
| **10–11** | Track D: OS Limits & Secrets Infra | `linux/`, `vault/` |
| **12–14** | **Recall Check** | Pick 5 random experiments from [`README.md`](./README.md); write down the failure signature, root cause, and fix in 3 minutes each without looking at the code. Re-run any you miss. |

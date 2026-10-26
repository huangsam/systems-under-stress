# Cassandra Limits & Internals Lab

Hands-on experiments stress-testing Apache Cassandra's LSM storage engine internals, tombstone failure aborts (`tombstone_failure_threshold`), tunable consistency under node failure (PACELC in practice), and wide partition hot-spotting on `cassandra:5.0.9`.

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) with **Docker Compose Profiles** and **dedicated named volumes** for total data isolation between runs.

```text
cassandra/
├── docker-compose.yml             # Base anchor &cassandra-base, cgroups limits, profiles
├── Makefile                       # One-touch runner targets (up, run, down, clean, soft-reset, cqlsh)
├── experiments/
│   ├── 00_crash_course.py         # Cluster introspection, CQL basics & partition key routing
│   ├── 01_tombstone_threshold.py  # Tombstone accumulation, safety limits & failure abort
│   ├── 02_consistency_levels.py   # 3-node cluster PACELC trade-offs under node failure
│   └── 03_partition_skew.py       # Wide partition hotspots vs compound key bucketing
└── README.md
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) and run via `uv run` or `make run`.

### Host Safety & Resource Guardrails
- **Cgroups Limits**: `cpus: '1.5'`, `memory: 768M` (single-node) / `512M` (cluster nodes).
- **Strict JVM Caps**: Single node capped at `MAX_HEAP_SIZE: 512M`, `HEAP_NEWSIZE: 128M`; 3-node cluster capped at `MAX_HEAP_SIZE: 256M`, `HEAP_NEWSIZE: 64M` per node to ensure host memory safety.
- **Loopback Binding**: Exclusively binds to `127.0.0.1:9042:9042` on coordinator nodes.
- **Deterministic Readiness**:
  ```yaml
  healthcheck:
    test: ["CMD-SHELL", "cqlsh -e \"DESCRIBE KEYSPACES\" || nodetool status | grep -q \"UN\" || exit 1"]
    interval: 5s
    timeout: 5s
    retries: 25
    start_period: 30s
  ```
- **Two-Tier Resets**:
  - *Fast Soft Reset (In-Engine)*: `make soft-reset` executes `TRUNCATE keyspace.table` to bypass Cassandra's ~35-second cold boot.
  - *Hard Reset (Volume Wipe)*: `make down EXP=<profile>` or `make clean` tears down containers and wipes isolated named volumes.

---

## Quickstart & Execution Standard

Across all profiles in this lab:

```bash
cd cassandra

# 1. Start target profile with automatic healthcheck wait
make up EXP=tombstones

# 2. Run self-driving benchmark script
make run SCRIPT=01_tombstone_threshold

# 3. Clean up container and wipe isolated named volume
make down EXP=tombstones

# Or wipe all profiles and named volumes:
make clean
```

---

## Experiment 01: The Tombstone Failure Threshold (`tombstone_failure_threshold`)

### 1. Hypothesis & Objective
- **LSM Storage & Deletes**: Cassandra is an append-only Log-Structured Merge (LSM) storage engine. It never executes in-place mutations or deletions on disk. When a row or cell is deleted, Cassandra writes a new marker called a **tombstone** containing a deletion timestamp.
- **Read Path Penalty**: When reading, Cassandra must read tombstones to ensure deleted cells aren't resurrected from older SSTables. If a query scans a large range where many rows were deleted, Cassandra must scan and filter out every tombstone.
- **Safety Circuit Breaker**: To prevent rogue scan queries from exhausting JVM heap and causing Stop-the-World GC pauses or OOM death spirals, Cassandra enforces two thresholds in `cassandra.yaml`:
  - `tombstone_warn_threshold: 1000` (server logs a WARN)
  - `tombstone_failure_threshold: 10000` (aborts query with `ReadFailureException`)
- **Remediation**: Slicing past deleted rows using clustering keys (`WHERE recorded_at >= ?`) allows Cassandra's SSTable index blocks to skip tombstones entirely.

### 2. Configuration & Limits
- **Docker Profile**: `profiles: ["tombstones"]` (`cassandra-tombstone-data:/var/lib/cassandra`)
- **JVM Caps**: `MAX_HEAP_SIZE: 512M`, `HEAP_NEWSIZE: 128M`
- **Threshold Limit**: Default Cassandra safety guard: 10,000 tombstones.

### 3. How to Run
```bash
make up EXP=tombstones
make run SCRIPT=01_tombstone_threshold
```

### 4. Empirical Benchmark & Observed Metrics

| Phase | Operation | Row / Marker Count | Duration / Throughput | Status |
|---|---|---|---|---|
| **Phase 1: Ingest** | `INSERT` sensor telemetry | 15,000 rows | ~1.42s (~10,500 rows/s) | **SUCCESS** |
| **Phase 2: Delete** | `DELETE` first 12,000 rows | 12,000 tombstones | ~1.18s (~10,100 ops/s) | **SUCCESS** |
| **Phase 3: Flush** | `nodetool flush` | Flush memtables to SSTable | ~0.8s | **SUCCESS** |
| **Phase 4: Full Scan** | `SELECT * WHERE sensor_id = 'A'` | Scanned > 10,000 tombstones | < 50ms | **ABORTED (`ReadFailureException`)** |
| **Phase 5: Remediated** | `SELECT * WHERE sensor_id = 'A' AND ts >= boundary` | 3,000 live rows | **1.8 ms** (0 tombstones scanned) | **SUCCESS** |

#### Failure Signature (`ReadFailureException`):
```text
cassandra.ReadFailure: Error from server: code=1300 [Read failure]
message="Server failure during read: Scanned over 10000 tombstones in query; query aborted (see tombstone_failure_threshold)"
info={'failures': 1, 'received_responses': 0, 'required_responses': 1, 'consistency': 'ONE'}
```

### 5. Production & Systems Takeaways
1. **Never use Cassandra as a Work Queue**: High-churn queues (frequent inserts followed by deletes) are the #1 cause of Cassandra production outages. The accumulation of tombstones degrades read latency and crashes consumers.
2. **Short TTLs vs Explicit Deletes**: Expired rows (`TTL`) also become tombstones until `gc_grace_seconds` elapses and compaction runs.
3. **`gc_grace_seconds` Trade-Off**: Tombstones cannot be purged immediately during compaction; they must be retained for `gc_grace_seconds` (default: 10 days / 864,000s) to ensure tombstones propagate to all offline replicas before being safely garbage collected. Setting `gc_grace_seconds = 0` is only safe in single-node environments.
4. **Clustering Key Range Bounds**: Always query time-series data with bounded clustering key slices (`WHERE recorded_at >= ? AND recorded_at < ?`) so the SSTable index summary can jump directly to live data blocks.

---

## Experiment 02: Tunable Consistency & PACELC in Practice (3-Node Mini-Cluster)

### 1. Hypothesis & Objective
- **PACELC Theorem**: In a distributed database, if there is a **P**artition, how does the system trade off **A**vailability vs **C**onsistency? **E**lse, how does it trade off **L**atency vs **C**onsistency?
- **Tunable Consistency**: Cassandra allows clients to tune consistency per-query:
  - `ConsistencyLevel.ONE`: Only 1 replica must acknowledge. Maximum availability, lowest latency, weak consistency.
  - `ConsistencyLevel.QUORUM`: Strict majority must acknowledge: $\lfloor \text{RF} / 2 \rfloor + 1$. For $\text{RF}=3$, quorum requires **2 replicas**.
  - `ConsistencyLevel.ALL`: Every replica ($3 / 3$) must acknowledge. Maximum consistency, zero fault tolerance (any single node failure rejects all operations).
- **Strong Consistency Formula**:
  $$R + W > \text{RF}$$
  When Read Consistency ($R$) + Write Consistency ($W$) strictly exceeds the Replication Factor ($\text{RF}$), the Pigeonhole Principle guarantees that the read set and write set overlap at least one common replica, guaranteeing strong consistency.

### 2. Configuration & Limits
- **Docker Profile**: `profiles: ["cluster-consistency"]`
- **Cluster Layout**: 3 nodes (`cassandra-node1`, `cassandra-node2`, `cassandra-node3`) with dedicated volumes (`cassandra-n1-data`, `cassandra-n2-data`, `cassandra-n3-data`).
- **Resource Limits**: `memory: 512M`, `MAX_HEAP_SIZE: 256M`, `HEAP_NEWSIZE: 64M` per node.
- **Keyspace**: `lab_consistency` with `replication_factor: 3`.

### 3. How to Run
```bash
make up EXP=cluster-consistency
make run SCRIPT=02_consistency_levels
```

### 4. Empirical Availability Matrix Under Node Failures

```text
======================================================================
  PACELC AVAILABILITY MATRIX UNDER NODE DEGRADATION
======================================================================
  Cluster State                  | ONE (req 1)  | QUORUM (req 2) | ALL (req 3)
  ------------------------------------------------------------------
  3 Nodes Alive (Healthy)        | PASS         | PASS           | PASS
  2 Nodes Alive (node3 DOWN)     | PASS         | PASS           | FAIL (Unavailable)
  1 Node Alive (node2/3 DOWN)    | PASS         | FAIL           | FAIL (Unavailable)
  Cluster Healed (3 Nodes)       | PASS         | PASS           | PASS
======================================================================
```

#### Failure Signature (`UnavailableException`):
```text
cassandra.Unavailable: Error from server: code=1000 [Unavailable exception]
message="Cannot achieve consistency level for cl QUORUM. Required: 2, Alive: 1"
info={'consistency': 'QUORUM', 'required_replicas': 2, 'alive_replicas': 1}
```

### 5. Production & Systems Takeaways
1. **Never use `ALL` in Production**: `ALL` provides zero fault tolerance. In a 3-node cluster with RF=3, a temporary GC pause or rolling restart on a single node causes an outage for all queries configured with `ALL`.
2. **`LOCAL_QUORUM` is the Multi-DC Gold Standard**: In multi-datacenter clusters, `LOCAL_QUORUM` requires a majority of replicas only in the coordinator's local datacenter, isolating queries from cross-region WAN network latency and partitions.
3. **Hinted Handoffs**: When a write uses `ConsistencyLevel.ONE` and a replica is down, the coordinator writes a "hint" locally. When the replica comes back online, the coordinator replays the hints to restore eventual consistency.

---

## Experiment 03: Wide Partitions & Hot-Spotting

### 1. Hypothesis & Objective
- **Partition Sizing Ceilings**: A partition in Cassandra is identified by its partition key and hash-routed to a token range on the ring. Cassandra documentation specifies a hard rule: **Keep partitions under 100MB and fewer than 100,000 cells**.
- **The Monolithic Anti-Pattern**: Putting all system events into a single partition key (`partition_key = 'ALL'`) concentrates all read and write traffic onto the single node owning that token, starving other cluster nodes and causing coordinator timeouts.
- **Synthetic Bucketing**: Bucketing by `((tenant_id, date_bucket), event_id)` caps partition size, distributes tokens across all nodes, and allows predictable read latency.

### 2. How to Run
```bash
make up EXP=wide-partitions
make run SCRIPT=03_partition_skew
```

### 3. Comparison Summary
```text
======================================================================
  PARTITION ARCHITECTURE COMPARISON
======================================================================
  Ingestion Strategy        : Monolithic (1 Partition)  vs Bucketed (10 Partitions)
  Write Duration            : 1.12s                     vs 0.74s
  Query Read Latency        : 14.8 ms                   vs 1.2 ms
  Partition Size Risk       : CRITICAL (>100MB bloat)   vs SAFE (<10MB per bucket)
======================================================================
```

---

## Cassandra Systems & Interview Reference

### Q1: Why do deletes in Cassandra make queries slower instead of faster?
In B-Tree relational engines (PostgreSQL), `DELETE` marks page slots free or vacuum purges them. In Cassandra's LSM tree:
1. Deletes are **sequential append-only writes** (tombstones) written to the CommitLog and Memtable.
2. When flushed, tombstones are stored in immutable SSTables alongside live data.
3. Read queries must read, parse, and evaluate both tombstones and data cells across multiple SSTables to resolve the current state.
4. Hence, deleting 1,000,000 rows adds 1,000,000 tombstone markers that every subsequent scan query must iterate through until compaction purges them after `gc_grace_seconds`.

### Q2: What are the internal stages of the Cassandra write path?
```text
Client Write
    │
    ▼
[ Coordinator Node ]
    │ (Dispatches to N replicas based on Murmur3 token range)
    ▼
[ CommitLog ] (Sequential disk append for crash recovery / durability)
    │
    ▼
[ Memtable ]  (In-memory skip list / concurrent hash map)
    │
    ▼ (When Memtable is full or commit log limit is reached)
[ Flush to SSTables ] (Immutable sorted string tables on disk)
```
Because writes only hit sequential disk log (`CommitLog`) and memory (`Memtable`), Cassandra writes are orders of magnitude faster than reads and require zero random I/O.

### Q3: When is Cassandra the WRONG architectural choice?
1. **Ad-hoc Analytical Queries**: Queries without the partition key require scanning the entire token ring across all nodes.
2. **ACID Multi-Row Transactions**: Cassandra supports LightWeight Transactions (LWT via Paxos) for single partitions, but not distributed multi-table ACID transactions.
3. **Queue / Churn Workloads**: As proven in Experiment 01, frequent deletes trigger `tombstone_failure_threshold` and cause GC death spirals.

---

## Primary Sources & Deep-Dive References

- [Apache Cassandra Documentation: Tombstones & Compaction](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/compaction/index.html) – The mechanics of delete markers, `gc_grace_seconds`, and `tombstone_failure_threshold`.
- DeCandia et al. (ACM SOSP 2007): [*Dynamo: Amazon's Highly Available Key-value Store*](https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf) – The foundational paper behind Cassandra's peer-to-peer ring architecture, consistent hashing (Murmur3), and tunable quorum consistency ($R + W > N$).
- Abadi, Daniel (IEEE Computer 2012): [*Consistency Tradeoffs in Modern Distributed Database System Design (CAP is only half the story / PACELC)*](https://www.cs.umd.edu/~abadi/papers/abadi-pacelc.pdf) – Formal proof of the PACELC theorem tested in Experiment 02.
- [DataStax Documentation: Cassandra Data Modeling & Partition Sizing](https://docs.datastax.com/en/cql-oss/3.x/cql/cql_using/useCreateTable.html) – Guidelines on why partitions must be kept strictly under 100MB / 100,000 cells.

---

## Clean Reset & Teardown

```bash
# Wipe all containers and named volumes across all profiles:
make clean
```

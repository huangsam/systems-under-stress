# Cassandra Limits & Internals Lab

Hands-on experiments stress-testing Apache Cassandra's LSM storage engine internals, tombstone failure aborts (`tombstone_failure_threshold`), tunable consistency under node failure (PACELC in practice), and wide partition hot-spotting.

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

---

## Quickstart & Execution Standard

Across all profiles in this lab (see [`docker-compose.yml`](./docker-compose.yml) for exact image tags, JVM heap caps, cgroups limits, and readiness healthchecks):

```bash
cd cassandra

# 1. Start target profile with automatic healthcheck wait
make up EXP=tombstones

# 2. Run the baseline crash course or a stress benchmark script
make run SCRIPT=00_crash_course
make run SCRIPT=01_tombstone_threshold

# 3. Fast in-engine table truncate or full profile teardown
make soft-reset
make down EXP=tombstones
make clean
```

---

## Experiment 00: Crash Course

Verifies Cassandra cluster connectivity, keyspace and table provisioning with composite primary keys (`((partition_key), clustering_column)`), prepared statements, $\mathcal{O}(1)$ Murmur3 partition token routing, and `system_schema` introspection.

```bash
make up EXP=tombstones
make run SCRIPT=00_crash_course
make soft-reset
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
- **Compose Profile**: `tombstones` (`cassandra-tombstone-data:/var/lib/cassandra` in [`docker-compose.yml`](./docker-compose.yml))
- **Container & JVM Caps**: `memory: 1280M`, `MAX_HEAP_SIZE: 512M`
- **Threshold Limit**: Default Cassandra safety guard (`tombstone_failure_threshold: 10000`)

### 3. How to Run
```bash
make up EXP=tombstones
make run SCRIPT=01_tombstone_threshold
make down EXP=tombstones
```

### 4. Observed Metrics & Failure Signatures

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
3. **`gc_grace_seconds` Trade-Off**: Tombstones cannot be purged immediately during compaction; they must be retained for `gc_grace_seconds` (default: 10 days / 864,000s) to ensure tombstones propagate to all offline replicas before being safely garbage collected.
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
- **Compose Profile**: `cluster-consistency` (`cassandra-n1-data`, `cassandra-n2-data`, `cassandra-n3-data` in [`docker-compose.yml`](./docker-compose.yml))
- **Cluster Layout**: 3 nodes (`cassandra-node1`, `cassandra-node2`, `cassandra-node3`)
- **Container & JVM Caps**: `memory: 896M`, `MAX_HEAP_SIZE: 384M` per node
- **Keyspace**: `lab_consistency` with `replication_factor: 3`

### 3. How to Run
```bash
make up EXP=cluster-consistency
make run SCRIPT=02_consistency_levels
make down EXP=cluster-consistency
```

### 4. Observed Metrics & Failure Signatures

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
- **The Monolithic Anti-Pattern**: Putting all system events into a single partition key (`partition_id = 'GLOBAL'`) concentrates all read and write traffic onto the single node owning that token, starving other cluster nodes and causing coordinator timeouts.
- **Synthetic Bucketing**: Bucketing by `((tenant_id, date_bucket), event_id)` caps partition size, distributes tokens across all nodes, and allows predictable read latency.

### 2. Configuration & Limits
- **Compose Profile**: `wide-partitions` (`cassandra-wide-data:/var/lib/cassandra` in [`docker-compose.yml`](./docker-compose.yml))
- **Container & JVM Caps**: `memory: 1280M`, `MAX_HEAP_SIZE: 512M`
- **Workload**: Compares 1 monolithic partition against 10 deterministically bucketed composite-key partitions.

### 3. How to Run
```bash
make up EXP=wide-partitions
make run SCRIPT=03_partition_skew
make down EXP=wide-partitions
```

### 4. Observed Metrics & Failure Signatures
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

### 5. Production & Systems Takeaways
1. **Always Bound Partition Growth by Time or Bucket ID**: Any high-velocity entity (IoT sensor, popular tenant, audit feed) must include a time bucket (e.g., `yyyy_mm_dd`) or shard modulo (`bucket_id`) inside the composite partition key `((entity_id, date_bucket), ts)`.
2. **Prevent Coordinator Heap Pressure**: Wide partitions force coordinator nodes to deserialize large SSTable index blocks and bloom filters, leading to long JVM GC pauses.

---

## Primary Sources & Deep-Dive References

- [Apache Cassandra Documentation: Tombstones & Compaction](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/compaction/index.html) – The mechanics of delete markers, `gc_grace_seconds`, and `tombstone_failure_threshold`.
- DeCandia et al. (ACM SOSP 2007): [*Dynamo: Amazon's Highly Available Key-value Store*](https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf) – The foundational paper behind Cassandra's peer-to-peer ring architecture, consistent hashing (Murmur3), and tunable quorum consistency ($R + W > N$).
- Abadi, Daniel (IEEE Computer 2012): [*Consistency Tradeoffs in Modern Distributed Database System Design (CAP is only half the story / PACELC)*](https://www.cs.umd.edu/~abadi/papers/abadi-pacelc.pdf) – Formal proof of the PACELC theorem tested in Experiment 02.
- [DataStax Documentation: Cassandra Data Modeling & Partition Sizing](https://docs.datastax.com/en/cql-oss/3.x/cql/cql_using/useCreateTable.html) – Guidelines on why partitions must be kept strictly under 100MB / 100,000 cells.

---

## Clean Reset & Teardown

```bash
# Truncate lab tables without restarting the JVM (fast reset):
make soft-reset

# Wipe all containers and named volumes across all profiles:
make clean
```

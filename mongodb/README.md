# MongoDB Limits & Internals Lab

Hands-on experiments stress-testing in-memory sort ceilings, external disk spill, write durability guarantees (`w:0` vs `w:1` vs `j:True`) under abrupt failure (`kill -9`), BSON document size limits, and replica set elections in MongoDB.

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) leveraging **Docker Compose Profiles** and **dedicated named volumes** for total data isolation between runs.

```text
mongodb/
├── docker-compose.yml              # Base anchors &mongo-base / &mongo-rs-node, cgroups limits, profiles
├── Makefile                        # One-touch runner targets (up, run, soft-reset, mongosh, down, clean)
├── experiments/
│   ├── 00_crash_course.py          # BSON documents, aggregation pipelines & serverStatus
│   ├── 01_sort_memory_limit.py     # 100MB in-memory sort limit vs allowDiskUse & B-Tree index
│   ├── 02_write_concern_crash.py   # Write durability & crash recovery across w:0, w:1, j:True
│   ├── 03_document_size_cap.py     # Document growth, 16MB BSON ceiling & bucket pattern
│   └── 04_replica_set_elections.py # 3-node replica set, stepdown failover & oplog exhaustion
└── README.md
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) and run via `uv run` or `make run`.

---

## Quickstart & Execution Standard

Across all profiles in this lab (see [`docker-compose.yml`](./docker-compose.yml) for exact image tags, WiredTiger cache limits, and healthchecks):

```bash
cd mongodb

# 1. Start target profile with automatic healthcheck wait
make up EXP=unindexed-sort

# 2. Run the baseline crash course or a stress benchmark script
make run SCRIPT=00_crash_course
make run SCRIPT=01_sort_memory_limit

# 3. Fast in-engine database drop or full profile teardown
make soft-reset
make down EXP=unindexed-sort
make clean
```

---

## Experiment 00: Crash Course

Verifies MongoDB connectivity, nested BSON document creation, atomic field updates (`$set`, `$inc`, `$push`), multi-stage aggregation pipelines (`$match` $\rightarrow$ `$group` $\rightarrow$ `$sort`), and storage engine diagnostics (`collStats`, `serverStatus`).

```bash
make up EXP=unindexed-sort
make run SCRIPT=00_crash_course
make soft-reset
```

---

## Experiment 01: The 100MB In-Memory Sort Limit

### 1. Hypothesis & Objective
- **In-Memory Sort Ceiling**: MongoDB enforces a strict internal limit on blocking in-memory sorts:
  $$\text{internalQueryMaxBlockingSortMemoryUsageBytes} = 104,857,600\text{ bytes (100MB)}$$
- When a query requests sorted results on fields without a supporting B-Tree index, the query planner must scan and buffer candidate documents in RAM to execute a blocking sort.
- **Hypothesis 1 (Unindexed Sort Failure)**: If the buffered sort set exceeds 104,857,600 bytes, MongoDB immediately aborts query execution with an `OperationFailure` error (Code 96 / 292).
- **Hypothesis 2 (External Disk Spill)**: Passing `allowDiskUse=True` instructs WiredTiger to spill intermediate sort runs to temporary disk files, allowing the query to complete at the expense of disk I/O latency.
- **Hypothesis 3 (Index Scan Elimination)**: Creating an index on the sort field eliminates the blocking `SORT` stage; documents stream in order via index traversal (`IXSCAN`) using 0 bytes of sort RAM.

### 2. Configuration & Limits
- **Compose Profile**: `unindexed-sort` (`mongo-sort-data:/data/db` in [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `memory: 384M`, `cpus: '1.0'`
- **Dataset Size**: 100,000 JSON documents (~115MB raw BSON) in `lab_db.orders`

### 3. How to Run
```bash
make up EXP=unindexed-sort
make run SCRIPT=01_sort_memory_limit
make down EXP=unindexed-sort
```

### 4. Observed Metrics & Failure Signatures

#### Failure Signature: In-Memory Sort Ceiling Exceeded
```text
pymongo.errors.OperationFailure: PlanExecutor error during find command :: caused by ::
Sort exceeded memory limit of 104857600 bytes, but did not opt in to external sort.
Error Code: 96
```

#### Execution Comparison

| Strategy | Query Pattern | Engine Stage | Memory Used | Spilled to Disk? | Execution Time | Outcome |
|---|---|---|---|---|---|---|
| **Unindexed (RAM only)** | `.sort("timestamp", -1)` | `SORT` (Blocking) | Exceeded 100MB | No (Disallowed) | ~420ms | **FAILED (Code 96)** |
| **Unindexed (`allowDiskUse`)** | `.sort(...).allow_disk_use(True)` | `SORT` (External) | Spilled to disk | **Yes** (`usedDisk: true`) | ~1,850ms | **SUCCEEDED** |
| **B-Tree Indexed** | `.sort("timestamp", -1)` with index | `IXSCAN` $\rightarrow$ `FETCH` | **0 bytes** | **No** | **~2ms** | **SUCCEEDED** |

### 5. Production & Systems Takeaways
1. **Unindexed Sorts are Production Timebombs**: In development environments with small datasets, `.sort()` succeeds invisibly in memory. Once a collection grows past 100MB in production, queries abruptly crash with `OperationFailure (Code 96)`.
2. **Never rely on `allowDiskUse=True` for OLTP endpoints**: Spilling sort runs to disk causes disk I/O contention and spikes query latency from milliseconds to seconds. Follow the **Equality, Sort, Range (ESR)** indexing rule so the sort stage is fulfilled directly by `IXSCAN`.

---

## Experiment 02: Write Concerns & Crash Durability (`kill -9`)

### 1. Hypothesis & Objective
MongoDB offers tunable write durability through write concern options (`w` and `j`):
1. **Unacknowledged (`w=0`)**: The driver writes data into the local TCP socket buffer and immediately returns. If the container crashes, writes in network buffers or server queues are lost.
2. **Memory-Acknowledged (`w=1, j=False`)**: The server acknowledges the write as soon as it is written to WiredTiger's in-memory cache. Writes waiting in RAM before the journal commit interval (up to 100ms) or checkpoint (60s) are lost on `SIGKILL`.
3. **Journal-Committed (`w=1, j=True`)**: The server blocks acknowledgment until the write is flushed and fsynced to the on-disk journal (write-ahead log). Upon restart, WiredTiger replays the journal, guaranteeing 0 lost acknowledged writes.

### 2. Configuration & Limits
- **Compose Profile**: `durability` (`mongo-durability-data:/data/db` in [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `memory: 384M`, `cpus: '1.0'`
- **Crash Simulation**: `docker compose --profile durability kill -s SIGKILL` (`kill -9`)

### 3. How to Run
```bash
make up EXP=durability
make run SCRIPT=02_write_concern_crash
make down EXP=durability
```

### 4. Observed Metrics & Failure Signatures

| Mode | Write Concern | Ack/Sent Writes | Recovered Documents | Lost Acknowledged Writes | Durability Guarantee |
|---|---|---|---|---|---|
| **Unacknowledged** | `w=0` | ~1,500 | ~1,120 | **~380 (25.3%)** | None (Fire-and-forget) |
| **Memory-Ack (Standalone Default)** | `w=1, j=False` | ~1,200 | ~1,040 | **~160 (13.3%)** | Vulnerable (Lost uncommitted cache) |
| **Journal-Synced** | `w=1, j=True` | ~700 | 700 | **0 (0.0%)** | **100% Guaranteed Durability** |

### 5. Production & Systems Takeaways
1. **Standalone vs Replica Set Write Concern Defaults**: On a standalone instance with `w: 1, j: false`, acknowledged writes reside in RAM and can vanish on power loss or kernel panic.
2. **Enforce Journaled Durability**: For financial ledgers or order confirmations, explicitly enforce `j: True` (or `w: "majority", j: True` on a replica set) so acknowledgments wait for WAL disk flush.

---

## Experiment 03: Document Growth, Reallocation & the 16MB BSON Ceiling

### 1. Hypothesis & Objective
- Appending events indefinitely into a single parent document via `$push` degrades write latency over time as WiredTiger repeatedly reallocates and rewrites the growing multi-megabyte document on every update.
- Once the document crosses **16,777,216 bytes (16MB)**, MongoDB hard-rejects the update (`BSONObjectTooLarge` / `DocumentTooLarge`).
- Remediating with the **Bucket Pattern** (`bucket_count < 500` + `upsert: true`) keeps individual documents small and write latency flat.

### 2. Configuration & Limits
- **Compose Profile**: `document-limits` (`mongo-doclimit-data:/data/db` in [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `memory: 384M`, `cpus: '1.0'`
- **Engine Ceiling**: Hard 16,777,216-byte (16MB) BSON document size cap

### 3. How to Run
```bash
make up EXP=document-limits
make run SCRIPT=03_document_size_cap
make down EXP=document-limits
```

### 4. Observed Metrics & Failure Signatures

| Ingestion Pattern | Document Size Reached | Write Latency Trend | Outcome / Failure Signature |
|---|---|---|---|
| **Unbounded Array (`$push`)** | **16,777,216 bytes (16.0 MB)** | Degrades linearly with document size | **REJECTED (`OperationFailure: BSONObjectTooLarge`)** |
| **Bucket Pattern (`count < 500`)** | Bounded (~250 KB / bucket) | Flat sub-millisecond writes | **SUCCEEDED (Infinite horizontal scale)** |

### 5. Production & Systems Takeaways
1. **Never model 1-to-Many as an unbounded embedded array**: High-frequency telemetry, audit logs, or comments appended via `$push` will eventually hit the 16MB BSON cliff and break writes.
2. **Use the Bucket Pattern for Time-Series / Event Streams**: Group events into bounded sub-documents (e.g., by hour or fixed count threshold) so WiredTiger updates remain small in-place modifications.

---

## Experiment 04: 3-Node Replica Set Elections, Write Concern Durability & Oplog Exhaustion

### 1. Hypothesis & Objective
In production, MongoDB is deployed as a multi-node Replica Set (Primary-Secondary-Secondary) to achieve high availability and consensus durability:
1. **Cluster Topology & Quorum**: A 3-node replica set requires a majority quorum of $\lfloor N/2 \rfloor + 1 = 2$ voting members. Raft-style election terms increment with every election, invalidating stale primaries.
2. **Durability Across Sudden Primary Stepdown**:
   - Streaming writes with **Local Acknowledgment (`w: 1`)** only commit to the primary's memory/journal. An abrupt stepdown (`replSetStepDown`) leaves in-flight writes vulnerable to `NotPrimaryError` / rollback.
   - Streaming writes with **Majority Consensus (`w: "majority", j: True`)** require quorum replication before client acknowledgment, guaranteeing **0% data loss** across failover.
3. **Capped Oplog Window Exhaustion**:
   - MongoDB maintains a rolling circular log of write operations in `local.oplog.rs` (`--oplogSize 10`).
   - If a secondary is paused or partitioned while write volume churns past the oplog's retention capacity, the primary truncates the oldest entries past the secondary's last applied timestamp.
   - Upon reconnecting, the stale secondary discovers an unbridgeable oplog gap and transitions into **`RECOVERING`** state, requiring a costly full Initial Sync.

### 2. Configuration & Limits
- **Compose Profile**: `replica-set` (`mongo-rs1-data`, `mongo-rs2-data`, `mongo-rs3-data` in [`docker-compose.yml`](./docker-compose.yml))
- **Topology**: 3 nodes (`mongo-node1` on `27017`, `mongo-node2` on `27018`, `mongo-node3` on `27019`)
- **Container Limits**: `memory: 128M`, `cpus: '0.5'`, `--oplogSize 10` (10MB fixed rolling window) per node

### 3. How to Run
```bash
make up EXP=replica-set
make run SCRIPT=04_replica_set_elections
make soft-reset
make down EXP=replica-set
```

### 4. Observed Metrics & Failure Signatures

#### Durability Comparison Under Sudden Primary Stepdown
| Write Concern | Attempted | Acknowledged | Stored on New Primary | Lost Acknowledged | Durability Guarantee |
|---|---|---|---|---|---|
| **`w: 1` (Local Memory)** | 28 | 27 | 27 | **0** | Vulnerable to Rollback |
| **`w: "majority", j: True`** | 11 | 11 | 11 | **0 (0.0% loss)** | **100% Durable (Committed)** |

#### Oplog Window Exhaustion Outcome
| Node | Role / State | Node Optime | Primary Oldest Oplog | Oplog Gap Status |
|---|---|---|---|---|
| **mongo-node3** | `RECOVERING` | `Timestamp(1794497423, 14)` | `Timestamp(1794497435, 226)` | **Fell Off Oplog (Stale - Requires Initial Sync)** |

### 5. Production & Systems Takeaways
1. **Durability Requires Majority Consensus (`w: "majority"`)**: Primary stepdown or network partitions can orphan un-replicated writes committed to a single node. Always enforce `w: "majority"` and `retryWrites=true` to guarantee zero loss across cluster elections.
2. **Oplog Sizing is a High-Availability SLA**: The oplog is a fixed circular buffer. If a replica lags longer than the oplog retention window, it falls into `RECOVERING` state and requires an I/O-intensive Initial Sync. Size the oplog to cover maximum anticipated maintenance and outage windows.

---

## Primary Sources & Deep-Dive References

- [MongoDB Manual: Sort Operations & In-Memory Limits](https://www.mongodb.com/docs/manual/reference/limits/#mongodb-limit-Sort-Operations) – Specification for the 100MB internal blocking sort limit and `allowDiskUse` semantics.
- [MongoDB Manual: Write Concern Architecture](https://www.mongodb.com/docs/manual/core/replica-set-write-concern/) – Mechanical differences between unacknowledged (`w:0`), acknowledged (`w:1`), and journal-committed (`j:true`) writes.
- [MongoDB Manual: BSON Document Size Limit (16MB)](https://www.mongodb.com/docs/manual/reference/limits/#mongodb-limit-BSON-Document-Size) – Explains the hard 16MB document boundary, WiredTiger reallocation overhead, and the Bucket Pattern.
- [MongoDB Engineering: The Equality, Sort, Range (ESR) Rule](https://www.mongodb.com/docs/manual/tutorial/equality-sort-range-rule/) – Index optimization guidelines to eliminate blocking `SORT` query plan stages.

---

## Clean Reset & Teardown

```bash
# Drop 'lab_db' database in-place (fast reset):
make soft-reset

# Wipe all containers and named volumes for this lab:
make clean
```

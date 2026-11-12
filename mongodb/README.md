# MongoDB Limits & Internals Lab

Hands-on experiments stress-testing memory sort ceilings, external disk spill, and write durability guarantees (`w:0` vs `w:1` vs `j:True`) under abrupt failure (`kill -9`) in MongoDB (`mongo:9.0.4`).

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) leveraging **Docker Compose Profiles** and **dedicated named volumes** for total data isolation between runs.

```text
mongodb/
├── docker-compose.yml       # Pinned image (mongo:9.0.4), cgroups limits, profiles
├── Makefile                 # One-touch runner targets
├── experiments/
│   ├── 00_crash_course.py         # BSON documents, aggregation pipelines & serverStatus
│   ├── 01_sort_memory_limit.py    # 100MB in-memory sort limit vs allowDiskUse & B-Tree index
│   ├── 02_write_concern_crash.py  # Write durability & crash recovery across w:0, w:1, j:True
│   ├── 03_document_size_cap.py    # Document growth, 16MB BSON ceiling & bucket pattern
│   └── 04_replica_set_elections.py # 3-node replica set, stepdown failover & oplog exhaustion
└── README.md
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) and run via `uv run` or `make run`.

---

## Experiment 01: The 100MB In-Memory Sort Limit

### 1. Hypothesis & Objective
- **In-Memory Sort Ceiling**: MongoDB enforces a strict internal limit on blocking in-memory sorts:
  $$\text{internalQueryMaxBlockingSortMemoryUsageBytes} = 104,857,600\text{ bytes (100MB)}$$
- When a query requests sorted results on fields without a supporting B-Tree index, the query planner must scan and buffer candidate documents in RAM to execute a blocking sort.
- **Hypothesis 1 (Unindexed Sort Failure)**: If the dataset exceeds 104,857,600 bytes, MongoDB immediately aborts the query execution with an `OperationFailure` error (Code 96 / 292):
  `PlanExecutor error during find command :: caused by :: Sort exceeded memory limit of 104857600 bytes, but did not opt in to external sort.`
- **Hypothesis 2 (External Disk Spill)**: Passing `allowDiskUse=True` instructs WiredTiger to spill intermediate sort runs to temporary disk files, allowing the query to complete successfully at the expense of disk I/O latency.
- **Hypothesis 3 (Index Scan Elimination)**: Creating an index on the sort field completely eliminates the blocking `SORT` stage. Documents are streamed in order via index traversal (`IXSCAN`) using 0 bytes of sorting memory.

### 2. Configuration & Constraints
- **Docker Image**: `mongo:9.0.4`
- **Resource Constraints**: `memory: 384M`, `cpus: '1.0'`
- **Target Database / Collection**: `lab_db.orders`
- **Dataset Size**: 100,000 JSON documents (~115MB raw BSON)

### 3. How to Run
```bash
cd mongodb

# 1. Start the unindexed-sort container
make up EXP=unindexed-sort

# 2. Run the workload
make run SCRIPT=01_sort_memory_limit

# 3. Clean up containers and wipe named volume
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
| **B-Tree Indexed** | `.sort("timestamp", -1)` with index | `IXSCAN` $\to$ `FETCH` | **0 bytes** | **No** | **~2ms** | **SUCCEEDED** |

---

## Experiment 02: Write Concerns & Crash Durability (`kill -9`)

### 1. Hypothesis & Objective
MongoDB offers tunable write durability through write concern options (`w` and `j`):
1. **Unacknowledged (`w=0`)**: The driver writes data into the local TCP socket buffer and immediately returns. If the container crashes, writes in network buffers or server queues that have not yet reached WiredTiger are lost.
2. **Memory-Acknowledged (`w=1, j=False`)**: The server acknowledges the write as soon as it is committed to WiredTiger's in-memory cache. Writes waiting in RAM before the journal commit interval (up to 100ms) or checkpoint (60s) will be lost if the server suffers an abrupt power loss or `SIGKILL`.
3. **Journal-Committed (`w=1, j=True`)**: The server blocks acknowledgment until the write has been flushed and fsynced to the on-disk journal file (write-ahead log). Upon container restart, WiredTiger replays the journal log, guaranteeing 0 lost acknowledged writes.

### 2. Configuration & Constraints
- **Docker Image**: `mongo:9.0.4`
- **Resource Constraints**: `memory: 384M`, `cpus: '1.0'`
- **Crash Simulation**: `docker compose --profile durability kill -s SIGKILL` (kill -9)

### 3. How to Run
```bash
cd mongodb

# 1. Start the durability container
make up EXP=durability

# 2. Run the crash workload
make run SCRIPT=02_write_concern_crash

# 3. Clean up containers and wipe named volume
make down EXP=durability
```

### 4. Empirical Durability Matrix

| Mode | Write Concern | Ack/Sent Writes | Recovered Documents | Lost Acknowledged Writes | Durability Guarantee |
|---|---|---|---|---|---|
| **Unacknowledged** | `w=0` | ~1,500 | ~1,120 | **~380 (25.3%)** | None (Fire-and-forget) |
| **Memory-Ack (Standalone Default)** | `w=1, j=False` | ~1,200 | ~1,040 | **~160 (13.3%)** | Vulnerable (Lost uncommitted cache) |
| **Journal-Synced** | `w=1, j=True` | ~700 | 700 | **0 (0.0%)** | **100% Guaranteed Durability** |

---

## Experiment 03: Document Growth, Reallocation & the 16MB BSON Ceiling

### 1. Hypothesis & Objective
- Appending events indefinitely into a single parent document via `$push` degrades write latency over time as WiredTiger repeatedly reallocates and rewrites the growing multi-megabyte document on every update.
- Once the document crosses **16,777,216 bytes (16MB)**, MongoDB hard-rejects the update (`BSONObjectTooLarge` / `DocumentTooLarge`).
- Remediating with the **Bucket Pattern** (`bucket_count < 64` + `upsert: true`) keeps individual documents small (~256KB) and write latency flat.

### 2. How to Run
```bash
make up EXP=document-limits
make run SCRIPT=03_document_size_cap
make down EXP=document-limits
```

---

## Experiment 04: 3-Node Replica Set Elections, Write Concern Durability & Oplog Exhaustion

### 1. Hypothesis & Objective
In production, MongoDB is deployed as a multi-node Replica Set (Primary-Secondary-Secondary) to achieve high availability and consensus durability:
1. **Cluster Topology & Quorum**: A 3-node replica set requires a majority quorum of $\lfloor N/2 \rfloor + 1 = 2$ voting members. Raft-style election terms increment with every election, invalidating stale primaries.
2. **Durability Across Sudden Primary Stepdown**:
   - Streaming writes with **Local Acknowledgment (`w: 1`)** only commit to the primary's memory/journal. An abrupt stepdown (`replSetStepDown`) leaves in-flight and uncommitted writes stranded or rejected with `NotPrimaryError` / `AutoReconnect`.
   - Streaming writes with **Majority Consensus (`w: "majority", j: True`)** require quorum replication before client acknowledgment, guaranteeing **0% data loss** across failover.
3. **Capped Oplog Window Exhaustion**:
   - MongoDB maintains a rolling circular log of write operations in `local.oplog.rs` (`--oplogSize 10`).
   - If a secondary is paused or partitioned while write volume churns past the oplog's retention capacity, the primary truncates the oldest entries past the secondary's last applied timestamp.
   - Upon reconnecting, the stale secondary discovers an unbridgeable oplog gap, fails sync source selection (`Cannot select sync source because it is too far behind`), and transitions into **`RECOVERING`** state, requiring a costly full Initial Sync.

### 2. Configuration & Constraints
- **Docker Image**: `mongo:9.0.4`
- **Resource Constraints**: `memory: 128M`, `cpus: '0.5'` per node
- **Replica Set Configuration**: 3 nodes (`mongo-node1: 27017`, `mongo-node2: 27018`, `mongo-node3: 27019`)
- **Capped Oplog Size**: `--oplogSize 10` (10MB fixed rolling window)
- **Isolated Named Volumes**: `mongo-rs1-data`, `mongo-rs2-data`, `mongo-rs3-data`

### 3. How to Run
```bash
cd mongodb

# 1. Start the 3-node replica set cluster
make up EXP=replica-set

# 2. Run the experiment workload
make run SCRIPT=04_replica_set_elections

# 3. Soft reset or clean teardown
make soft-reset
make down EXP=replica-set
```

### 4. Observed Metrics & Failure Signatures

#### Replica Set Topology & Quorum
```text
Replica Set: rs0  |  Current Election Term: 1  |  Voting Quorum: 2 of 3 nodes (majority)

╭──────────────────┬─────────────────┬──────────────┬────────┬────────┬───────────────────────────╮
│ Node Host        │ Port (Host)     │ Role / State │ Health │ Uptime │ Optime                    │
├──────────────────┼─────────────────┼──────────────┼────────┼────────┼───────────────────────────┤
│ mongo-node1:2017 │ 127.0.0.1:27017 │ PRIMARY      │ 1 (OK) │ 98s    │ Timestamp(1794497402, 8)  │
│ mongo-node2:2017 │ 127.0.0.1:27018 │ SECONDARY    │ 1 (OK) │ 10s    │ Timestamp(1794497402, 1)  │
│ mongo-node3:2017 │ 127.0.0.1:27019 │ SECONDARY    │ 1 (OK) │ 10s    │ Timestamp(1794497402, 1)  │
╰──────────────────┴─────────────────┴──────────────┴────────┴────────┴───────────────────────────╯
✔ Quorum Verified: Exactly 1 PRIMARY and 2 SECONDARIES active.
```

#### Durability Comparison Under Sudden Primary Stepdown
| Write Concern | Attempted | Acknowledged | Stored on New Primary | Lost Acknowledged | Durability Guarantee |
|---|---|---|---|---|---|
| **`w: 1` (Local Memory)** | 28 | 27 | 27 | **0** | Vulnerable to Rollback |
| **`w: "majority", j: True`** | 11 | 11 | 11 | **0 (0.0% loss)** | **100% Durable (Committed)** |

#### Oplog Window Exhaustion Outcome
| Node | Role / State | Node Optime | Primary Oldest Oplog | Oplog Gap Status |
|---|---|---|---|---|
| **mongo-node3** | `RECOVERING` | `Timestamp(1794497423, 14)` | `Timestamp(1794497435, 226)` | **Fell Off Oplog (Stale - Requires Initial Sync)** |

---

## Production & Systems Takeaways

1. **Unindexed Sorts are Production Timebombs**:
   - In development environments with small datasets, `.sort()` succeeds invisibly in memory. Once a collection grows past 100MB in production, queries abruptly crash with `OperationFailure (Code 96)`.
   - Never rely on `allowDiskUse=True` as a permanent fix for OLTP endpoints. Spilling sort runs to disk causes disk I/O contention, locks up WiredTiger cache pages, and spikes query latency from milliseconds to seconds.
   - **Remedy**: Follow the **Equality, Sort, Range (ESR)** indexing rule so the sort stage is eliminated and fulfilled directly by the index traversal (`IXSCAN`).

2. **Standalone vs Replica Set Write Concern Defaults**:
   - In a standalone MongoDB instance, the default write concern is `w: 1, j: false`. Acknowledged writes are held in RAM and are not guaranteed to survive a power outage or kernel panic.
   - For mission-critical transactional data (financial ledgers, order confirmations), applications must explicitly enforce `j: True` or utilize replica sets with `w: "majority"` and journaled durability.

3. **Durability Requires Majority Consensus (`w: majority`)**:
   - Primary stepdown or network partitions can orphan un-replicated writes committed to a single node. Standalone defaults or `w: 1` will experience write rollbacks.
   - Always enforce `w: "majority"` and enable retryable writes (`retryWrites=true`) to guarantee zero loss across cluster elections.

4. **Oplog Sizing is a High-Availability SLA**:
   - The oplog is a fixed circular buffer. If a replica lags or remains disconnected longer than the oplog retention window, it falls into `RECOVERING` state and cannot catch up via normal replication.
   - Recovering from an oplog gap requires a full, I/O-intensive Initial Sync which impacts production database performance. Size the oplog to cover maximum anticipated maintenance, snapshot, and backup windows.

---

## Primary Sources & Deep-Dive References

- [MongoDB Manual: Sort Operations & In-Memory Limits](https://www.mongodb.com/docs/manual/reference/limits/#mongodb-limit-Sort-Operations) – Specification for the 100MB internal blocking sort limit and `allowDiskUse` semantics.
- [MongoDB Manual: Write Concern Architecture](https://www.mongodb.com/docs/manual/core/replica-set-write-concern/) – Mechanical differences between unacknowledged (`w:0`), acknowledged (`w:1`), and journal-committed (`j:true`) writes.
- [MongoDB Manual: BSON Document Size Limit (16MB)](https://www.mongodb.com/docs/manual/reference/limits/#mongodb-limit-BSON-Document-Size) – Explains the hard 16MB document boundary, WiredTiger reallocation overhead, and the Bucket Pattern.
- [MongoDB Engineering: The Equality, Sort, Range (ESR) Rule](https://www.mongodb.com/docs/manual/tutorial/equality-sort-range-rule/) – Index optimization guidelines to eliminate blocking `SORT` query plan stages.

---

## Clean Reset & Teardown

```bash
# Wipe all containers and named volumes for this lab:
make clean
```

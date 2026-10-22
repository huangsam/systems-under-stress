# PostgreSQL Limits & Internals Lab

Hands-on experiments stress-testing transaction isolation anomalies, lock queue saturation, and memory limits (`work_mem`) in PostgreSQL (`postgres:18.6`).

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) with **Docker Compose Profiles** and **dedicated named volumes** for total data isolation between runs.

```text
postgres/
├── docker-compose.yml       # Pinned image, cgroups limits, profiles
├── Makefile                 # One-touch runner targets
├── initdb.d/
│   └── init.sql             # Automatic schema initialization
├── experiments/
│   ├── 01_isolation.py      # Dirty reads & Write Skew (Repeatable Read vs Serializable)
│   ├── 02_locks.py          # Hot row lock queues & deadlock detection
│   └── 03_work_mem.py       # Sort spill-to-disk on low work_mem
└── README.md
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) and run friction-free with `uv run`.

---

## Experiment 01: Transaction Isolation Anomalies & Write Skew

### 1. Hypothesis & Objective
- **Dirty Read Hypothesis**: In PostgreSQL, `READ UNCOMMITTED` is treated identically to `READ COMMITTED` by PostgreSQL's MVCC tuple visibility rules; uncommitted changes from concurrent sessions are never visible.
- **Write Skew Hypothesis (Doctors on Call)**:
  - When two doctors concurrently attempt to step down from call (rule: $\ge 1$ doctor must remain on call), `REPEATABLE READ` allows both transactions to read the snapshot ($count = 2$), update distinct rows, and commit successfully. This breaks the global constraint ($count = 0$).
  - `SERIALIZABLE` uses Serializable Snapshot Isolation (SSI) to detect the read/write dependency cycle and terminates one transaction with error code `40001 serialization_failure`.

### 2. Configuration & Constraints
- **Docker Image**: `postgres:18.6`
- **Resource Constraints**: `memory: 256M`, `cpus: '1.0'`
- **Database**: `lab_db`

### 3. How to Run
```bash
cd postgres

# 1. Start the isolation lab container
make up EXP=isolation

# 2. Run the workload
make run SCRIPT=01_isolation

# 3. Clean up containers and wipe named volumes
make clean
```

---

## Empirical Benchmark & Results

### Part 1: Dirty Read Immunity
- **Test**: Session A updates `doctors.name` inside an uncommitted transaction; Session B reads concurrently under `READ UNCOMMITTED`.
- **Result**: `Session B` read only the committed state.
- **Engine Behavior**: PostgreSQL does not have uncommitted dirty tuple reads; `READ UNCOMMITTED` is mapped directly to `READ COMMITTED`.

### Part 2: The Doctors On-Call Write Skew

| Isolation Level | Dr. Alice (Tx A) | Dr. Bob (Tx B) | Remaining On Call | Invariant Status |
|---|---|---|---|---|
| **`REPEATABLE READ`** | `COMMITTED` | `COMMITTED` | **0** (Both stepped down!) | **VIOLATED** (Write Skew anomaly) |
| **`SERIALIZABLE`** | `COMMITTED` | **`SERIALIZATION_FAILURE (40001)`** | **1** (Dr. Alice only) | **PRESERVED** (SSI rw-dependency abort) |

---

## Experiment 02: Lock Contention, Hot-Row Queues & Deadlock Detection

### 1. Hypothesis & Objective
- **Hot Row Serialization**: When 50 concurrent transactions increment a single row (`Alice`, `id=1`), PostgreSQL row-level exclusive locks (`XLock`) serialize updates. Even under high concurrency, zero lost updates occur and final balance will strictly equal $1,050.00.
- **Controlled Deadlock**: When two concurrent transactions acquire locks in opposite directions ($A \rightarrow B$ vs $B \rightarrow A$), circular wait occurs. PostgreSQL's background deadlock detector fires after `deadlock_timeout=100ms`, breaks the cycle, and terminates the younger transaction with SQLSTATE `40P01 deadlock_detected`.
- **Global Lock Ordering**: Deterministic lock ordering (`ORDER BY id` or `min(id), max(id)`) eliminates circular wait, yielding 100% success with zero deadlocks.

### 2. Empirical Results

| Test Scenario | Concurrency | Outcome | Final Balance / Status |
|---|---|---|---|
| **Hot Row Contention** | 50 concurrent updates | **50 / 50 succeeded** | **$1,050.00** (Zero lost updates) |
| **Opposite Lock Order** | 2 conflicting transactions | **Deadlock detected & broken** | Tx 1: `COMMITTED`, Tx 2: `40P01` |
| **Deterministic Lock Ordering** | 40 bidirectional transfers | **40 / 40 succeeded** | **0 deadlocks** (Wealth preserved: $2,000) |

---

## Experiment 03: PostgreSQL Memory Limits (`work_mem`) & Disk Spill

### 1. Hypothesis & Objective
- `work_mem` controls the maximum RAM allocated to an individual sort or hash table before PostgreSQL spills to temporary disk files.
- Sorting 150,000 text payloads under `work_mem = 4MB` will force an external merge sort spilling megabytes to disk.
- Elevating `work_mem = 64MB` allows the entire sort to complete in RAM via in-memory quicksort.

### 2. Empirical Benchmark (`EXPLAIN (ANALYZE, BUFFERS)`)

| Configuration | Sort Method Observed | Disk Spill | RAM Consumed | Execution Time |
|---|---|---|---|---|
| **`work_mem = 4MB`** | `external merge` | **16,744 kB (16.7 MB)** | ~4 MB | 155.9 ms |
| **`work_mem = 64MB`** | `quicksort` | **0 kB (Pure RAM)** | **23,723 kB (23.7 MB)** | 147.7 ms |

### 3. Production & Systems Takeaways
1. **The `work_mem` Multiplier Dilemma**: `work_mem` is **NOT** a global server-wide ceiling. It is allocated *per sort or hash node per query per connection*. A complex analytical query with 3 joins and 2 sorts can consume $5 \times \text{work\_mem}$. If 50 connections execute simultaneously, memory usage spikes to $250 \times \text{work\_mem}$, risking kernel OOM kills.
2. **Best Practice**: Keep global `work_mem` conservative (4MB–16MB) in `postgresql.conf`, and elevate it dynamically for specific batch jobs or reporting queries using `SET LOCAL work_mem = '128MB'`.

---

## Clean Reset & Teardown

```bash
# Wipe all containers and named volumes for this lab:
make clean
```

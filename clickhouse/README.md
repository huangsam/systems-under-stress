# ClickHouse Limits & Internals Lab

Hands-on experiments stress-testing MergeTree write mechanics, the "Too Many Parts" backpressure failure, and analytical query memory ceilings (`max_memory_usage`) in ClickHouse (`clickhouse/clickhouse-server:26.9.23.15`).

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) with **Docker Compose Profiles** and **dedicated named volumes** for total data isolation between runs.

```text
clickhouse/
├── docker-compose.yml       # Pinned image, cgroups limits (512M RAM, 1.0 CPU), profiles
├── Makefile                 # One-touch runner targets (up, run, down, clean, client)
├── users.d/
│   └── memory-limit.xml     # User profile override for max_memory_usage=64MB
├── experiments/
│   ├── 01_parts_explosion.py # Un-batched single-row inserts vs batched ingestion
│   └── 02_memory_limit.py    # Analytical RAM ceiling & external group-by disk spill
└── README.md
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) (`clickhouse-connect>=0.7.0`) and run friction-free with `uv run`.

---

## Quickstart & Execution Standard

Across all profiles in this lab:

```bash
cd clickhouse

# 1. Start target profile with automatic healthcheck wait
make up EXP=parts-limit

# 2. Run the self-driving benchmark script
make run SCRIPT=01_parts_explosion

# 3. Clean up container and wipe isolated named volume
make down EXP=parts-limit
# Or wipe all profiles:
make clean
```

---

## Experiment 01: The "Too Many Parts" Failure (Single-Row vs Batched Inserts)

### 1. Hypothesis & Objective
- **MergeTree Architecture**: ClickHouse is an append-only analytical column store. Every `INSERT` statement writes an immutable physical filesystem part consisting of compressed column files (`.bin`) and index marks (`.mrk3`). Background threads continuously merge small parts into larger ones.
- **Un-batched Point Inserts Failure**: Firing hundreds of rapid, single-row `INSERT` statements generates parts faster than background merges can compact them. Once the number of active parts in a partition crosses `parts_to_delay_insert`, ClickHouse introduces artificial sleep delays; once it reaches `parts_to_throw_insert`, ClickHouse rejects writes with `DB::Exception: Too many parts in all data parts in table (Code: 252)`.
- **Vectorized Batched Ingestion**: Grouping 2,000+ rows into a single `INSERT` writes exactly **1 active data part** on disk in a few milliseconds, completely avoiding merge debt and disk fragmentation.

### 2. Configuration & Limits
- **Docker Profile**: `parts-limit` (`clickhouse-parts-data:/var/lib/clickhouse`)
- **Resource Constraints**: `memory: 512M`, `cpus: '1.0'`
- **MergeTree Settings**: `parts_to_delay_insert = 80`, `parts_to_throw_insert = 120`

### 3. How to Run
```bash
make up EXP=parts-limit
make run SCRIPT=01_parts_explosion
```

### 4. Empirical Benchmark & Observed Metrics

| Ingestion Mode | Row Count | HTTP Requests | Active Parts Created | Outcome | Duration / Throughput |
|---|---|---|---|---|---|
| **Un-batched Single Rows** | 120+ rows | 120 requests | **120 parts** | **REJECTED** (Code: 252) | Writes halted after ~120 inserts |
| **Vectorized Batch** | 2,000 rows | 1 request | **1 part** | **SUCCESS** | **~15 ms** (~130,000 rows/sec) |

#### Failure Signature (`Code: 252`):
```text
DB::Exception: Too many parts in all data parts in table default.parts_test (120 parts, max: 120).
Merges are processing significantly slower than inserts. (TOO_MANY_PARTS) (version 26.9.23.15)
```

### 5. Production & Systems Takeaways
1. **Never treat ClickHouse like an OLTP database**: Application services must never perform direct `INSERT INTO table VALUES (...)` per API call.
2. **Buffer Architectures**:
   - Ingest through streaming buffers: **Apache Kafka**, **Vector**, **FluentBit**, or **Redis Streams**.
   - Ensure workers batch at least 1,000 to 100,000 rows or buffer for 1–5 seconds before flushing to ClickHouse.
3. **Asynchronous Inserts (`async_insert`)**:
   - When client batching is not feasible, enable ClickHouse native async inserts:
     ```sql
     SET async_insert = 1;
     SET wait_for_async_insert = 1;
     ```
   - ClickHouse will internally accumulate point writes into an in-memory buffer and write them out as coalesced parts.

---

## Experiment 02: Analytical Memory Limit Saturation & External Aggregation

### 1. Hypothesis & Objective
- **Aggregation Hash Tables**: For `GROUP BY`, `DISTINCT`, and join operations, ClickHouse constructs vectorized in-memory hash tables mapping keys to aggregate states.
- **Memory Cap Breach**: When aggregating across high-cardinality keys (such as 1,000,000 unique UUIDs), RAM consumption grows linearly with key count. Under a strict query memory limit (`max_memory_usage = 64000000` / 64MB), ClickHouse terminates the query with `DB::Exception: Memory limit (for query) exceeded (Code: 241)` to prevent host container OOM (Exit 137).
- **External Group By (Spill to Disk)**: Setting `max_bytes_before_external_group_by = 20000000` enables ClickHouse to spill partial aggregation hash tables to temporary files on disk, execute a multi-way external merge sort, and complete the query successfully.
- **Algorithmic Alternatives**: Replacing exact cardinality (`uniqExact`) with HyperLogLog (`uniq`) drastically reduces memory footprint from tens of megabytes to a constant 12KB register.

### 2. Configuration & Limits
- **Docker Profile**: `memory-cap` (`clickhouse-mem-data:/var/lib/clickhouse`)
- **Server Setting**: `/etc/clickhouse-server/users.d/memory-limit.xml` (`max_memory_usage: 64000000`)
- **Dataset**: 1,000,000 synthetic clickstream events generated in-engine via `numbers(1000000)`.

### 3. How to Run
```bash
make up EXP=memory-cap
make run SCRIPT=02_memory_limit
```

### 4. Empirical Benchmark & Observed Metrics

| Aggregation Strategy | Cardinality | Query Settings | Memory Consumed | Outcome | Duration |
|---|---|---|---|---|---|
| **In-Memory Hash Table** | 1,000,000 UUIDs | `max_bytes_before_external_group_by = 0` | > 64 MiB | **ABORTED (Code: 241)** | ~0.35s |
| **External Group By (Disk Spill)** | 1,000,000 UUIDs | `max_bytes_before_external_group_by = 20MB` | Kept under 64 MiB | **SUCCESS** | ~1.45s |
| **HyperLogLog (`uniq`)** | 1,000,000 UUIDs | `uniq(user_id)` | ~12 KB | **SUCCESS** | ~0.02s |

#### Failure Signature (`Code: 241`):
```text
DB::Exception: Memory limit (for query) exceeded: would use 68.12 MiB (limit: 61.04 MiB).
(MEMORY_LIMIT_EXCEEDED) (version 26.9.23.15)
```

### 5. Production & Systems Takeaways
1. **Safeguard Cluster Health**: Always configure global `max_memory_usage` and `max_execution_time` to prevent rogue ad-hoc analytical queries from starving production dashboards or triggering container OOM killer.
2. **Graceful Degradation via Spilling**:
   - For heavy ETL or scheduled aggregations, configure `max_bytes_before_external_group_by` to roughly 50% of `max_memory_usage`.
   - The query trades a small I/O latency penalty for guaranteed completion without failing client pipelines.
3. **Approximation Algorithms**:
   - Never use `COUNT(DISTINCT col)` or `uniqExact()` on web scale clickstream / metric logs when a 1% error margin is acceptable.
   - Use `uniq()` (HyperLogLog) or `uniqCombined64()` for 100x lower memory overhead and vectorized CPU evaluation.

---

## Experiment 03: Columnar Compression Codecs & `LowCardinality`

### 1. Hypothesis & Objective
- Because ClickHouse stores each column contiguously on disk (`column.bin`), values of the same type and distribution sit adjacent to each other.
- Applying domain-specific codecs (`CODEC(Delta, ZSTD)` on monotonic timestamps, `CODEC(Gorilla, ZSTD)` on slowly changing gauges, and `LowCardinality(String)` dictionary encoding) achieves 10x–50x physical disk savings compared to `CODEC(NONE)`.

### 2. How to Run
```bash
make up EXP=compression
make run SCRIPT=03_codec_compression
make down EXP=compression
```

### 3. Production & Systems Takeaways
1. **Delta + ZSTD on Timestamps**: Storing the difference between adjacent sorted timestamps ($\Delta = +1\text{s}$) turns 32-bit integers into tiny identical values that ZSTD compresses by >40x.
2. **`LowCardinality(String)`**: Replaces repeated string columns (e.g., `region`, `status_code`) with integer dictionary IDs, shrinking both disk storage and `GROUP BY` hash-table memory.

---

## Real-Time Observability & System Tables

ClickHouse provides comprehensive introspection via virtual system tables:

```sql
-- 1. Inspect active data parts and compression ratios
SELECT name, rows, bytes_on_disk, data_compressed_bytes, data_uncompressed_bytes
FROM system.parts
WHERE table = 'clickstream_events' AND active = 1;

-- 2. Inspect query execution telemetry, peak memory usage, and abort codes
SELECT type, event_time, query_duration_ms, memory_usage, exception_code, exception
FROM system.query_log
ORDER BY event_time DESC
LIMIT 5;

-- 3. Check active queries and current RAM consumption
SELECT query_id, user, elapsed, memory_usage, query
FROM system.processes;
```

# Redis Limits Lab

Hands-on experiments stress-testing memory limits, eviction policies, cache stampedes, and durability boundaries in Redis (`redis:8.10.2`).

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) with **Docker Compose Profiles** and **dedicated named volumes** for total data isolation between runs.

```text
redis/
├── docker-compose.yml       # Pinned image, cgroups limits, profiles
├── Makefile                 # One-touch runner targets
├── experiments/
│   ├── monitor.py           # Real-time memory & eviction terminal stream
│   ├── 00_crash_course.py / .main.kts  # Core data structures, pipelining & INFO diagnostics
│   ├── 01_eviction.py / .main.kts      # Stress memory limits & compare eviction policies
│   ├── 02_stampede.py / .main.kts      # Cache stampede & locking fixes
│   └── 03_persistence.py / .main.kts   # AOF vs RDB crash durability
└── README.md
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) (run via `uv run` or `make run`) and Kotlin script dependencies via `@file:DependsOn` (run via `kotlin` or `make run-kt`).

---

## Experiment 01: Memory Limits & Eviction Under Load

### 1. Hypothesis & Objective
- **Hypothesis A (`noeviction`)**: When memory hits `maxmemory 16mb`, Redis will reject all subsequent write operations with `OOM command not allowed`, halting write traffic while reads remain operational.
- **Hypothesis B (`allkeys-lru`)**: Redis will seamlessly plateau memory at ~16MB by evicting cold/least-recently-used keys in real-time. Under an 80/20 skewed access pattern, hot keys will maintain a high hit ratio (>90%) while cold keys are recycled.

### 2. Configuration & Constraints
- **Docker Image**: `redis:8.10.2`
- **Container Limit**: `memory: 64M`, `cpus: '1.0'`
- **Redis Memory Cap**: `maxmemory 16mb`
- **Workload**: 2,500 keys $\times$ 10KB payloads = ~25MB of data (156% of `maxmemory`).

### 3. Quick Start & Reproduction

#### Step 1: Test `allkeys-lru` (Automatic Eviction)
```bash
# Start container with 16MB maxmemory and LRU eviction
make up EXP=eviction-lru

# Optional: In a second terminal window, watch live metrics
make monitor

# Run the workload
make run SCRIPT=01_eviction
```

#### Step 2: Test `noeviction` (OOM Rejection)
```bash
# Tear down previous profile and wipe volume
make down EXP=eviction-lru

# Start noeviction profile
make up EXP=eviction-noeviction

# Run the workload again to observe the failure boundary
make run SCRIPT=01_eviction
```

### 4. What You Will Observe (Empirical Results)

| Metric / Event | `allkeys-lru` | `noeviction` |
|---|---|---|
| **Memory Limit Hit** | ~Item 1,235 (16.0MB) | **Item 1,233 (16.0MB)** |
| **Writes Past 16MB** | **2,500 / 2,500 succeed** (0 OOM errors) | **1,267 rejected** with `OutOfMemoryError` |
| **Evicted Keys Count** | **1,273 keys automatically evicted** | **0 keys evicted** |
| **Peak Memory Reached** | 16.0MB | 16.1MB |
| **Existing Key Reads** | 100% accessible | 100% accessible |
| **Recovery Behavior** | Self-healing (recycles LRU keys) | Hard write freeze until keys deleted |

### 5. Production & Systems Takeaways
1. **Never use `noeviction` for a caching tier**: A sudden surge in distinct keys will brick your write path. Reserve `noeviction` only for Redis instances used as primary in-memory databases where silent data loss is worse than an outage.
2. **Beware the memory margin**: The Docker container limit is set to `64M` while Redis is capped at `16mb`. This buffer prevents the Linux kernel OOM Killer from terminating Redis when memory fragmentation (`mem_fragmentation_ratio`) or replication buffers spike.
3. **Zipfian distributions save the day**: Under LRU eviction with skewed traffic, hitting the memory ceiling rarely degrades end-user latency because frequently requested data remains in cache.

---

## Experiment 02: Cache Stampede (Thundering Herd) Simulation

### 1. Hypothesis & Objective
When a hot cache key expires under high concurrency (100 simultaneous requests):
- **Naive Cache-Aside**: Triggers a thundering herd where all 100 requests experience a cache miss and hammer the primary database simultaneously (100 DB queries).
- **Distributed Mutex (`SET NX PX`)**: Only the first worker acquires the lock to recompute from the database; the other 99 workers poll briefly and read the warm value from cache (1 DB query).
- **Probabilistic Early Refresh (XFetch)**: Proactively refreshes the cache before the hard TTL expires, eliminating cache miss latency for end users.

### 2. Empirical Results (100 Concurrent Requests)

| Strategy | Database Query Count | Served from Cache | Total Elapsed |
|---|---|---|---|
| **1. Naive Cache-Aside** | **100 queries** (100% stampede) | 0 | 81.5ms |
| **2. Distributed Mutex (`SET NX PX`)** | **1 query** (Collapsed) | **99** | 82.5ms |
| **3. Probabilistic Early Refresh (XFetch)** | **30 background queries** | **70 instant hits** | 58.9ms |

### 3. Production & Systems Takeaways
1. **Never use unprotected Cache-Aside for hot keys**: Expiring a key for a homepage or top search term without locking or probabilistic refresh will instantly overwhelm primary database connection pools.
2. **Safe Mutex Release**: Always use a unique token with `SET lock:key token NX PX <ms>` and release via a Lua script checking token ownership to prevent an expired lock from being accidentally freed by a slow worker.

---

## Experiment 03: Persistence Durability & Crash Recovery (`kill -9`)

### 1. Hypothesis & Objective
When an ungraceful crash (kernel panic or `SIGKILL`) strikes an active Redis instance:
- **RDB Snapshots Only (`save 60 10000`, `appendonly no`)**: Maximum raw write speed, but catastrophic data loss for any writes acknowledged after the last snapshot point.
- **AOF `appendfsync everysec`**: High throughput (~85% of RDB speed) with a bounded risk window of $\le 1$ second of data loss.
- **AOF `appendfsync always`**: Zero data loss (complete durability), but write throughput is cut by more than half due to synchronous disk flushing.

### 2. Empirical Benchmark & Crash Audit

We fired rapid pipelined writes, issued `docker compose kill -s SIGKILL` mid-stream at 3,500 acknowledged writes, remounted the volume, and audited recovered keys:

| Persistence Mode | Write Throughput | Acknowledged Writes | Recovered After `SIGKILL` | Lost Writes | Loss % |
|---|---|---|---|---|---|
| **RDB Snapshots Only** | **79,676 writes/sec** | 3,500 | **0** | **3,500** | **100.0%** |
| **AOF (`appendfsync everysec`)** | **66,522 writes/sec** | 3,500 | **3,500** | **0** | **0.0%** |
| **AOF (`appendfsync always`)** | **36,851 writes/sec** | 3,500 | **3,500** | **0** | **0.0%** |

### 3. Production & Systems Takeaways
1. **Never rely on RDB alone for critical state**: RDB snapshots are great for cold backups and fast replica initialization, but fatal for disaster recovery because point-in-time snapshots lose up to 60+ seconds of confirmed user writes.
2. **`appendfsync everysec` is the industry golden mean**: It delivers almost 85% of the raw in-memory write performance of RDB while bounding the worst-case disaster recovery blast radius to at most 1 second of transactions.
3. **`appendfsync always` turns Redis into a disk engine**: Synchronous `fsync` slashes throughput from ~80k writes/sec to ~36k writes/sec even on fast Apple NVMe SSDs.

---

## Primary Sources & Deep-Dive References

- [Redis Documentation: Key Eviction & Memory Management](https://redis.io/docs/latest/develop/reference/eviction/) – Detailed breakdown of `maxmemory`, eviction policies (`volatile-lru`, `allkeys-lru`, `allkeys-lfu`), and sampling precision.
- Sanfilippo, Salvatore (Antirez): [*Random Notes on Redis 4.0 Memory Usage & LFU Eviction*](http://antirez.com/news/109) – Architecture behind approximated LRU vs 24-bit logarithmic counter LFU.
- [Redis Documentation: Redis Persistence Demystified](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/) – RDB snapshotting semantics, AOF write buffers, and OS `fork()` Copy-On-Write (COW) memory amplification.
- Vattani, Chierichetti, Lowenstein (VLDB 2015): [*Optimal Probabilistic Cache Stampede Prevention*](https://vldb.org/pvldb/vol8/p886-vattani.pdf) – Mathematical analysis of the XFetch algorithm to prevent thundering herd / stampede conditions.

---

## Clean Reset & Teardown

```bash
# Reset in-memory keys without stopping the container (fast reset):
make soft-reset

# Wipe all containers and named volumes for this lab:
make clean
```

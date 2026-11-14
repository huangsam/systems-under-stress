# Apache Kafka (KRaft) Limits & Internals Lab

Hands-on experiments stress-testing Kafka consumer group rebalance storms (`max.poll.interval.ms`), offset commit data loss (`enable.auto.commit=True`) and poison-pill head-of-line blocking, hot-key partition skew, and durability vs. availability tradeoffs (`acks=all` + `min.insync.replicas` vs. `acks=1` leader failover).

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) running Apache Kafka in **KRaft mode** (no ZooKeeper) with **Docker Compose Profiles** and **isolated named volumes** per profile.

```text
kafka/
├── docker-compose.yml                         # KRaft single-broker & 3-node multi-broker profiles
├── Makefile                                   # Runner targets (up, run, verify, soft-reset, down, clean)
├── experiments/
│   ├── 00_crash_course.py                     # KRaft connectivity, topic creation, produce/consume & offsets
│   ├── 01_rebalance_storms.py                 # max.poll.interval.ms breach vs. cooperative-sticky
│   ├── 02_offset_commit_pitfalls.py           # Auto-commit batch loss vs. manual commit + poison-pill DLQ
│   ├── 03_partition_skew_and_lag.py           # Hot-key partition skew vs. salted key parallelism
│   └── 04_acks_and_insync_replicas.py         # acks=all + min.insync.replicas=2 vs. acks=1 failover loss
└── README.md
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) and run via `uv run` or `make run`.

---

## Quickstart & Execution Standard

Across all profiles in this lab (see [`docker-compose.yml`](./docker-compose.yml) for exact image tags, JVM heap caps, cgroups limits, and healthchecks):

```bash
cd kafka

# 1. Start target profile with automatic healthcheck wait
make up EXP=single-broker

# 2. Run the baseline crash course or a stress experiment
make run SCRIPT=00_crash_course
make run SCRIPT=01_rebalance_storms

# 3. Fast in-engine topic cleanup or full profile teardown
make soft-reset
make down EXP=single-broker
make clean
```

---

## Experiment 00: Crash Course

Verifies KRaft broker connectivity, controller/broker metadata inspection, programmatic topic creation (3 partitions), keyed/unkeyed produce round-trips with delivery callbacks, consumer group polling, and watermark/committed offset validation (`00_crash_course`).

```bash
make up EXP=single-broker
make run SCRIPT=00_crash_course
make soft-reset
```

---

## Experiment 01: Consumer Group Rebalance Storms

### 1. Hypothesis & Objective
- **Liveness vs. Processing Budget**: Kafka's `max.poll.interval.ms` bounds the maximum time between consecutive `poll()` calls. If a consumer blocks longer than `max.poll.interval.ms`, the GroupCoordinator assumes the consumer is wedged and evicts it.
- **Hypothesis 1 (Eager Rebalance Storm)**: Under `range` assignment, evicting one slow consumer triggers a **stop-the-world** rebalance that revokes *all* partitions from healthy sibling consumers too. When the slow consumer finishes and polls again, it rejoins and triggers a second storm while uncommitted records are redelivered.
- **Hypothesis 2 (Cooperative Sticky + Bounded Work)**: Using `cooperative-sticky` assignment and keeping per-poll work within `max.poll.interval.ms` eliminates timeout storms; scaling out only moves the necessary subset of partitions incrementally.

### 2. Configuration & Limits
- **Compose Profile**: `single-broker` (`kafka-single-data:/var/lib/kafka/data` in [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `memory: 512M`, `cpus: '1.0'`, `KAFKA_HEAP_OPTS="-Xms128m -Xmx256m"`
- **Experiment Parameters**: 4-partition topic, `max.poll.interval.ms=3000` (Phase A with 3.8s stall) vs. `max.poll.interval.ms=10000` + `cooperative-sticky` (Phase B)

### 3. How to Run
```bash
make up EXP=single-broker
make run SCRIPT=01_rebalance_storms
make down EXP=single-broker
```

### 4. Observed Metrics & Failure Signatures

#### Failure Signature: `max.poll.interval.ms` Breach
```text
MAXPOLL|rdkafka#consumer-2: Application maximum poll interval (6500ms) exceeded by 370ms (adjust max.poll.interval.ms for long-running message processing): leaving group
```

#### Execution Comparison

| Phase / Strategy | Revocation Events (C2) | Partitions Revoked | Duplicate Deliveries | Outcome |
|---|---|---|---|---|
| **Phase A: `range` + 7.5s stall (`>6.5s` limit)** | 3 | 8 | 1 | **STOP-THE-WORLD STORM** |
| **Phase B: `cooperative-sticky` + bounded work** | 0 steady / 1 scale-out | 4 (incremental) | 0 | **INCREMENTAL (1 moved)** |

### 5. Production & Systems Takeaways
1. **Bound `poll()` Loop Work, Not Just Timeouts**: Raising `max.poll.interval.ms` delays failure detection; instead, lower `max.poll.records`, offload heavy I/O to bounded worker pools, or pause/resume partitions while draining.
2. **Default to `cooperative-sticky` Assignment**: Eager `range` / `roundrobin` protocols halt the entire consumer group during any membership change. Incremental cooperative rebalancing keeps unaffected partitions flowing.

---

## Experiment 02: Offset Commit Pitfalls & Poison-Pill DLQ

### 1. Hypothesis & Objective
- **Auto-Commit Decoupling**: `enable.auto.commit=True` commits the highest *polled* offset on a background timer (`auto.commit.interval.ms`), completely decoupled from whether application business logic actually finished processing the batch.
- **Hypothesis 1 (Silent Message Loss)**: Crashing mid-batch after `auto.commit.interval.ms` elapses advances the group's committed offset past unprocessed records, causing a replacement consumer to silently skip them.
- **Hypothesis 2 (Poison-Pill Head-of-Line Block)**: Switching to `enable.auto.commit=False` without a Dead Letter Queue (DLQ) causes a single malformed record to fail forever, blocking all subsequent offsets in the partition.
- **Hypothesis 3 (Bounded Retry + DLQ)**: Retrying up to a fixed cap and routing unrecoverable records to a DLQ topic with diagnostic headers preserves 0 data loss and unblocks partition progress.

### 2. Configuration & Limits
- **Compose Profile**: `single-broker` (`kafka-single-data:/var/lib/kafka/data` in [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `memory: 512M`, `cpus: '1.0'`
- **Experiment Parameters**: 1-partition topics, `auto.commit.interval.ms=200` (Phase A), 10-record stream with poison pill at offset 4 (Phases B & C)

### 3. How to Run
```bash
make up EXP=single-broker
make run SCRIPT=02_offset_commit_pitfalls
make down EXP=single-broker
```

### 4. Observed Metrics & Failure Signatures

#### Failure Signatures
```text
Phase A: Silently lost IDs (skipped by auto-commit): [3, 4, 5, 6, 7, 8, 9]
Phase B: Attempt 1..3: Poison pill id=4 raised ValueError -> offset NOT committed (tail_processed=0)
```

#### Execution Comparison

| Mode | Valid Processed | Lost / Redelivered | DLQ Routed | Outcome |
|---|---|---|---|---|
| **Phase A: `enable.auto.commit=True`** | 13/20 | **7 lost** (`id=3..9`) | 0 | **SILENT DATA LOSS** |
| **Phase B: Manual commit (no DLQ)** | 4/9 | **3x poison loop** (`id=4`) | 0 | **HEAD-OF-LINE BLOCK** |
| **Phase C: Manual commit + DLQ** | **9/9** | **0 lost** | **1** | **ZERO LOSS + UNBLOCKED** |

### 5. Production & Systems Takeaways
1. **Disable `enable.auto.commit` for At-Least-Once Processing**: Only commit offsets explicitly after downstream side effects (DB writes, HTTP calls) succeed, and make handlers idempotent.
2. **Every Partition Needs a Poison-Pill Escape Hatch**: Without bounded retries and a DLQ, one corrupt message halts an entire partition indefinitely.

---

## Experiment 03: Hot-Key Partition Skew & Consumer Group Lag

### 1. Hypothesis & Objective
- **Partition Affinity**: Kafka guarantees per-key ordering by hashing message keys (`murmur2`) to a single partition. A consumer group's parallelism is bounded by partition count, and each partition is owned by at most one consumer in the group.
- **Hypothesis 1 (Hot-Key Saturation)**: When 90% of traffic shares one hot key (`tenant:vip-1`), 1 partition and 1 consumer absorb the load and build lag while sibling consumers sit idle.
- **Hypothesis 2 (Key Salting)**: Appending a bounded salt (`tenant:vip-1#<salt>`) distributes traffic across all 4 partitions, engaging all 4 consumers in parallel and cutting drain time proportionally.

### 2. Configuration & Limits
- **Compose Profile**: `single-broker` (`kafka-single-data:/var/lib/kafka/data` in [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `memory: 512M`, `cpus: '1.0'`
- **Workload**: 200 messages across 4 partitions, 4 consumers in group, `20ms` simulated work per message

### 3. How to Run
```bash
make up EXP=single-broker
make run SCRIPT=03_partition_skew_and_lag
make down EXP=single-broker
```

### 4. Observed Metrics & Failure Signatures

| Strategy | Partition Distribution | Max Partition Load | Consumer Counts (4 workers) | Drain Time | Speedup |
|---|---|---|---|---|---|
| **Phase A: Hot Key (`tenant:vip-1`)** | `{0: 5, 1: 5, 2: 5, 3: 185}` | **185/200 (92%)** | `[5, 185, 5, 5]` | 4.85s | 1.00x |
| **Phase B: Salted Key (`#salt`)** | `{0: 50, 1: 50, 2: 50, 3: 50}` | **50/200 (25%)** | `[50, 50, 50, 50]` | **1.39s** | **3.48x** |

### 5. Production & Systems Takeaways
1. **Scaling Consumers Cannot Fix Key Skew**: Adding more partitions or consumer instances leaves hot-key lag unchanged because all records for that key still serialize through one partition.
2. **Use Key Salting When Strict Global Key Order Isn't Required**: Salting keys trades strict per-tenant ordering for horizontal throughput across partitions.

---

## Experiment 04: `acks=all` + `min.insync.replicas` vs. `acks=1` Failover Loss

### 1. Hypothesis & Objective
- **Durability vs. Availability (CAP)**:
  - **Hypothesis 1 (`acks=all` + `min.insync.replicas=2`)**: With `replication_factor=2` and `min.insync.replicas=2`, killing 1 replica shrinks ISR to 1 (`< 2`). Kafka immediately rejects writes with `NOT_ENOUGH_REPLICAS` rather than accepting under-replicated writes that could be lost.
  - **Hypothesis 2 (`acks=1` + Leader Failover)**: With `acks=1`, the leader acknowledges writes as soon as they hit its local log. If followers are lagging/paused and the leader receives `SIGKILL` before replicating, unclean leader election promotes a stale follower and permanently loses acknowledged writes.

### 2. Configuration & Limits
- **Compose Profile**: `multi-broker` (3 KRaft nodes: `kafka-1`, `kafka-2`, `kafka-3` with isolated `kafka-1-data`, `kafka-2-data`, `kafka-3-data` volumes in [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `memory: 512M`, `cpus: '0.75'`, `KAFKA_HEAP_OPTS="-Xms128m -Xmx256m"` per broker
- **Cluster Tunings**: `KAFKA_REPLICA_LAG_TIME_MAX_MS=3000`, `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=3`, `KAFKA_LOG_CLEANER_DEDUPE_BUFFER_SIZE=16777216`

### 3. How to Run
```bash
make up EXP=multi-broker
make run SCRIPT=04_acks_and_insync_replicas
make down EXP=multi-broker
```

### 4. Observed Metrics & Failure Signatures

#### Failure Signature: Under-Replicated Rejection (`acks=all`)
```text
Write attempt under ISR=1: acked=0/5, errors=5
Captured failure signature: Broker: Not enough in-sync replicas
```

#### Durability vs. Availability Comparison

| Configuration | Failure Injected | Write Behavior | Acknowledged Loss |
|---|---|---|---|
| **`acks=all`, `min.insync.replicas=2`** | Follower `SIGKILL` (`ISR=1 < 2`) | **REJECTED** (`Broker: Not enough in-sync replicas`) | **0/15 lost** (after recovery) |
| **`acks=1`, `unclean.leader.election=true`** | Leader `SIGKILL` before replication | **ACCEPTED** (`15/15` acked) | **15 acked writes LOST** (`5/20` surviving) |

### 5. Production & Systems Takeaways
1. **Pair `acks=all` with `min.insync.replicas=2` and `replication_factor >= 3`**: `RF=2, min.insync.replicas=2` guarantees durability but sacrifices availability on a single broker failure; `RF=3, min.insync.replicas=2` tolerates 1 broker failure with zero data loss and full write availability.
2. **Never Use `acks=1` for Durable Events**: `acks=1` silently loses acknowledged records during abrupt leader crashes or unclean failovers.

---

## Primary Sources & Deep-Dive References

- [Apache Kafka Documentation: KRaft Mode](https://kafka.apache.org/documentation/#kraft)
- [Apache Kafka Documentation: Producer & Topic Durability Configs (`acks`, `min.insync.replicas`)](https://kafka.apache.org/documentation/#producerconfigs_acks)
- [KIP-429: Kafka Consumer Incremental Rebalance Protocol (`cooperative-sticky`)](https://cwiki.apache.org/confluence/display/KAFKA/KIP-429%3A+Kafka+Consumer+Incremental+Rebalance+Protocol)

---

## Clean Reset & Teardown

```bash
# Fast topic cleanup without restarting brokers
make soft-reset

# Stop and wipe a specific profile's volumes
make down EXP=single-broker
make down EXP=multi-broker

# Wipe all profiles and volumes in this lab
make clean
```

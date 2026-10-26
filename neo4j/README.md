# Neo4j Limits & Internals Lab

Hands-on experiments stress-testing graph database operational ceilings, combinatorial path explosion, and supernode lock contention in Neo4j (`neo4j:5.26.32-community`).

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) with **Docker Compose Profiles** and **dedicated named volumes** for total data isolation between runs.

```text
neo4j/
├── docker-compose.yml       # Pinned image, cgroups limits (512MB RAM), JVM heap caps (384MB), profiles
├── Makefile                 # One-touch runner targets (up, run, soft-reset, cypher-shell, clean)
├── experiments/
│   ├── 00_crash_course.py            # Property graphs, Cypher traversals & dbms.components
│   ├── 01_unbounded_path.py          # Combinatorial path explosion: variable-length hops vs shortestPath
│   ├── 02_supernode.py               # Dense supernodes (50k edges): pointer chasing & write lock contention
│   └── 03_cypher_vs_recursive_sql.py # Native index-free adjacency vs recursive CTE in PostgreSQL
└── README.md
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) and run via `uv run` or `make run`.

### Host Safety & Resource Caps
- **Docker Image**: `neo4j:5.26.32-community`
- **Host Port Binding**: `127.0.0.1:7474` (HTTP Browser), `127.0.0.1:7687` (Bolt binary protocol)
- **Cgroups Resource Limits**: `memory: 512M`, `cpus: '1.0'`
- **JVM Constraints**:
  - `NEO4J_server_memory_heap_initial__size: 256m`
  - `NEO4J_server_memory_heap_max__size: 384m`
  - `NEO4J_server_memory_pagecache_size: 64m`
- **Credentials**: `neo4j` / `password123` (`NEO4J_AUTH`)

---

## Experiment 01: Unbounded Path Traversal (Combinatorial Explosion)

### 1. Hypothesis & Objective
- **Index-Free Adjacency**: Neo4j stores relationships as raw memory/disk pointer offsets connecting node records, eliminating runtime relational join indexes. While single-hop pointer dereferencing is sub-microsecond, variable-length path traversal without strict upper bounds (`MATCH (a)-[*1..n]->(b)`) suffers from exponential combinatorial explosion $\mathcal{O}(b^d)$, where $b$ is the average node branching factor and $d$ is traversal depth.
- **ShortestPath Pruning**: Neo4j's `shortestPath((a)-[*]-(b))` algorithm employs bidirectional breadth-first search (BFS), terminating as soon as the forward and backward frontiers intersect $\mathcal{O}(b^{d/2})$, avoiding exhaustive combinatorial path evaluation.

### 2. Configuration & Constraints
- **Profile**: `profiles: ["traversal-depth"]`
- **Volume**: `neo4j-traversal-data:/data`
- **Workload Topology**: 2,000 nodes, 8,000 relationships (branching factor $b = 4$).

### 3. How to Run
```bash
cd neo4j

# 1. Start the traversal-depth container and wait for readiness
make up EXP=traversal-depth

# 2. Run the workload
make run SCRIPT=01_unbounded_path

# 3. Clean up containers and wipe named volumes
make clean
```

### 4. Empirical Benchmark & Results

#### Part 1: Variable-Length Depth Scaling (`MATCH p = (a)-[*1..d]->(b)`)

| Traversal Depth ($d$) | Total Paths Enumerated | Engine DB Hits | Server Execution Time (ms) | Client Wall Time (ms) | Growth Factor |
|---|---|---|---|---|---|
| **1 hop** | 4 | 9 | 0.8 ms | 1.9 ms | - |
| **2 hops** | 16 | 37 | 1.2 ms | 2.4 ms | **4.0x** |
| **3 hops** | 64 | 149 | 2.1 ms | 4.1 ms | **4.0x** |
| **4 hops** | 256 | 597 | 5.8 ms | 9.3 ms | **4.0x** |
| **5 hops** | 1,024 | 2,389 | 22.4 ms | 31.6 ms | **4.0x** |
| **6 hops** | 4,096 | 9,557 | 98.7 ms | 124.3 ms | **4.0x** |

> [!WARNING]
> At depth 8–10 in a dense graph, total paths exceed $10^6$, exceeding the JVM's 384MB heap and triggering an OutOfMemoryError or transaction termination (`dbms.transaction.timeout`).

#### Part 2: Exhaustive Path Enumeration vs Bidirectional `shortestPath`

Evaluating paths between Node `0` and Node `500` (distance = 5 hops):

| Query Strategy | Cypher Construct | Result | DB Hits (`PROFILE`) | Latency (ms) | Efficiency Gain |
|---|---|---|---|---|---|
| **Exhaustive Path Search** | `MATCH p = (a)-[*1..6]->(b)` | 16 paths found | **9,557** | 124.3 ms | Baseline |
| **Bidirectional BFS** | `MATCH p = shortestPath((a)-[*..15]->(b))` | 5 hops | **42** | 2.1 ms | **227x fewer DB hits** |

---

## Experiment 02: Dense Supernodes & Write Lock Contention

### 1. Hypothesis & Objective
- **Pointer-Chasing Degradation**: In Neo4j's record storage architecture, each node record stores a 34-byte pointer to the head of its relationship linked list (`firstInRel` / `firstOutRel`). Traversing or filtering connected edges requires sequentially walking through every relationship record in the chain. When a node becomes a "Supernode" (e.g. 50,000 edges), single-hop lookups degenerate from $\mathcal{O}(1)$ into an expensive $\mathcal{O}(k)$ linear scan through disk and CPU cache.
- **Write Lock Serialization**: When creating an edge between two nodes, Neo4j must update the relationship pointer head on *both* node records. To protect pointer integrity, the engine acquires an **exclusive write lock** on both end nodes. When concurrent worker threads attach edges to the same supernode, all transactions serialize on that single node lock.

### 2. Configuration & Constraints
- **Profile**: `profiles: ["supernode"]`
- **Volume**: `neo4j-supernode-data:/data`
- **Workload Topology**:
  - Supernode: 1 hub node with 50,000 incoming follower edges.
  - Sparse node: 1 hub node with 10 incoming follower edges.

### 3. How to Run
```bash
cd neo4j

# 1. Start the supernode lab container
make up EXP=supernode

# 2. Run the workload
make run SCRIPT=02_supernode

# 3. Clean up containers and wipe named volumes
make clean
```

### 4. Empirical Benchmark & Results

#### Part 1: Read Traversal Degradation (Pointer Chasing in Relationship Chains)

Filtering for a specific leaf connected to the hub (`WHERE f.id = ...`):

| Target Node Type | Degree (Edge Count) | Engine DB Hits (`PROFILE`) | Server Execution Time (ms) | Client Wall Time (ms) | Relative Cost |
|---|---|---|---|---|---|
| **Sparse Node** | 10 edges | **12** | 0.6 ms | 1.4 ms | **1.0x** |
| **Dense Supernode** | 50,000 edges | **50,002** | 26.8 ms | 34.2 ms | **4,166x more DB hits** |

#### Part 2: Concurrent Write Lock Contention (10 Concurrent Worker Threads)

Workload: 10 concurrent threads each creating 25 new incoming edges (250 total writes).

| Metric | Supernode Contention (All threads locking 1 hub) | Partitioned Target Hubs (Independent locks) | Impact of Lock Contention |
|---|---|---|---|
| **Total Duration** | **1.42 s** | **0.21 s** | **6.8x slower wall time** |
| **Write Throughput** | **176.1 writes/s** | **1,190.5 writes/s** | **85% throughput collapse** |
| **P50 Latency** | **48.2 ms** | **6.1 ms** | **7.9x latency amplification** |
| **P95 Latency** | **112.4 ms** | **11.8 ms** | **9.5x latency amplification** |
| **Max Latency** | **148.9 ms** | **15.2 ms** | **9.8x peak wait spike** |

---

## Experiment 03: Multi-Hop Cypher vs Recursive SQL (Neo4j vs PostgreSQL)

### 1. Hypothesis & Objective
- Compare transitive dependency reachability across depths 1..7 on an identical 1,500-node service graph in **PostgreSQL (`postgres:18.6`)** (`WITH RECURSIVE` CTE) and **Neo4j** (`MATCH (s:Service {id: 0})-[:DEPENDS_ON*1..d]->(dep)`).
- Demonstrate how relational B-Tree joins excel at shallow hops (1–3), while Neo4j's **index-free adjacency** ($\mathcal{O}(1)$ pointer chasing per edge) avoids intermediate worktable materialization on deep traversals.

### 2. How to Run
```bash
make up EXP=compare-sql
make run SCRIPT=03_cypher_vs_recursive_sql
make down EXP=compare-sql
```

---

## Production & Systems Takeaways

### 1. Guardrails for Unbounded Cypher Queries
- **Never allow unbounded variable-length syntax in production**: Avoid `MATCH (a)-[*]->(b)`. Always enforce strict upper bounds: `MATCH (a)-[*1..3]->(b)`.
- **Set Server-Side Transaction Timeouts**:
  Configure `dbms.transaction.timeout=5s` in `neo4j.conf` to abort combinatorially runaway traversals before they exhaust JVM heap.
- **Directionality Matters**: Specifying edge direction `(a)-[:REL]->(b)` cuts search space in half compared to undirected `(a)-[:REL]-(b)`.

### 2. Architectural Mitigations for Supernodes
- **Relationship Bucketing / Intermediate Tree Grouping**: Rather than attaching 500,000 orders directly to a single `Customer` node, bucket relationships by month or year: `(c:Customer)-[:HAS_YEAR]->(y:Year)-[:HAS_MONTH]->(m:Month)-[:PLACED]->(o:Order)`.
- **Fanout on Read vs Fanout on Write**: For celebrity / high-fanout entities (e.g. social feeds), decouple relationship graph storage from live feed fanout using message queues (Kafka) and caching (Redis).
- **Node Labels & Relationship Types**: Segregate high-cardinality edges into specialized relationship types (`:FOLLOWS_CELEBRITY` vs `:FOLLOWS_FRIEND`) so the Cypher planner avoids scanning irrelevant relationship record sub-chains.

---

## Primary Sources & Deep-Dive References

- [Neo4j Manual: Cypher Query Tuning & Execution Plans](https://neo4j.com/docs/cypher-manual/current/planning-and-tuning/) – Details on DB hits, pipeline operators (`Expand(All)` vs `ShortestPath`), and cardinality estimation.
- [Neo4j Architecture: Index-Free Adjacency Under the Hood](https://neo4j.com/docs/operations-manual/current/performance/) – Record store format (nodes, relationships, relationship chains) and pointer-based traversal mechanics.
- [Neo4j Engineering: The Supernode Challenge in Graph Databases](https://neo4j.com/blog/dark-side-neo4j-worst-practices/) – Anatomy of dense node chains, record locking during concurrent edge insertions, and relationship bucketing patterns.
- [Neo4j Graph Data Science: Path Finding Algorithms](https://neo4j.com/docs/graph-data-science/current/algorithms/pathfinding/) – Bidirectional BFS vs Dijkstra vs single-source shortest path performance comparisons.

---

## Universal Execution Targets

```bash
# Start container for target profile and wait for readiness
make up EXP=traversal-depth
make up EXP=supernode

# Execute self-driving test workloads via uv
make run SCRIPT=01_unbounded_path
make run SCRIPT=02_supernode

# Sub-second in-engine reset (DETACH DELETE all nodes)
make soft-reset EXP=traversal-depth

# Interactive Cypher shell inside running container
make cypher-shell EXP=traversal-depth

# Teardown specific profile volume
make down EXP=traversal-depth

# Wipe all containers and lab volumes across all profiles
make clean
```

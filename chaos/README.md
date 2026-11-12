# Network Chaos & Fault Injection Lab (Toxiproxy)

Hands-on experiments exploring **network-level gray failures**—latency jitter, connection pool starvation, thundering herd retry storms, and silent TCP socket hangs—using [Shopify Toxiproxy](https://github.com/shopify/toxiproxy) and an upstream Redis datastore.

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) with **Docker Compose Profiles** routing TCP traffic through a programmable L4/L7 fault-injection proxy (`toxiproxy`) in front of an upstream datastore (`redis-upstream`):

```text
chaos/
├── docker-compose.yml                     # Base anchor &chaos-base, Toxiproxy + Redis upstream, limits
├── Makefile                               # One-touch runner targets (up, run, soft-reset, down, clean, logs)
├── experiments/
│   ├── toxi_client.py                     # Zero-dependency Toxiproxy HTTP control plane client
│   ├── 00_crash_course.py                 # Control plane verification, latency jitter & bandwidth toxics
│   ├── 01_pool_starvation.py              # Downstream latency spike vs upstream connection pool exhaustion
│   ├── 02_retry_storms_vs_backoff.py      # TCP reset_peer outage: naive retry storm vs Full Jitter backoff
│   └── 03_half_open_tcp_hangs.py          # Silent black-hole packet drop vs explicit socket read timeouts
└── README.md
```

```text
  +-------------------------------------------------------------------------+
  | Host (127.0.0.1)                                                        |
  |                                                                         |
  |  +--------------------+             +--------------------------------+  |
  |  | Python Workload    |             | Toxiproxy HTTP Control Plane   |  |
  |  | (PEP 723 / uv run) |             | (127.0.0.1:8474)               |  |
  |  +---------+----------+             +---------------+----------------+  |
  |            |                                        |                   |
  |            | TCP Traffic                            | Inject Toxics     |
  |            v                                        v (latency/resets)  |
  |  +-------------------------------------------------------------------+  |
  |  | Toxiproxy Proxy Port (127.0.0.1:26379)                            |  |
  |  +----------------------------------+--------------------------------+  |
  +-------------------------------------|-----------------------------------+
                                        | Internal Docker Network
                                        v
                       +---------------------------------+
                       | Redis Upstream Container        |
                       | (redis-upstream:6379)           |
                       +---------------------------------+
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) and run via `uv run` or `make run`.

---

## Quickstart & Execution Standard

Across all experiments in this lab (see [`docker-compose.yml`](./docker-compose.yml) for exact image tags, port mappings, and healthchecks):

```bash
cd chaos

# 1. Start the Toxiproxy proxy and upstream Redis containers
make up EXP=chaos

# 2. Run the baseline crash course or a fault-injection experiment
make run SCRIPT=00_crash_course
make run SCRIPT=01_pool_starvation

# 3. Fast reset (clear all active toxics and flush upstream keys) or full teardown
make soft-reset
make down EXP=chaos
make clean
```

---

## Experiment 00: Crash Course

Verifies Toxiproxy HTTP control plane connectivity (`127.0.0.1:8474`), proxy initialization (`127.0.0.1:26379` $\rightarrow$ `redis-upstream:6379`), sub-millisecond baseline RTT, dynamic latency/jitter toxic injection, bandwidth throttling (`30 KB/s`), and instant recovery upon toxic removal.

```bash
make up EXP=chaos
make run SCRIPT=00_crash_course
make soft-reset
```

---

## Experiment 01: Connection Pool Starvation Under Downstream Latency Spikes

### 1. Hypothesis & Objective
- Demonstrates Little's Law in connection pooling ($L = \lambda \times W$): the number of concurrently held connections ($L$) equals arrival rate ($\lambda$) multiplied by average response time ($W$).
- When downstream network latency jumps from `1ms` to `200ms`, connection holding time increases 200x.
- Even though the upstream database is healthy and returning zero errors, a bounded client connection pool rapidly saturates, causing queued caller threads to breach their pool acquisition timeout.

### 2. Configuration & Limits
- **Compose Profile**: `chaos` (`toxiproxy` and `redis-upstream`, each capped at `memory: 64M`, `cpus: '0.5'` in [`docker-compose.yml`](./docker-compose.yml))
- **Client Pool Constraints**: `BlockingConnectionPool(max_connections=5, timeout=0.08)` (80ms acquisition timeout)
- **Fault Injected**: `latency` toxic (`latency=200ms`, `jitter=20ms`) across 15 concurrent worker threads.

### 3. How to Run
```bash
make up EXP=chaos
make run SCRIPT=01_pool_starvation
make soft-reset
```

### 4. Observed Metrics & Failure Signatures

| Scenario | Downstream RTT | Pool Capacity | Concurrent Workers | Succeeded | Aborted (`ConnectionError`) |
|---|---|---|---|---|---|
| **1. Healthy Baseline** | ~1 ms | 5 connections | 15 threads | **15 / 15 (100%)** | 0 |
| **2. Latency Spike (`+200ms`)** | ~200 ms | 5 connections | 15 threads | **5 / 15 (33%)** | **10 / 15 (`No connection available.`)** |

#### Failure Signature:
```text
redis.exceptions.ConnectionError: No connection available.
```

### 5. Production & Systems Takeaways
1. **Downstream Latency Becomes Upstream Resource Exhaustion**: Never size connection pools or worker thread pools assuming happy-path latency. A slow downstream dependency holds connections 100x longer, starving all callers sharing that pool.
2. **Fail Fast & Bulkhead Isolation**: Pair bounded acquisition timeouts with circuit breakers and isolated connection pools per criticality tier so a slow secondary query cannot starve critical checkout or auth flows.

---

## Experiment 02: Transient Outage Mitigation (Naive Retry Storms vs Full Jitter)

### 1. Hypothesis & Objective
- Evaluates client retry behavior during a transient `0.8s` network link failure (`reset_peer` toxic sending TCP `RST` packets).
- **Strategy A (Naive Immediate Retry)**: 25 concurrent clients retry immediately in a tight loop upon `ConnectionError`, firing a synchronized burst of 125 requests in `<100ms` and exhausting all retry budgets before the link recovers.
- **Strategy B (Exponential Backoff with Full Jitter)**: Applies the AWS Architecture sleep formula:
  $$\text{sleep} = \text{Uniform}\left(0, \min\left(\text{cap}, \text{base} \times 2^{\text{attempt}}\right)\right)$$
  Retries desynchronize across time, flatten the burst spike, and achieve 100% task completion once the link heals.

### 2. Configuration & Limits
- **Compose Profile**: `chaos` (see [`docker-compose.yml`](./docker-compose.yml))
- **Fault Injected**: `reset_peer` toxic active for `800ms`, then automatically removed
- **Workload**: 25 concurrent workers, `max_retries = 4` (up to 5 attempts per worker)

### 3. How to Run
```bash
make up EXP=chaos
make run SCRIPT=02_retry_storms_vs_backoff
make soft-reset
```

### 4. Observed Metrics & Failure Signatures

| Retry Strategy | Concurrent Workers | Total Network Attempts Fired | Succeeded Tasks | Failed Tasks | Recovery Rate |
|---|---|---|---|---|---|
| **Strategy A: Naive Immediate Retry** | 25 | **125 (5.0x burst storm in <50ms)** | 0 / 25 | **25 / 25** | **0.0%** |
| **Strategy B: Exponential Backoff + Full Jitter** | 25 | ~65 (Desynchronized over 1.2s) | **25 / 25** | **0 / 25** | **100.0%** |

### 5. Production & Systems Takeaways
1. **Unjittered Retries are Self-Inflicted DDoS Attacks**: Even standard exponential backoff ($2^n$) without jitter keeps concurrent clients synchronized in lockstep waves that repeatedly crush a recovering database.
2. **Always Add Full Jitter + Retry Budgets**: Combine randomized Full Jitter with a client-wide retry token bucket (e.g., max 10% retry overhead) so total outage states fail fast instead of multiplying cluster load by 5x.

---

## Experiment 03: Silent Half-Open TCP Socket Hangs & Missing Read Timeouts

### 1. Hypothesis & Objective
- Simulates cloud packet black holes (AWS NAT Gateway idle state eviction, stateful firewall drops, or dead peer hosts) where packets are silently dropped without transmitting a `TCP RST` or `FIN`.
- **Client A (`socket_timeout=None`)**: Issues a command over an established socket and blocks indefinitely inside the kernel `recv()` syscall waiting for ACK/response bytes.
- **Client B (`socket_timeout=1.0s`)**: Enforces an explicit read timeout, detects the silent black hole in `1.0s`, raises `redis.exceptions.TimeoutError`, and cleanly releases the worker thread.

### 2. Configuration & Limits
- **Compose Profile**: `chaos` (see [`docker-compose.yml`](./docker-compose.yml))
- **Fault Injected**: `timeout` toxic with `timeout=0` (silently drops all packets on the established TCP stream without closing the socket)

### 3. How to Run
```bash
make up EXP=chaos
make run SCRIPT=03_half_open_tcp_hangs
make soft-reset
```

### 4. Observed Metrics & Failure Signatures

| Client Configuration | Socket Read Timeout | Behavior Under Silent Packet Drop | Thread Release Time | Outcome |
|---|---|---|---|---|
| **Client A (Naive Default)** | `socket_timeout=None` | Hangs indefinitely in kernel `socket.recv()` | **Hung (>15 min in Linux TCP)** | **THREAD LEAKED / FROZEN** |
| **Client B (Resilient)** | `socket_timeout=1.0` | Raises `TimeoutError` at deadline | **1.00 s** | **CLEAN FAIL-FAST RECOVERY** |

#### Failure Signature:
```text
redis.exceptions.TimeoutError: Timeout reading from socket
```

### 5. Production & Systems Takeaways
1. **`connect_timeout` Does Not Protect Established Connections**: A `connect_timeout` only bounds the initial 3-way `SYN / SYN-ACK / ACK` handshake. Once connected, a missing `socket_timeout` (read/write timeout) leaves threads vulnerable to 15-minute OS TCP retransmission hangs (`net.ipv4.tcp_retries2 = 15`).
2. **Mandatory Read Timeouts on Every Network Client**: Every HTTP, gRPC, Redis, and SQL driver in production must explicitly configure both connection establishment timeouts and per-operation read/write socket timeouts.

---

## Primary Sources & Deep-Dive References

- [Shopify Toxiproxy Documentation & Architecture](https://github.com/shopify/toxiproxy) – TCP proxy architecture and toxic specifications (`latency`, `bandwidth`, `reset_peer`, `timeout`, `slicer`).
- Brooker, Marc (AWS Architecture Blog, 2015): [*Exponential Backoff And Jitter*](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) – Mathematical simulation of Full Jitter, Equal Jitter, and Decorrelated Jitter under concurrent contention.
- Huang et al. (ACM HotOS 2017): [*Gray Failure: The Achilles' Heel of Cloud-Scale Systems*](https://www.microsoft.com/en-us/research/publication/gray-failure-the-achilles-heel-of-cloud-scale-systems/) – Formal characterization of partial network degradation, differential observability, and cascading pool exhaustion.
- [Linux Kernel Documentation: IP Sysctl (`tcp_retries2` & Keepalive)](https://docs.kernel.org/networking/ip-sysctl.html) – Why unacknowledged TCP sockets hang for 13–30 minutes before the kernel aborts a half-open connection.

---

## Clean Reset & Teardown

```bash
# Clear all active Toxiproxy toxics and flush upstream Redis keys:
make soft-reset

# Wipe all containers and networks for this lab:
make clean
```

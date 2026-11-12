# Network Chaos & Fault Injection Lab (Toxiproxy)

An empirical laboratory exploring **network-level gray failures**—latency jitter, connection pool starvation, thundering herd retry storms, and silent TCP socket hangs—using [Shopify Toxiproxy](https://github.com/shopify/toxiproxy) and an upstream Redis datastore.

---

## 1. Architectural Philosophy: Why Resource Limits Are Not Enough

In real-world cloud environments, production databases rarely fail via clean process crashes (`SIGKILL` or `docker stop`). Instead, over **70% of high-severity production incidents stem from partial network degradation**:
- Downstream latency jitter that silently exhausts upstream connection pools.
- Intermittent packet drops that trigger self-inflicted DDoS retry storms.
- Cloud NAT gateways or stateful firewalls silently dropping idle connections without sending `TCP RST` or `FIN` packets.

This lab simulates these dynamic conditions deterministically using Toxiproxy as a layer-7/layer-4 proxy sitting between Python clients and the upstream datastore.

---

## 2. Lab Topology & Port Mappings

```
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

- **Toxiproxy API**: `127.0.0.1:8474` (HTTP REST control plane to add/remove toxics dynamically)
- **Proxy Port**: `127.0.0.1:26379` (Client traffic target, proxied to upstream)
- **Redis Upstream**: Private Docker network only (no unconstrained host port exposure)

---

## 3. Quick Start (CLI Workflow)

```bash
cd chaos

# 1. Start the Toxiproxy and upstream Redis containers
make up EXP=chaos

# 2. Run the Crash Course or specific experiments
make run SCRIPT=00_crash_course
make run SCRIPT=01_pool_starvation
make run SCRIPT=02_retry_storms_vs_backoff
make run SCRIPT=03_half_open_tcp_hangs

# 3. Fast reset: wipe all active toxics and flush Redis keys
make soft-reset

# 4. Clean teardown: stop containers and unmount resources
make clean
```

---

## 4. Experiment Matrix

### Experiment 00: Crash Course (Toxiproxy Control Plane & Primitives)
- **Objective**: Verify Toxiproxy proxy initialization, baseline latency (<1ms), dynamic latency/jitter toxic injection, bandwidth throttling (30 KB/s limit), and instant post-reset recovery.
- **Run**: `make run SCRIPT=00_crash_course`

---

### Experiment 01: Connection Pool Starvation Under Downstream Latency Spikes
- **Objective**: Demonstrates Little's Law in connection pooling ($L = \lambda \times W$). When downstream latency jumps from 1ms to 200ms, connection holding time jumps 200x.
- **Constraints**: 15 concurrent worker threads, pool limited to 5 connections (`max_connections=5`), pool acquisition timeout = 80ms (`timeout=0.08`).
- **Observed Failure**: First 5 workers acquire the pool; remaining 10 workers queue up, breach the 80ms pool timeout, and abort with `ConnectionError: No connection available.` even though Redis itself never returned an error.
- **Run**: `make run SCRIPT=01_pool_starvation`

---

### Experiment 02: Transient Outage Mitigation - Naive Retry Storms vs Full Jitter
- **Objective**: Evaluates system behavior during a 0.8-second transient link outage (TCP `reset_peer`).
- **Observed Comparison**:
  - **Strategy A (Naive Immediate Retry)**: 25 clients retry immediately in a tight loop; 125 requests are fired in <100ms, exhausting all retry budgets before the link heals (100% failure rate).
  - **Strategy B (Exponential Backoff with Full Jitter)**: AWS Architecture Center formula $\text{sleep} = \text{Uniform}(0, \min(\text{cap}, \text{base} \times 2^{\text{attempt}}))$. Retries desynchronize across time, flatten the burst spike, and achieve 100% recovery once the link recovers.
- **Run**: `make run SCRIPT=02_retry_storms_vs_backoff`

---

### Experiment 03: Silent Half-Open TCP Socket Hangs & Missing Read Timeouts
- **Objective**: Simulates cloud packet black holes (AWS NAT Gateway idle timeouts, firewall state table expirations) where packets are silently dropped without TCP `RST` or `FIN`.
- **Observed Failure**:
  - **Client A (`socket_timeout=None`)**: Thread hangs indefinitely in kernel `socket.recv()`. In production, Linux TCP retransmission defaults (`tcp_retries2=15`) freeze the thread for **15 to 20 minutes**.
  - **Client B (`socket_timeout=1.0s`)**: Thread detects silent drop in exactly 1.0s and throws `redis.exceptions.TimeoutError`, cleanly recycling worker resources.
- **Run**: `make run SCRIPT=03_half_open_tcp_hangs`

---

## 5. Production & Systems Takeaways

1. **Downstream Latency Becomes Upstream Starvation**:
   Never size connection pools in isolation. When dependencies get slow, client thread pools and connection queues saturate exponentially.
2. **Always Use Full Jitter on Retries**:
   Unjittered retries cause synchronized waves (thundering herds) that repeatedly re-crash recovering databases.
3. **Mandatory Socket Read Timeouts**:
   Never rely on TCP connection termination for error handling. Always set explicit `socket_timeout` alongside `connect_timeout`.

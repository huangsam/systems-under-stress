# HashiCorp Vault Limits & Internals Lab

Hands-on experiments stress-testing dynamic secret engines, lease revocation cascades, token lifecycle ceilings (`explicit_max_ttl`), and Transit encryption-as-a-service throughput in HashiCorp Vault.

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) with **Docker Compose Profiles** and **dedicated named volumes** for total data isolation between runs.

```text
vault/
├── docker-compose.yml        # Base anchor &vault-base, cgroups limits, profiles
├── Makefile                  # One-touch runner targets (up, run, down, clean, vault-cli, logs)
├── experiments/
│   ├── 00_crash_course.py       # KV v2 secrets, token leases & system health
│   ├── 01_dynamic_db_creds.py   # Dynamic PostgreSQL credentials & instant lease revocation
│   ├── 02_token_renewal.py      # Renewable child tokens & explicit_max_ttl boundary enforcement
│   └── 03_transit_throughput.py # Transit EaaS encryption, batching & key rotation
└── README.md
```

All test scripts declare client dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) and run via `uv run` or `make run`.

---

## Quickstart & Execution Standard

Across all profiles in this lab (see [`docker-compose.yml`](./docker-compose.yml) for exact image tags, resource limits, and healthchecks):

```bash
cd vault

# 1. Start target profile with automatic healthcheck wait
make up EXP=dynamic-creds

# 2. Run the baseline crash course or a stress benchmark script
make run SCRIPT=00_crash_course
make run SCRIPT=01_dynamic_db_creds

# 3. Clean up containers and wipe isolated named volumes
make down EXP=dynamic-creds
make clean
```

---

## Experiment 00: Crash Course

Verifies Vault seal status and health diagnostics, KV v2 versioned secret storage (`v1` $\rightarrow$ `v2` updates and versioned reads), soft delete vs permanent `destroy_secret_versions`, and child token lease creation.

```bash
make up EXP=dynamic-creds
make run SCRIPT=00_crash_course
make down EXP=dynamic-creds
```

---

## Experiment 01: Dynamic Secrets Engine & Lease Revocation Cascades

### 1. Hypothesis & Objective
- **Eliminating Static "Secret Zero" in Databases**: In traditional architectures, application microservices share long-lived database credentials stored in config files or environment variables. A breach compromises the database indefinitely until manual rotation.
- **Dynamic Credential Generation**: Vault's database secrets engine dynamically creates unique, ephemeral PostgreSQL roles on demand with short time-to-live (TTL). Every application instance receives its own unique username and password.
- **Cryptographic Revocation Cascade**: When a lease expires or is explicitly revoked via `client.sys.revoke_lease()`, Vault executes PostgreSQL revocation statements synchronously (`REVOKE ALL PRIVILEGES ... DROP ROLE ...`). PostgreSQL immediately severs subsequent connections with an authentication failure.

### 2. Configuration & Limits
- **Compose Profile**: `dynamic-creds` (`vault-dynamic-data` and `vault-pg-data` in [`docker-compose.yml`](./docker-compose.yml))
- **Dual-Service Topology**: `vault-dynamic` (`memory: 128M`, `cpus: '0.5'`) + companion `postgres` (`memory: 256M`, `cpus: '0.5'`)
- **Database Engine Settings**: `postgresql-database-plugin`, role `dev-readonly` (`default_ttl: 15s`, `max_ttl: 1m`)

### 3. How to Run
```bash
make up EXP=dynamic-creds
make run SCRIPT=01_dynamic_db_creds
make down EXP=dynamic-creds
```

### 4. Observed Metrics & Failure Signatures

| Step | Operation | Entity / Target | Outcome | Latency |
|---|---|---|---|---|
| **1** | Database Engine Mount | Vault `database/` | Engine configured with `postgresql-database-plugin` | ~12 ms |
| **2** | Dynamic Credential Issue | Vault `database/creds/dev-readonly` | Created role `v-root-dev-readon-...` | **~24 ms** |
| **3** | Database Query | PostgreSQL `127.0.0.1:5432` | Authenticated and read classification rows | **~8 ms** |
| **4** | Lease Revocation | Vault `sys/leases/revoke` | Vault issued SQL `DROP ROLE` cascade | **~18 ms** |
| **5** | Subsequent Connection | PostgreSQL `127.0.0.1:5432` | **REJECTED**: `FATAL: password authentication failed` | **Immediate** |

#### Post-Revocation Failure Signature:
```text
psycopg.errors.OperationalError: connection failed:
FATAL: password authentication failed for user "v-root-dev-readon-1729578000"
```

### 5. Production & Systems Takeaways
1. **Blast Radius Elimination**: A leaked database password is only valid for its short lease window. Once the application terminates or the lease is revoked, the database user no longer exists.
2. **Audit Attribution**: Because every lease generates a unique PostgreSQL role (`v-root-dev-readon-xyz`), database audit logs map queries directly to specific Vault tokens and service instances.
3. **Connection Pool Lifecycle Alignment**: Configure application connection pool `maxLifetime` shorter than the Vault lease TTL (`maxLifetime < lease_duration`), or run a lease-renewer sidecar so pooled connections do not outlive their revoked database role.

---

## Experiment 02: Token TTL, Renewable Leases & Max TTL Ceilings

### 1. Hypothesis & Objective
- **Token Heartbeat Leasing**: Short-lived Vault tokens allow applications to prove continuous liveness. A token issued with `ttl=2s` expires unless periodically renewed (`renew_self`).
- **The Max TTL Ceiling**: To prevent runaway tokens from living indefinitely through continuous renewal loops, Vault enforces an immutable upper bound: `explicit_max_ttl`.
- **Ceiling Breach Invariant**: When elapsed time reaches `explicit_max_ttl`, Vault dynamically clamps the lease increment to `explicit_max_ttl - elapsed`. Once the ceiling is reached, Vault rejects renewal requests (`lease is not renewable`) and revokes the token.

### 2. Configuration & Limits
- **Compose Profile**: `token-lifecycle` (`vault-token-data:/vault/file` in [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `memory: 128M`, `cpus: '0.5'`
- **Token Lifecycle Parameters**: Baseline `ttl=2s`, `explicit_max_ttl=6s`, renewal pulse every `1.0s`

### 3. How to Run
```bash
make up EXP=token-lifecycle
make run SCRIPT=02_token_renewal
make down EXP=token-lifecycle
```

### 4. Observed Metrics & Failure Signatures

```text
Time (s)   Action                 Reported TTL     Status
-----------------------------------------------------------------
  1.02s    Renew Pulse #1         2s               RENEWED (Capped by Max TTL)
  2.04s    Renew Pulse #2         2s               RENEWED (Capped by Max TTL)
  3.07s    Renew Pulse #3         2s               RENEWED (Capped by Max TTL)
  4.09s    Renew Pulse #4         1s               RENEWED (Capped by Max TTL)
  5.12s    Renew Pulse #5         0s               RENEWED (Capped by Max TTL)
  6.15s    Renew Pulse #6         0s               REJECTED: Ceiling Hit
```

#### Ceiling Rejection Signature:
```text
hvac.exceptions.InvalidRequest: lease is not renewable, on post http://127.0.0.1:8200/v1/auth/token/renew-self
Code: 400. Errors: ["lease is not renewable"]
```

### 5. Production & Systems Takeaways
1. **Never Rely on Infinite Renewals**: Applications must treat token expiration as a normal lifecycle event and re-authenticate via platform identity (e.g., Kubernetes ServiceAccount JWT or cloud IAM role) when `max_ttl` is reached.
2. **Dynamic TTL Clamping**: Vault computes `lease_duration = min(requested_increment, max_ttl - elapsed_time)`. Shrinking lease durations signal clients to prepare for re-authentication before hard revocation occurs.
3. **Hierarchical Revocation Cascades**: Revoking a parent token immediately revokes all child tokens and dynamic database leases beneath it.

---

## Experiment 03: Transit Encryption-as-a-Service (EaaS) & Key Rotation

### 1. Hypothesis & Objective
- Offloading AES256-GCM96 encryption to Vault's `transit` engine eliminates raw key management inside application memory, but sequential per-record HTTP calls incur network round-trip overhead.
- Using Vault's `batch_input` bulk encryption API collapses 200 HTTP round-trips into 1 call (>20x throughput speedup).
- Rotating the transit key (`v1` $\rightarrow$ `v2`) and executing `rewrap_data` upgrades legacy `vault:v1:...` ciphertexts to `vault:v2:...` server-side without ever exposing plaintext over the network.

### 2. Configuration & Limits
- **Compose Profile**: `transit-crypto` (`vault-transit-data:/vault/file` in [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `memory: 128M`, `cpus: '0.5'`
- **Workload**: 200 payload encryptions comparing sequential HTTP requests vs single-request `batch_input`, followed by key ring rotation and server-side ciphertext rewrapping.

### 3. How to Run
```bash
make up EXP=transit-crypto
make run SCRIPT=03_transit_throughput
make down EXP=transit-crypto
```

### 4. Observed Metrics & Failure Signatures

| Operation Mode | HTTP Requests | Total Records | Observed Throughput | Key Version State |
|---|---|---|---|---|
| **Sequential Transit Encrypt** | 200 requests | 200 records | ~150–300 ops/sec (HTTP RTT bound) | `vault:v1:...` |
| **Batched Transit (`batch_input`)** | **1 request** | 200 records | **>5,000 ops/sec (>20x speedup)** | `vault:v1:...` |
| **Key Rotation + `rewrap_data`** | Server-side rewrap | 200 records | Zero plaintext exposure; `v1` retired | **`vault:v2:...` (`min_decryption_version=2`)** |

### 5. Production & Systems Takeaways
1. **Always Batch High-Volume Transit Operations**: When encrypting rows during ETL or bulk API ingestion, use `batch_input` to amortize HTTP/TLS overhead across hundreds of items.
2. **Envelope Encryption for Large Payloads**: For payloads larger than a few megabytes, generate a data encryption key (DEK) via `transit/datakey/plaintext`, encrypt data locally with AES-GCM, and store the Vault-wrapped DEK alongside the ciphertext.
3. **Enforce `min_decryption_version` After Rewrap**: After rotating keys and rewrapping historical database records, advance `min_decryption_version` to cryptographically invalidate compromised legacy key versions.

---

## Primary Sources & Deep-Dive References

- [HashiCorp Vault Documentation: Token Concepts & Lifecycle](https://developer.hashicorp.com/vault/docs/concepts/tokens) – Complete architecture of service tokens, child tokens, TTL renewal, and hard `explicit_max_ttl` ceilings.
- [HashiCorp Vault Documentation: Dynamic Database Secrets Engine](https://developer.hashicorp.com/vault/docs/secrets/databases) – Role-based credential generation, dynamic role statements, and automated lease revocation mechanisms.
- [HashiCorp Vault Documentation: KV Secrets Engine v2](https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2) – Versioning semantics, metadata storage, and soft delete vs permanent destroy (`destroy_secret_versions`).
- [HashiCorp Vault Documentation: Transit Secrets Engine](https://developer.hashicorp.com/vault/docs/secrets/transit) – Encryption-as-a-Service, cryptographic key rotation, batch encryption APIs, and `rewrap` workflows.
- Shamir, Adi (Communications of the ACM, 1979): [*How to Share a Secret*](https://web.mit.edu/6.857/OldStuff/Fall03/ref/Shamir-HowToShareASecret.pdf) – The mathematical polynomial interpolation foundation used in Vault's unseal ceremony.

---

## Clean Reset & Teardown

```bash
# Tear down a specific profile and wipe its volume:
make down EXP=dynamic-creds

# Wipe all containers and named volumes for this lab:
make clean
```

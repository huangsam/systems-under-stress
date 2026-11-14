# Systems Under Stress Agent Guidelines

This repository (`systems-under-stress`) is an experimental lab for stress-testing and exploring the operational limits of database and systems Docker images (PostgreSQL, Redis, MongoDB, ClickHouse, Cassandra, Vault, Debian Linux, Toxiproxy).

This file serves as the root agent instructions, invariants, and guardrails.

---

## 1. Safety & Host Invariants

1. **Mandatory Resource Limits** *(enforced by CI)*:
   - Every service defined in a Compose file **must** specify resource limits under `deploy.resources.limits`:
     ```yaml
     deploy:
       resources:
         limits:
           cpus: '1.0'
           memory: 256M
     ```
   - Never run unconstrained containers that could exhaust Docker Desktop VM memory or freeze macOS.

2. **Loopback-Only Network Binding** *(enforced by CI)*:
   - Never bind to `0.0.0.0`. All host port mappings must explicitly bind to `127.0.0.1`:
     ```yaml
     ports:
       - "127.0.0.1:6379:6379"
     ```

3. **No Sensitive Host Mounts** *(enforced by CI)*:
   - Volume mounts are restricted to named Docker volumes or relative paths within the specific lab directory.
   - Never mount host root directories, user directories (`~/.ssh`, `~/.aws`), or the Docker socket (`/var/run/docker.sock`).

4. **Squeeze Resources, Don't Bloat Disk** *(manual invariant)*:
   - To test limits (OOM, eviction, disk spill, lock contention), **lower the container and engine limits** (e.g., 32MB–64MB RAM, 4MB `work_mem`, 16MB `maxmemory`) rather than generating gigabytes of disk data.
   - Workload datasets must remain under 50MB whenever possible.

5. **Zero Hardcoded Secrets** *(manual invariant)*:
   - Use dummy credentials only (e.g., `user=postgres`, `password=postgres`, `VAULT_DEV_ROOT_TOKEN_ID=root`). Never commit personal tokens or keys.

---

## 2. Architecture & Experiment Invariants

1. **One Directory per Lab** *(enforced by CI)*:
   - Group experiments by target engine or failure domain into a dedicated top-level directory (`redis/`, `postgres/`, `clickhouse/`, `mongodb/`, `cassandra/`, `vault/`, `linux/`, `chaos/`).

2. **Single Compose File with Profiles & Overrides** *(enforced by CI)*:
   - Each lab directory has one `docker-compose.yml` defining experiment variants via Docker Compose `profiles`.
   - Use top-level `x-<lab>-base: &<lab>-base` YAML anchors to keep common image definitions, resource limits, and healthchecks DRY.
   - Parameterize image tags with environment variable fallbacks (`image: ${<ENGINE>_IMAGE:-<default_image>:<tag>}`).

3. **Isolated Named Volumes per Profile** *(enforced by CI)*:
   - Each profile requiring persistent storage **must** use its own distinct named volume(s) (e.g., `redis-eviction-lru-data`, `postgres-isolation-data`).
   - Never share stateful named volumes across distinct profiles.

4. **Deterministic Readiness (No Blind `sleep`)** *(enforced by CI)*:
   - Every service must define an explicit `healthcheck`.
   - Workload runners must use Compose's built-in readiness wait:
     ```bash
     docker compose --profile <name> up -d --wait
     ```

5. **Two-Tier Resets & Teardown Hygiene** *(enforced by CI)*:
   - **Soft Reset (Fast)**: Provide `make soft-reset` for sub-second in-engine state resets (`FLUSHALL`, `TRUNCATE`, `DROP`, toxic reset) in every stateful lab (see [§3 Per-Lab Exceptions](#3-per-lab-exceptions) for stateless labs).
   - **Hard Reset (Cold)**: Clean teardown via `docker compose --profile <name> down -v` (`make down`) or `docker compose --profile "*" down -v` (`make clean`) so no orphaned volumes remain on disk.

6. **Self-Contained Client Dependencies (PEP 723 via `uv`)** *(enforced by CI)*:
   - Every executable script `experiments/[0-9][0-9]_*.py` (and standalone monitors) must declare dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) metadata:
     ```python
     # /// script
     # requires-python = ">=3.11"
     # dependencies = [
     #     "rich>=13.7.0",
     # ]
     # ///
     ```
   - Execute host-side scripts via `uv run` so dependencies run in an isolated ephemeral environment without host `venv` friction.

7. **Baseline Crash Course (`00_crash_course.py`)** *(enforced by CI)*:
   - Every lab must provide `experiments/00_crash_course.py` to verify baseline connectivity, core engine primitives, and diagnostic introspection before running destructive failure experiments.

8. **Observability & Real-Time Feedback** *(manual invariant)*:
   - Never rely solely on an unhandled crash or raw traceback.
   - Experiment scripts must print structured live progress, before/after state metrics, and the exact captured failure signature using `rich`, and assert phase preconditions, failure signatures, and recovery invariants via `verify_phase(...)`.

9. **Ergonomic CLI Standards (`Makefile`)** *(enforced by CI)*:
   - Every lab directory must provide a `Makefile` supporting the required targets: `help`, `up`, `run`, `down`, `clean`, `logs`, and `soft-reset` (unless exempted in [§3](#3-per-lab-exceptions)).
   - See the root [`README.md`](./README.md#quick-start-universal-cli-workflow) for the canonical CLI usage workflow.

10. **Canonical Lab `README.md` Structure** *(enforced by CI)*:
    - Each lab must have a single `README.md` that links directly to [`./docker-compose.yml`](./README.md) rather than duplicating raw YAML config blocks or hardcoded image tags.
    - Every lab `README.md` must include these exact H2 (`## `) sections in order:
      - `## Lab Architecture` (directory tree and link to [`docker-compose.yml`])
      - `## Quickstart & Execution Standard` (including `00_crash_course` usage)
      - `## Experiment 00: Crash Course` (baseline verification summary and run command)
      - `## Experiment 01: <Title>` ... `## Experiment 0N: <Title>` — each numbered experiment (`01`..`0N`) must contain all 5 H3 (`### `) subsections:
        1. `### 1. Hypothesis & Objective`
        2. `### 2. Configuration & Limits`
        3. `### 3. How to Run`
        4. `### 4. Observed Metrics & Failure Signatures`
        5. `### 5. Production & Systems Takeaways`
      - `## Primary Sources & Deep-Dive References`
      - `## Clean Reset & Teardown`
    - Every Docker Compose profile (excluding `default` aliases) and every `experiments/[0-9][0-9]_*.py` script must be documented in the lab's `README.md`.

---

## 3. Per-Lab Exceptions

Specific architectural differences across the 9 labs are explicitly bounded in the table below:

| Lab | Scope / Exception | Reason |
| :--- | :--- | :--- |
| **`cassandra/`** | `make run` invokes `CASS_DRIVER_NO_EXTENSIONS=1 uv run --python 3.11` | `cassandra-driver` C extensions and `asyncore` removal in Python 3.12+ require Python 3.11 runtime pinning. |
| **`chaos/`** | Organized by failure domain (`chaos/`) with `toxi_client.py` helper module | Tests network-layer gray failures (`toxiproxy` + `redis-upstream`) rather than a single storage engine; `toxi_client.py` is an imported library, not a standalone PEP 723 runner. |
| **`linux/`** | No `make soft-reset`; `make run` executes inside container via `docker compose exec`; mounts `.:/lab:ro` | Kernel cgroup/ulimit/OOM experiments test ephemeral OS processes inside a custom `Dockerfile` container and auto-infer `EXP` from `SCRIPT`. |
| **`redis/`** | Includes `experiments/monitor.py` and `make monitor` target | Streams real-time memory fragmentation and key eviction telemetry in a secondary terminal. |
| **`vault/`** | No `make soft-reset` target | Vault runs in ephemeral `-dev` server mode where each experiment dynamically provisions and revokes its own isolated mounts, roles, and tokens. |

---

## 4. Verification & Enforcement

Invariants are split between automated CI gates (`scripts/check_invariants.py` and `ruff`) and manual code review:

```bash
# 1. Run Python linter and formatter checks across all scripts
make lint

# 2. Validate all Docker Compose configs AND run the automated invariant & docs audit
make compose-lint
```

- **Automated by `scripts/check_invariants.py` (`make compose-lint`)**:
  - Compose service resource limits (`cpus` and `memory`), `127.0.0.1` loopback port bindings, `healthcheck.test` definitions, volume mount safety, and profile volume isolation.
  - PEP 723 inline metadata headers on all `experiments/[0-9][0-9]_*.py` scripts.
  - Presence of required `Makefile` targets (`help`, `up`, `run`, `down`, `clean`, `logs`, `soft-reset`).
  - Lab `README.md` canonical section structure, complete coverage of all Compose profiles and experiment scripts, absence of hardcoded image version tags, and accuracy of the root `README.md` experiment matrix count.
- **Pre-Commit Hook (`lefthook.yml`)**: Automatically runs `ruff` and `make compose-lint` before commits.

---

## 5. Adding a New Lab

When adding a new technology lab `<lab>/`:
1. Create `<lab>/docker-compose.yml`, `<lab>/Makefile`, `<lab>/README.md`, and `<lab>/experiments/00_crash_course.py` (+ numbered experiments `01_*.py`..`0N_*.py`) following all invariants in §1 and §2.
2. Add `<lab>` to the lab loop in the root [`Makefile`](./Makefile) (`compose-lint` and `clean-all` targets).
3. Add a row to the **Lab Matrix** table in the root [`README.md`](./README.md) and update the total experiment count in the heading.
4. If the lab requires a structural exception, document it in [§3 Per-Lab Exceptions](#3-per-lab-exceptions) and codify it in [`scripts/check_invariants.py`](./scripts/check_invariants.py).
5. Run `make lint && make compose-lint` and verify zero violations.

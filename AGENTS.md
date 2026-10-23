# Systems Under Stress Agent Guidelines

This repository (`systems-under-stress`) is an experimental lab for stress-testing and exploring the operational limits of database and systems Docker images (PostgreSQL, Redis, MongoDB, ClickHouse, Cassandra, Neo4j, Vault, Debian Linux).

This file serves as the root agent instructions, invariants, and guardrails.

---

## 1. Safety & Host Invariants

1. **Mandatory Resource Limits**:
   - Every service defined in a Compose file **must** specify resource limits under `deploy.resources.limits`:
     ```yaml
     deploy:
       resources:
         limits:
           cpus: '1.0'
           memory: 256M
     ```
   - Never run unconstrained containers that could exhaust Docker Desktop VM memory or freeze macOS.

2. **Loopback-Only Network Binding**:
   - Never bind to `0.0.0.0`. All port mappings must explicitly bind to `127.0.0.1`:
     ```yaml
     ports:
       - "127.0.0.1:6379:6379"
     ```

3. **No Sensitive Host Mounts**:
   - Volume mounts are restricted to named Docker volumes or relative paths within the specific experiment folder.
   - Never mount host root directories, user directories (`~/.ssh`, `~/.aws`), or the Docker socket (`/var/run/docker.sock`).

4. **Squeeze Resources, Don't Bloat Disk**:
   - To test limits (OOM, eviction, disk spill, lock contention), **lower the container limits** (e.g., 32MB–64MB RAM, 4MB `work_mem`) rather than generating gigabytes of disk data.
   - Workload datasets should remain under 50MB whenever possible.

5. **Zero Hardcoded Secrets**:
   - Use dummy credentials only (e.g., `user=postgres`, `password=postgres`). Never commit personal tokens or keys.

---

## 2. Architecture & Experiment Invariants

1. **One Directory per Technology**:
   - Group experiments by system (e.g., `redis/`, `postgres/`, `clickhouse/`).

2. **Single Compose File with Profiles**:
   - Each technology directory has one `docker-compose.yml` that defines experiment variants via Docker Compose `profiles` (e.g., `profiles: ["eviction"]`, `profiles: ["stampede"]`).
   - Use YAML anchors (`&base` and `*base`) to keep the base image version and common healthchecks DRY.

3. **Isolated Named Volumes per Profile**:
   - Each profile requiring persistent storage **must** have its own distinct named volume (e.g., `redis-persist-data`, `redis-eviction-data`).
   - Never share stateful volumes across different profiles.

4. **Deterministic Readiness (No Blind `sleep`)**:
   - Every service must have an explicit `healthcheck`.
   - Workload runners must use Compose's built-in wait:
     ```bash
     docker compose --profile <name> up -d --wait
     ```

5. **Two-Tier Resets & Teardown Hygiene**:
   - **Soft Reset (Fast)**: Provide commands or script functions for in-engine resets (`FLUSHALL`, `TRUNCATE TABLE`, etc.) for sub-second query iteration.
   - **Hard Reset (Cold)**: Clean teardown via `docker compose --profile <name> down -v` or `docker compose --profile "*" down -v` to ensure Docker does not leave orphaned profiled volumes on disk.

6. **Self-Contained Client Dependencies (PEP 723 via `uv`)**:
   - Python client/benchmark scripts must declare dependencies inline via [PEP 723](https://peps.python.org/pep-0723/) metadata:
     ```python
     # /// script
     # requires-python = ">=3.12"
     # dependencies = [
     #     "redis>=5.0",
     # ]
     # ///
     ```
   - Execute via `uv run <script>.py` so dependencies run in an isolated ephemeral environment without host venv friction.

7. **Observability & Real-Time Feedback**:
   - Never rely solely on an unhandled crash or final stack trace.
   - Every experiment should provide a way to observe the limit being approached or hit in real-time (e.g. streaming memory usage, eviction rates, lock wait times, or connection counts).

8. **Ergonomic CLI Standards (Makefile)**:
   - Provide a concise `Makefile` in each technology lab directory supporting standard targets:
     - `make up EXP=<profile>`: Starts target profile with `--wait`.
     - `make run EXP=<profile>`: Executes the workload script via `uv run`.
     - `make monitor EXP=<profile>`: Streams live metrics/stats for that engine.
     - `make clean`: Tears down all containers and named volumes via `--profile "*" down -v`.

9. **Documentation & Lab Structure**:
   - Every experiment directory or major profile should be accompanied by a `README.md` following the standard lab template:
     - **Hypothesis & Objective**
     - **Configuration & Limits**
     - **Reproduction Steps**
     - **Observed Metrics & Failure Signatures**
     - **Production & Systems Takeaways**

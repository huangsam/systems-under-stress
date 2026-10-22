# Linux Container Limits & Kernel Primitives Lab

Hands-on experiments stress-testing Linux kernel subsystems, cgroups v2 resource controllers, file descriptor tables, and the Linux Out-Of-Memory (OOM) Killer inside isolated containers (`debian:trixie`).

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) leveraging **Docker Compose Profiles** with explicit cgroups limits and Linux kernel controls:

```text
linux/
├── Dockerfile               # Debian Bookworm slim base + python3, procps, lsof
├── docker-compose.yml       # Cgroups v2 controllers, ulimits, swap constraints
├── Makefile                 # One-touch runner targets (up, run, down, clean, sh, logs)
├── experiments/
│   ├── 01_fork_bomb_cgroup.py  # Spawns recursive processes under pids.max: 40
│   ├── 02_fd_limits.py         # Sets ulimit -n 64 and saturates /proc/self/fd/
│   └── 03_oom_killer.py        # Allocates 4MB chunks under 32M RAM to trip OOM 137
└── README.md
```

All test scripts declare metadata inline via [PEP 723](https://peps.python.org/pep-0723/) and run friction-free with standard `python3` or `uv run`.

---

## Experiment Profiles & Resource Limits

| Profile | Target Service | CPU Limit | Memory Limit | Kernel / Cgroup Limit | Target Failure Mode |
|---|---|---|---|---|---|
| `pid-limit` | `pid-limit` | `0.5` | `64M` | `pids: 40` (`pids.max`) | `BlockingIOError` (`EAGAIN` Errno 11) |
| `fd-exhaustion` | `fd-exhaustion` | `0.5` | `64M` | `ulimits.nofile: 64` (`RLIMIT_NOFILE`) | `OSError` (`EMFILE` Errno 24) |
| `oom-killer` | `oom-killer` | `0.5` | `32M` | `memswap_limit: 32M` (Zero swap) | Kernel `SIGKILL` (Signal 9 / Exit Code 137) |

---

## Experiment 01: The Cgroups PID Limit (Contained Fork Bomb)

### 1. Hypothesis & Objective
When a container is constrained by cgroups v2 `pids.max: 40`, executing a recursive process spawner will deterministically halt upon reaching 40 tasks. The Linux kernel will refuse further `clone()` / `fork()` syscalls with:
```text
BlockingIOError: [Errno 11] Resource temporarily unavailable (EAGAIN)
```
The macOS host and all neighbouring containers experience **0% CPU or process table degradation**.

### 2. Configuration & Constraints
- **Base Image**: `debian:trixie`
- **Compose Profile**: `profiles: ["pid-limit"]`
- **Cgroups Limits**: `pids: 40`, `memory: 64M`, `cpus: '0.5'`
- **Target File**: `/sys/fs/cgroup/pids.max` and `/sys/fs/cgroup/pids.current`

### 3. Reproduction Steps
```bash
# 1. Start the pid-limit container with 40-process ceiling
make up EXP=pid-limit

# 2. Run the fork bomb experiment inside the container
make run EXP=pid-limit SCRIPT=01_fork_bomb_cgroup

# 3. Teardown
make down EXP=pid-limit
```

### 4. Observed Metrics & Failure Signatures
```text
[*] cgroup pids.max : 40 (path: /sys/fs/cgroup/pids.max)
[*] Initial Tasks   : 2 (path: /sys/fs/cgroup/pids.current)

--- Phase 1: Controlled Process Spawning ---
  [Spawned Child #01] PID: 12     | cgroups pids.current: 3 / 40
  [Spawned Child #02] PID: 13     | cgroups pids.current: 4 / 40
  ...
  [Spawned Child #38] PID: 49     | cgroups pids.current: 40 / 40

######################################################################
  >>> SUCCESS: HIT CGROUP PID LIMIT <<<
  Exact Exception Caught : BlockingIOError: [Errno 11] Resource temporarily unavailable
  Total Children Spawned : 38
  Cgroup Tasks At Failure: 40 / 40
######################################################################
```

### 5. Production & Systems Takeaways
1. **Cgroups v2 vs `ulimit -u`**: `ulimit -u` (`RLIMIT_NPROC`) restricts processes per Linux User ID (UID). If an attacker forks processes across multiple UIDs inside a container, `ulimit` fails to protect the node. The cgroups v2 `pids` controller enforces a hard boundary across the entire cgroup hierarchy regardless of UID.
2. **Kubernetes `PodPidsLimit`**: In Kubernetes clusters, nodes configure `podPidsLimit` (via Kubelet `--pod-max-pids`) to prevent rogue containers from exhausting the kernel PID space (`/proc/sys/kernel/pid_max`).

---

## Experiment 02: File Descriptor & Inode Exhaustion

### 1. Hypothesis & Objective
When an application runs under a file descriptor ceiling (`ulimit -n 64`), repeatedly opening files, sockets, or pipes without closing them will saturate the per-process descriptor table. Attempting to open FD #64 fails with:
```text
OSError: [Errno 24] Too many open files (EMFILE)
```
Furthermore, when the table is 100% full, even administrative inspection calls (like `opendir('/proc/self/fd/')`) fail with `EMFILE` because directory traversal itself requires allocating a file descriptor.

### 2. Configuration & Constraints
- **Compose Profile**: `profiles: ["fd-exhaustion"]`
- **Container Limit**: `ulimits: nofile: soft: 64, hard: 64`
- **Kernel Interfaces**: `/proc/self/fd/`, `/proc/sys/fs/file-nr`, `/proc/sys/fs/file-max`

### 3. Reproduction Steps
```bash
# 1. Start fd-exhaustion container
make up EXP=fd-exhaustion

# 2. Run descriptor exhaustion experiment
make run EXP=fd-exhaustion SCRIPT=02_fd_limits

# 3. Teardown
make down EXP=fd-exhaustion
```

### 4. Observed Metrics & Failure Signatures
```text
[*] Initial RLIMIT  : soft=64, hard=64
[*] Baseline open descriptors (5 active):
    fd  0 -> /dev/pts/0 (stdin)
    fd  1 -> /dev/pts/0 (stdout)
    fd  2 -> /dev/pts/0 (stderr)
    fd  3 -> /dev/urandom
    fd  4 -> /lab/experiments/02_fd_limits.py

--- Phase 2: Opening Descriptors Until Limit (64) ---
  [Allocated FD] Index #58 -> Assigned fd=61
  [Allocated FD] Index #59 -> Assigned fd=62
  [Allocated FD] Index #60 -> Assigned fd=63

######################################################################
  >>> SUCCESS: HIT PROCESS FILE DESCRIPTOR CEILING <<<
  Exact Exception Caught : OSError: [Errno 24] Too many open files
  Total Open Files Held  : 60
  Failed Iteration Index : #61
######################################################################

--- Phase 3: Inspecting FD Table at Capacity ---
[*] Note: Attempting opendir('/proc/self/fd') while at 100% capacity:
    Expected Insight: Error reading fd dir: [Errno 24] Too many open files
    (When the FD table is completely full, even opendir() cannot allocate an FD!)
[*] Releasing exactly 1 FD to inspect /proc/self/fd snapshot...
[*] Active entries in fd table (64 active): fd 0 through fd 63
```

### 5. Production & Systems Takeaways
1. **EMFILE vs ENFILE**:
   - `EMFILE` (`Errno 24: Too many open files`): Local per-process limit hit (`RLIMIT_NOFILE`). Other processes and containers on the host can still allocate descriptors normally.
   - `ENFILE` (`Errno 23: File table overflow`): The entire Linux kernel has exhausted global system file handles (`fs.file-max`). This causes host-wide failure.
2. **Microservice Failure Cascade**:
   - In network servers (Nginx, Go HTTP, Node.js), unclosed client connections or leaking DB connections exhaust FDs.
   - The kernel's `accept()` syscall fails with `EMFILE`, silently dropping all new incoming TCP handshakes.

---

## Experiment 03: Kernel OOM Killer vs Swap Behavior (Exit Code 137)

### 1. Hypothesis & Objective
When a container is configured with a strict 32MB RAM ceiling and zero swap (`memory: 32M`, `memswap_limit: 32M`), allocating physical dirty memory in 4MB chunks will breach the cgroup limit. Because swap is disabled, the Linux kernel cannot page out dirty anonymous pages to disk and invokes `out_of_memory()`. The kernel dispatches `SIGKILL` (signal 9) to the culprit process, resulting in the universal container termination status:
$$\text{Exit Code} = 128 + 9 = 137$$

### 2. Configuration & Constraints
- **Compose Profile**: `profiles: ["oom-killer"]`
- **Memory Limits**: `memory: 32M`, `memswap_limit: 32M` (Strictly zero swap)
- **Allocation Method**: 4MB dirty chunks (writing to 4096-byte page offsets to force page faults and charge RSS against the memory cgroup)
- **Telemetry Source**: `/sys/fs/cgroup/memory.events` (`oom_kill` counter)

### 3. Reproduction Steps
```bash
# 1. Start the oom-killer container with 32MB limit
make up EXP=oom-killer

# 2. Run OOM killer observer experiment
make run EXP=oom-killer SCRIPT=03_oom_killer

# 3. Teardown
make down EXP=oom-killer
```

### 4. Observed Metrics & Failure Signatures
```text
[*] cgroups memory.max     : 32.0 MB (/sys/fs/cgroup/memory.max)
[*] cgroups memory.current : 3.8 MB (/sys/fs/cgroup/memory.current)
[*] Initial memory.events  : {'low': 0, 'high': 0, 'max': 0, 'oom': 0, 'oom_kill': 0}

--- Phase 1: Forking Worker Child for Controlled OOM Observation ---
[*] Memory Allocator Worker started (PID: 18)...
  [Chunk #01] Allocated +4.0 MB (Total: 4.0 MB) | VmRSS: 5216 kB
  [Chunk #02] Allocated +4.0 MB (Total: 8.0 MB) | VmRSS: 9312 kB
  [Chunk #03] Allocated +4.0 MB (Total: 12.0 MB) | VmRSS: 13408 kB
  [Chunk #04] Allocated +4.0 MB (Total: 16.0 MB) | VmRSS: 17504 kB
  [Chunk #05] Allocated +4.0 MB (Total: 20.0 MB) | VmRSS: 21600 kB
  [Chunk #06] Allocated +4.0 MB (Total: 24.0 MB) | VmRSS: 25696 kB
  [Chunk #07] Allocated +4.0 MB (Total: 28.0 MB) | VmRSS: 29792 kB

######################################################################
  >>> SUCCESS: DETECTED PROCESS TERMINATION BY LINUX KERNEL <<<
  Termination Reason : Process killed by Signal 9
  Signal Name        : SIGKILL (Kill signal, cannot be caught or ignored)
  Calculated Exit    : 128 + 9 = EXIT CODE 137
######################################################################

--- Phase 2: Post-Mortem Cgroup Telemetry Inspection ---
[*] Post-mortem memory.events  : {'low': 0, 'high': 0, 'max': 1, 'oom': 1, 'oom_kill': 1}
[*] Net kernel oom_kill count  : +1
```

### 5. Production & Systems Takeaways
1. **The Math of Exit Code 137**:
   Unix shells, Docker, and Kubernetes report process exit status via `128 + n` when killed by an unhandled signal $n$. Since `SIGKILL` is signal 9:
   $$128 + 9 = 137$$
2. **Why Swap Matters**:
   When swap is enabled on a node without strict `memswap_limit`, memory exhaustion does not immediately trigger OOM. Instead, the kernel begins aggressive page thrashing (swapping anonymous memory to disk), skyrocketing CPU I/O wait times and slowing the application to a crawl before failing.
3. **Cgroups v2 Telemetry (`memory.events`)**:
   Modern Kubernetes nodes and container runtimes (containerd, CRI-O) poll `/sys/fs/cgroup/memory.events` for the `oom_kill` counter rather than parsing system logs or kernel `dmesg`.

---

## Systems Architecture Deep Dive & Interview Questions

### Q1: What does container Exit Code 137 mean in Docker and Kubernetes?
**Answer**:
When a process inside a container terminates due to an unhandled signal, the Linux kernel sets its status word to indicate the signal number. The standard convention implemented by POSIX shells, Docker, and Kubernetes is:
$$\text{Exit Code} = 128 + \text{Signal Number}$$
Signal 9 is `SIGKILL` ($128 + 9 = 137$).
In containerized environments, Exit Code 137 is the signature of:
1. The **Linux kernel OOM Killer** terminating a container that breached its cgroup memory limit (`limits.memory`).
2. The **orchestrator** (Docker or Kubernetes Kubelet) sending `SIGKILL` after a timeout following `SIGTERM` (e.g. failed graceful shutdown during rolling deployment).

### Q2: Why is running an application as PID 1 inside a container dangerous if it doesn't handle zombie reaping?
**Answer**:
In Linux, when a child process terminates, it enters a `defunct` zombie state until its parent calls `wait()` or `waitpid()` to read its exit status.
If the parent process terminates before its children, the children are "orphaned" and reparented to **PID 1** (the init process).
In a standard Linux OS, `systemd` or `init` continually runs a zombie reaping loop. However, inside a minimal Docker container, your application binary is typically PID 1. If that application does not implement a `SIGCHLD` handler and call `waitpid()`, orphaned processes remain as zombies forever, eventually exhausting the kernel PID table (`pids.max`).
*Solution*: Use an init wrapper like `tini` (Docker `--init`) or implement proper `SIGCHLD` handling.

### Q3: How do container abstractions actually work under the hood?
**Answer**:
Containers are not virtual machines; they are standard Linux processes partitioned by two kernel primitives:
1. **Namespaces** (Isolation Boundaries): Control what a process can **see**.
   - `pid`: Process tree isolation (container sees itself as PID 1).
   - `net`: Network interface and routing table isolation.
   - `mnt`: Filesystem mount point isolation.
   - `ipc`, `uts`, `user`, `cgroup`: IPC, hostname, UID mappings, and cgroup visibility.
2. **Control Groups (cgroups v2)** (Resource Boundaries): Control what a process can **consume**.
   - `cpu`: CPU quotas, throttling, and shares.
   - `memory`: Hard memory ceilings (`memory.max`), swap limits, and OOM kills.
   - `pids`: Maximum concurrent task/thread count (`pids.max`).
   - `io`: Block I/O read/write bandwidth and IOPS limits.

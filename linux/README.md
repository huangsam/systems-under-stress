# Linux Container Limits & Kernel Primitives Lab

Hands-on experiments stress-testing Linux kernel subsystems, cgroups v2 resource controllers (`pids.max`, `memory.max`), file descriptor tables (`RLIMIT_NOFILE`), and the Linux Out-Of-Memory (OOM) Killer inside isolated containers.

---

## Lab Architecture

This lab uses a single [`docker-compose.yml`](./docker-compose.yml) and custom [`Dockerfile`](./Dockerfile) leveraging **Docker Compose Profiles** with explicit cgroups v2 limits and Linux kernel controls:

```text
linux/
├── Dockerfile                  # Debian slim base + python3, procps, lsof, rich
├── docker-compose.yml          # Base anchor &linux-base, cgroups v2 controllers, ulimits, swap caps
├── Makefile                    # One-touch runner targets (up, run, down, clean, sh, logs)
├── experiments/
│   ├── 00_crash_course.py      # Kernel runtime, /proc/self/status, POSIX limits & cgroups v2
│   ├── 01_fork_bomb_cgroup.py  # Spawns recursive processes under pids.max: 40
│   ├── 02_fd_limits.py         # Sets ulimit -n 64 and saturates /proc/self/fd/
│   └── 03_oom_killer.py        # Allocates 4MB chunks under 32M RAM to trip OOM 137
└── README.md
```

All test scripts declare metadata inline via [PEP 723](https://peps.python.org/pep-0723/) and execute inside the target Linux container via `make run`.

---

## Quickstart & Execution Standard

Across all profiles in this lab (see [`docker-compose.yml`](./docker-compose.yml) for exact cgroup ceilings, ulimits, and healthchecks; `make run` automatically selects the matching profile container from `SCRIPT`):

```bash
cd linux

# 1. Build and start the target profile container with automatic healthcheck wait
make up EXP=pid-limit

# 2. Run the baseline crash course or a stress experiment inside the container
make run SCRIPT=00_crash_course
make run SCRIPT=01_fork_bomb_cgroup

# 3. Tear down the profile container or wipe all profiles
make down EXP=pid-limit
make clean
```

---

## Experiment 00: Crash Course

Verifies the container's Linux kernel environment (`/proc/version`, `uname`), process status and RSS memory mappings (`/proc/self/status`), POSIX resource limits (`getrlimit` for `RLIMIT_NOFILE`, `RLIMIT_NPROC`), cgroups v2 controller files (`/sys/fs/cgroup/pids.max`, `memory.max`, `cpu.max`), and `/proc/self/fd` descriptor links.

```bash
make up EXP=pid-limit
make run SCRIPT=00_crash_course
make down EXP=pid-limit
```

---

## Experiment 01: The Cgroups PID Limit (Contained Fork Bomb)

### 1. Hypothesis & Objective
When a container is constrained by cgroups v2 `pids.max: 40`, executing a recursive process spawner will deterministically halt upon reaching 40 tasks. The Linux kernel refuses further `clone()` / `fork()` syscalls with:
```text
BlockingIOError: [Errno 11] Resource temporarily unavailable (EAGAIN)
```
The macOS host and all neighboring containers experience **0% CPU or process table degradation**.

### 2. Configuration & Limits
- **Compose Profile**: `pid-limit` (see [`docker-compose.yml`](./docker-compose.yml))
- **Cgroups v2 Limits**: `pids: 40`, `memory: 64M`, `cpus: '0.5'`
- **Kernel Telemetry**: `/sys/fs/cgroup/pids.max` and `/sys/fs/cgroup/pids.current`

### 3. How to Run
```bash
make up EXP=pid-limit
make run SCRIPT=01_fork_bomb_cgroup
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
1. **Cgroups v2 vs `ulimit -u`**: `ulimit -u` (`RLIMIT_NPROC`) restricts processes per Linux User ID (UID). If a workload forks processes across multiple UIDs inside a container, `ulimit` fails to protect the node. The cgroups v2 `pids` controller enforces a hard boundary across the entire cgroup hierarchy regardless of UID.
2. **Kubernetes `PodPidsLimit`**: Production Kubernetes nodes configure `podPidsLimit` (via Kubelet `--pod-max-pids`) to prevent runaway thread/process leaks from exhausting the host kernel PID space (`/proc/sys/kernel/pid_max`).

---

## Experiment 02: File Descriptor & Inode Exhaustion

### 1. Hypothesis & Objective
When an application runs under a file descriptor ceiling (`ulimit -n 64`), repeatedly opening files, sockets, or pipes without closing them saturates the per-process descriptor table. Attempting to open FD #64 fails with:
```text
OSError: [Errno 24] Too many open files (EMFILE)
```
Furthermore, when the table is 100% full, even administrative inspection calls (like `opendir('/proc/self/fd/')`) fail with `EMFILE` because directory traversal itself requires allocating a file descriptor.

### 2. Configuration & Limits
- **Compose Profile**: `fd-exhaustion` (see [`docker-compose.yml`](./docker-compose.yml))
- **Container Limits**: `ulimits.nofile: 64` (soft & hard), `memory: 64M`, `cpus: '0.5'`
- **Kernel Interfaces**: `/proc/self/fd/`, `/proc/sys/fs/file-nr`, `/proc/sys/fs/file-max`

### 3. How to Run
```bash
make up EXP=fd-exhaustion
make run SCRIPT=02_fd_limits
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
```

### 5. Production & Systems Takeaways
1. **`EMFILE` vs `ENFILE`**:
   - `EMFILE` (`Errno 24: Too many open files`): Local per-process limit hit (`RLIMIT_NOFILE`). Other processes and containers on the host continue allocating descriptors normally.
   - `ENFILE` (`Errno 23: File table overflow`): The Linux kernel has exhausted global system file handles (`fs.file-max`), causing host-wide failure.
2. **Microservice Failure Cascade**: In network servers, leaking HTTP client sockets or database connections exhausts FDs, causing the kernel's `accept()` syscall to fail with `EMFILE` and drop incoming TCP connections.

---

## Experiment 03: Kernel OOM Killer vs Swap Behavior (Exit Code 137)

### 1. Hypothesis & Objective
When a container is configured with a strict 32MB RAM ceiling and zero swap (`memory: 32M`, `memswap_limit: 32M`), allocating physical dirty memory in 4MB chunks breaches the cgroup limit. Because swap is disabled, the Linux kernel cannot page out dirty anonymous pages to disk and invokes `out_of_memory()`. The kernel dispatches `SIGKILL` (signal 9) to the culprit process, resulting in the universal container termination status:
$$\text{Exit Code} = 128 + 9 = 137$$

### 2. Configuration & Limits
- **Compose Profile**: `oom-killer` (see [`docker-compose.yml`](./docker-compose.yml))
- **Memory Limits**: `memory: 32M`, `memswap_limit: 32M` (strictly zero swap), `cpus: '0.5'`
- **Allocation & Telemetry**: 4MB dirty chunks (4096-byte page strides forcing physical RSS faults); `/sys/fs/cgroup/memory.events` (`oom_kill` counter)

### 3. How to Run
```bash
make up EXP=oom-killer
make run SCRIPT=03_oom_killer
make down EXP=oom-killer
```

### 4. Observed Metrics & Failure Signatures
```text
[*] cgroups memory.max     : 32.0 MB (/sys/fs/cgroup/memory.max)
[*] cgroups memory.current : 3.8 MB (/sys/fs/cgroup/memory.current)
[*] Initial memory.events  : {'low': 0, 'high': 0, 'max': 0, 'oom': 0, 'oom_kill': 0}

--- Phase 1: Forking Worker Child for Controlled OOM Observation ---
  [Chunk #01] Allocated +4.0 MB (Total: 4.0 MB) | VmRSS: 5216 kB
  ...
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
1. **The Math of Exit Code 137**: Unix shells, Docker, and Kubernetes report process exit status via `128 + n` when killed by an unhandled signal $n$. Since `SIGKILL` is signal 9, $128 + 9 = 137$.
2. **Why Swap Configuration Matters**: When swap is enabled without strict `memswap_limit`, memory exhaustion triggers aggressive page thrashing rather than immediate OOM termination, spiking I/O wait times and degrading latency across the host.
3. **Cgroups v2 Telemetry (`memory.events`)**: Container runtimes (`containerd`, `CRI-O`) poll `/sys/fs/cgroup/memory.events` for the `oom_kill` counter to report `OOMKilled: true` in pod statuses.

---

## Primary Sources & Deep-Dive References

- [Linux Kernel Documentation: Control Group v2 (cgroups-v2)](https://docs.kernel.org/admin-guide/cgroup-v2.html) – The definitive Linux kernel specification for `pids.max`, `memory.max`, `memory.events`, and unified hierarchical controllers.
- [Linux Kernel Documentation: The Out-Of-Memory (OOM) Killer](https://docs.kernel.org/admin-guide/sysctl/vm.html) – Heuristics behind `oom_score`, `oom_score_adj`, and memory threshold overcommit behavior.
- [Linux Programmer's Manual: `getrlimit` / `setrlimit`](https://man7.org/linux/man-pages/man2/getrlimit.2.html) – POSIX resource limit specifications for `RLIMIT_NOFILE`, `RLIMIT_NPROC`, and `RLIMIT_AS`.
- [Linux Programmer's Manual: `proc(5)`](https://man7.org/linux/man-pages/man5/proc.5.html) – File descriptor table inspection via `/proc/[pid]/fd`, process status metrics via `/proc/[pid]/status`, and memory maps.
- Kerrisk, Michael: *The Linux Programming Interface (TLPI)* – The canonical reference on Linux process lifecycle, file descriptor tables, and IPC signals.

---

## Clean Reset & Teardown

```bash
# Tear down a specific profile container:
make down EXP=pid-limit

# Wipe all containers and networks for this lab:
make clean
```

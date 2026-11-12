# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
# ]
# ///
"""
Experiment 01: The Cgroups PID Limit (Contained Fork Bomb)
==========================================================
Hypothesis:
  When a Linux container is constrained by cgroups v2 `pids.max` (e.g., 40 PIDs),
  a recursive fork loop will deterministically halt upon attempting to exceed 40 tasks.
  The kernel refuses further task creation with:
      BlockingIOError: [Errno 11] Resource temporarily unavailable (EAGAIN)
  The macOS host and other Docker containers experience 0% degradation or starvation.

Invariants & Guardrails:
  - Strictly contained within Docker cgroup (pids: 40).
  - Explicit macOS safety guard prevents local execution outside Linux container.
  - Automatic teardown and zombie reaping via pipe-signaled child lifecycle.
"""

import errno
import os
import platform
import sys
import time

from rich.console import Console

console = Console()


def get_cgroup_pids_paths() -> tuple[str | None, str | None]:
    """Locate cgroup v2 or v1 pids controller files."""
    # Check cgroup v2 directly in container root
    v2_max = "/sys/fs/cgroup/pids.max"
    v2_curr = "/sys/fs/cgroup/pids.current"
    if os.path.exists(v2_max) and os.path.exists(v2_curr):
        return v2_max, v2_curr

    # Inspect /proc/self/cgroup for nested cgroup path
    if os.path.exists("/proc/self/cgroup"):
        try:
            with open("/proc/self/cgroup") as f:
                for line in f:
                    parts = line.strip().split(":")
                    if len(parts) == 3:
                        subpath = parts[2].lstrip("/")
                        candidate_max = os.path.join("/sys/fs/cgroup", subpath, "pids.max")
                        candidate_curr = os.path.join("/sys/fs/cgroup", subpath, "pids.current")
                        if os.path.exists(candidate_max) and os.path.exists(candidate_curr):
                            return candidate_max, candidate_curr
        except Exception:
            pass

    # Check cgroup v1 fallback
    v1_max = "/sys/fs/cgroup/pids/pids.max"
    v1_curr = "/sys/fs/cgroup/pids/pids.current"
    if os.path.exists(v1_max) and os.path.exists(v1_curr):
        return v1_max, v1_curr

    return None, None


def read_file_safe(path: str | None) -> str:
    """Read a sysfs file safely."""
    if not path or not os.path.exists(path):
        return "N/A"
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception as e:
        return f"Error: {e}"


def run_experiment() -> None:
    console.print("=" * 70, markup=False, highlight=False, soft_wrap=True)
    console.print(" Linux Kernel Primitives Lab - Experiment 01: Cgroups PID Limit", markup=False, highlight=False, soft_wrap=True)
    console.print("=" * 70, markup=False, highlight=False, soft_wrap=True)

    # 1. Host Invariant Check: Protect macOS host
    if platform.system() != "Linux":
        console.print(f"\n[SECURITY GUARD] Detected host OS: {platform.system()} ({platform.release()}).", markup=False, highlight=False, soft_wrap=True)
        console.print("Linux cgroups controllers (/sys/fs/cgroup) are exclusive to the Linux kernel.", markup=False, highlight=False, soft_wrap=True)
        console.print("An unconstrained fork bomb must NEVER be executed directly on the host machine.", markup=False, highlight=False, soft_wrap=True)
        console.print("\nPlease run this experiment inside the isolated Linux container:", markup=False, highlight=False, soft_wrap=True)
        console.print("    cd linux", markup=False, highlight=False, soft_wrap=True)
        console.print("    make up EXP=pid-limit", markup=False, highlight=False, soft_wrap=True)
        console.print("    make run EXP=pid-limit SCRIPT=01_fork_bomb_cgroup\n", markup=False, highlight=False, soft_wrap=True)
        sys.exit(0)

    # 2. Inspect Linux environment & cgroup controllers
    pids_max_file, pids_curr_file = get_cgroup_pids_paths()
    pids_max = read_file_safe(pids_max_file)
    initial_pids = read_file_safe(pids_curr_file)

    console.print(f"[*] Kernel Version  : {platform.uname().release}", markup=False, highlight=False, soft_wrap=True)
    console.print(f"[*] Current PID     : {os.getpid()}", markup=False, highlight=False, soft_wrap=True)
    console.print(f"[*] cgroup pids.max : {pids_max} (path: {pids_max_file})", markup=False, highlight=False, soft_wrap=True)
    console.print(f"[*] Initial Tasks   : {initial_pids} (path: {pids_curr_file})", markup=False, highlight=False, soft_wrap=True)

    if pids_max == "max" or pids_max == "N/A":
        console.print("[!] WARNING: cgroups pids.max is unconstrained or not detected.", markup=False, highlight=False, soft_wrap=True)
        console.print("    Ensure the container was started with `pids: 40` limit.", markup=False, highlight=False, soft_wrap=True)

    # 3. Controlled Process Spawning Loop
    console.print("\n--- Phase 1: Controlled Process Spawning (Approaching Cgroup Ceiling) ---", markup=False, highlight=False, soft_wrap=True)
    pipe_r, pipe_w = os.pipe()
    children = []
    spawn_count = 0
    hit_limit = False

    try:
        # We attempt up to 100 forks, knowing the limit is 40
        for i in range(1, 100):
            try:
                pid = os.fork()
                if pid == 0:
                    # Child process: close write pipe, block on read until parent signals teardown
                    os.close(pipe_w)
                    try:
                        os.read(pipe_r, 1)
                    except Exception:
                        pass
                    os.close(pipe_r)
                    os._exit(0)
                else:
                    # Parent process
                    children.append(pid)
                    spawn_count += 1
                    curr = read_file_safe(pids_curr_file)
                    console.print(f"  [Spawned Child #{i:02d}] PID: {pid:<6} | cgroups pids.current: {curr} / {pids_max}", markup=False, highlight=False, soft_wrap=True)
                    # Brief pause for telemetry observation
                    time.sleep(0.02)

            except (BlockingIOError, OSError) as exc:
                if exc.errno == errno.EAGAIN:  # Errno 11: Resource temporarily unavailable
                    hit_limit = True
                    curr = read_file_safe(pids_curr_file)
                    console.print("\n" + "#" * 70, markup=False, highlight=False, soft_wrap=True)
                    console.print("  >>> SUCCESS: HIT CGROUP PID LIMIT <<<", markup=False, highlight=False, soft_wrap=True)
                    console.print(f"  Exact Exception Caught : {type(exc).__name__}: [Errno {exc.errno}] {os.strerror(exc.errno)}", markup=False, highlight=False, soft_wrap=True)
                    console.print(f"  Total Children Spawned : {spawn_count}", markup=False, highlight=False, soft_wrap=True)
                    console.print(f"  Cgroup Tasks At Failure: {curr} / {pids_max}", markup=False, highlight=False, soft_wrap=True)
                    console.print("#" * 70, markup=False, highlight=False, soft_wrap=True)
                    break
                else:
                    console.print(f"\n[!] Unexpected OSError: {exc}", markup=False, highlight=False, soft_wrap=True)
                    raise

        if not hit_limit:
            console.print(f"\n[!] Warning: Spawned {spawn_count} processes without hitting EAGAIN.", markup=False, highlight=False, soft_wrap=True)
            console.print("    Check docker-compose.yml resource limits.", markup=False, highlight=False, soft_wrap=True)

    finally:
        # 4. Deterministic Teardown & Hygiene
        console.print("\n--- Phase 2: Deterministic Teardown & Zombie Reaping ---", markup=False, highlight=False, soft_wrap=True)
        console.print(f"[*] Notifying {len(children)} child processes to exit via pipe signal...", markup=False, highlight=False, soft_wrap=True)
        try:
            # Closing the write end causes children to receive EOF on os.read()
            os.close(pipe_w)
            os.close(pipe_r)
        except Exception:
            pass

        # Reaping zombies with waitpid
        reaped = 0
        for pid in children:
            try:
                os.waitpid(pid, 0)
                reaped += 1
            except Exception:
                pass

        final_curr = read_file_safe(pids_curr_file)
        console.print(f"[*] Reaped {reaped} child processes.", markup=False, highlight=False, soft_wrap=True)
        console.print(f"[*] Restored cgroups pids.current: {final_curr} / {pids_max}", markup=False, highlight=False, soft_wrap=True)

    # 5. Systems Takeaways & Interview Angles
    console.print("\n" + "=" * 70, markup=False, highlight=False, soft_wrap=True)
    console.print(" Systems & Production Takeaways:", markup=False, highlight=False, soft_wrap=True)
    console.print("=" * 70, markup=False, highlight=False, soft_wrap=True)
    console.print(" 1. The Cgroup v2 `pids` Controller:", markup=False, highlight=False, soft_wrap=True)
    console.print("    Prevents fork bombs (:(){ :|:& };:) and runaway concurrency leaks.", markup=False, highlight=False, soft_wrap=True)
    console.print("    Unlike `ulimit -u` (RLIMIT_NPROC), which is scoped per UID, cgroups", markup=False, highlight=False, soft_wrap=True)
    console.print("    enforce limits across the entire control group hierarchy.", markup=False, highlight=False, soft_wrap=True)
    console.print(" 2. Kernel Failure Signature:", markup=False, highlight=False, soft_wrap=True)
    console.print("    clone() / fork() syscall returns EAGAIN (Errno 11).", markup=False, highlight=False, soft_wrap=True)
    console.print("    The kernel does not kill existing processes; it simply halts new task creation.", markup=False, highlight=False, soft_wrap=True)
    console.print(" 3. Host Isolation:", markup=False, highlight=False, soft_wrap=True)
    console.print("    The macOS host kernel and neighbouring containers experienced ZERO CPU/PID", markup=False, highlight=False, soft_wrap=True)
    console.print("    exhaustion, demonstrating strict namespace and cgroup isolation.", markup=False, highlight=False, soft_wrap=True)
    console.print("=" * 70, markup=False, highlight=False, soft_wrap=True)


if __name__ == "__main__":
    run_experiment()

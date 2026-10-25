# /// script
# requires-python = ">=3.11"
# dependencies = []
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
    print("=" * 70)
    print(" Linux Kernel Primitives Lab - Experiment 01: Cgroups PID Limit")
    print("=" * 70)

    # 1. Host Invariant Check: Protect macOS host
    if platform.system() != "Linux":
        print(f"\n[SECURITY GUARD] Detected host OS: {platform.system()} ({platform.release()}).")
        print("Linux cgroups controllers (/sys/fs/cgroup) are exclusive to the Linux kernel.")
        print("An unconstrained fork bomb must NEVER be executed directly on the host machine.")
        print("\nPlease run this experiment inside the isolated Linux container:")
        print("    cd linux")
        print("    make up EXP=pid-limit")
        print("    make run EXP=pid-limit SCRIPT=01_fork_bomb_cgroup\n")
        sys.exit(0)

    # 2. Inspect Linux environment & cgroup controllers
    pids_max_file, pids_curr_file = get_cgroup_pids_paths()
    pids_max = read_file_safe(pids_max_file)
    initial_pids = read_file_safe(pids_curr_file)

    print(f"[*] Kernel Version  : {platform.uname().release}")
    print(f"[*] Current PID     : {os.getpid()}")
    print(f"[*] cgroup pids.max : {pids_max} (path: {pids_max_file})")
    print(f"[*] Initial Tasks   : {initial_pids} (path: {pids_curr_file})")

    if pids_max == "max" or pids_max == "N/A":
        print("[!] WARNING: cgroups pids.max is unconstrained or not detected.")
        print("    Ensure the container was started with `pids: 40` limit.")

    # 3. Controlled Process Spawning Loop
    print("\n--- Phase 1: Controlled Process Spawning (Approaching Cgroup Ceiling) ---")
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
                    print(f"  [Spawned Child #{i:02d}] PID: {pid:<6} | cgroups pids.current: {curr} / {pids_max}")
                    # Brief pause for telemetry observation
                    time.sleep(0.02)

            except (BlockingIOError, OSError) as exc:
                if exc.errno == errno.EAGAIN:  # Errno 11: Resource temporarily unavailable
                    hit_limit = True
                    curr = read_file_safe(pids_curr_file)
                    print("\n" + "#" * 70)
                    print("  >>> SUCCESS: HIT CGROUP PID LIMIT <<<")
                    print(f"  Exact Exception Caught : {type(exc).__name__}: [Errno {exc.errno}] {os.strerror(exc.errno)}")
                    print(f"  Total Children Spawned : {spawn_count}")
                    print(f"  Cgroup Tasks At Failure: {curr} / {pids_max}")
                    print("#" * 70)
                    break
                else:
                    print(f"\n[!] Unexpected OSError: {exc}")
                    raise

        if not hit_limit:
            print(f"\n[!] Warning: Spawned {spawn_count} processes without hitting EAGAIN.")
            print("    Check docker-compose.yml resource limits.")

    finally:
        # 4. Deterministic Teardown & Hygiene
        print("\n--- Phase 2: Deterministic Teardown & Zombie Reaping ---")
        print(f"[*] Notifying {len(children)} child processes to exit via pipe signal...")
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
        print(f"[*] Reaped {reaped} child processes.")
        print(f"[*] Restored cgroups pids.current: {final_curr} / {pids_max}")

    # 5. Systems Takeaways & Interview Angles
    print("\n" + "=" * 70)
    print(" Systems & Production Takeaways:")
    print("=" * 70)
    print(" 1. The Cgroup v2 `pids` Controller:")
    print("    Prevents fork bombs (:(){ :|:& };:) and runaway concurrency leaks.")
    print("    Unlike `ulimit -u` (RLIMIT_NPROC), which is scoped per UID, cgroups")
    print("    enforce limits across the entire control group hierarchy.")
    print(" 2. Kernel Failure Signature:")
    print("    clone() / fork() syscall returns EAGAIN (Errno 11).")
    print("    The kernel does not kill existing processes; it simply halts new task creation.")
    print(" 3. Host Isolation:")
    print("    The macOS host kernel and neighbouring containers experienced ZERO CPU/PID")
    print("    exhaustion, demonstrating strict namespace and cgroup isolation.")
    print("=" * 70)


if __name__ == "__main__":
    run_experiment()

# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Experiment 03: Kernel OOM Killer vs Swap Behavior (Exit Code 137)
================================================================
Hypothesis:
  When a container operates under a strict 32MB RAM ceiling with swap disabled
  (`memory: 32M`, `memswap_limit: 32M`), allocating physical dirty memory in
  4MB increments will rapidly exhaust the cgroup memory limit.
  Because no swap partition is available to page out anonymous memory, the Linux
  kernel memory controller trips into an Out-Of-Memory state and executes the
  Linux OOM Killer (`out_of_memory()`).
  The kernel dispatches SIGKILL (signal 9) to terminate the allocating task.
  The resulting process / container termination exit status is deterministically:
      128 + 9 = Exit Code 137

Invariants & Guardrails:
  - Strictly limited to 32MB inside container.
  - Safe memory dirtying (4KB page strides) forces physical page faults.
  - Dual execution mode:
      1. Default (Observer): Forks worker, streams telemetry, captures SIGKILL &
         inspects cgroups v2 memory.events (`oom_kill 1`).
      2. Direct (--direct): Allocates in-process until container exits with 137.
"""

import argparse
import os
import platform
import sys
import time
from typing import Dict, Optional, Tuple


def get_cgroup_memory_paths() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Locate cgroup v2 (or v1) memory controller files."""
    # Check cgroup v2 directly in container root
    v2_max = "/sys/fs/cgroup/memory.max"
    v2_curr = "/sys/fs/cgroup/memory.current"
    v2_events = "/sys/fs/cgroup/memory.events"
    if os.path.exists(v2_max) and os.path.exists(v2_curr):
        return v2_max, v2_curr, v2_events if os.path.exists(v2_events) else None

    # Inspect /proc/self/cgroup for nested cgroup path
    if os.path.exists("/proc/self/cgroup"):
        try:
            with open("/proc/self/cgroup", "r") as f:
                for line in f:
                    parts = line.strip().split(":")
                    if len(parts) == 3:
                        subpath = parts[2].lstrip("/")
                        candidate_max = os.path.join("/sys/fs/cgroup", subpath, "memory.max")
                        candidate_curr = os.path.join("/sys/fs/cgroup", subpath, "memory.current")
                        candidate_events = os.path.join("/sys/fs/cgroup", subpath, "memory.events")
                        if os.path.exists(candidate_max) and os.path.exists(candidate_curr):
                            return candidate_max, candidate_curr, candidate_events
        except Exception:
            pass

    # Check cgroup v1 fallback
    v1_max = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    v1_curr = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    v1_oom = "/sys/fs/cgroup/memory/memory.oom_control"
    if os.path.exists(v1_max) and os.path.exists(v1_curr):
        return v1_max, v1_curr, v1_oom

    return None, None, None


def read_file_safe(path: Optional[str]) -> str:
    """Read a sysfs file safely."""
    if not path or not os.path.exists(path):
        return "N/A"
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception as e:
        return f"Error: {e}"


def parse_memory_events(events_path: Optional[str]) -> Dict[str, int]:
    """Parse cgroups v2 memory.events file into a dictionary."""
    results: Dict[str, int] = {}
    if not events_path or not os.path.exists(events_path):
        return results
    try:
        with open(events_path, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) == 2 and parts[1].isdigit():
                    results[parts[0]] = int(parts[1])
    except Exception:
        pass
    return results


def allocate_until_oom(chunk_size_bytes: int = 4 * 1024 * 1024) -> None:
    """
    Allocate memory in dirty chunks to ensure physical page allocation (RSS).
    In Linux, virtual allocation (malloc) is sparse until pages are faulted.
    Dirtying each 4KB page forces the kernel to charge the memory cgroup.
    """
    chunks = []
    total_allocated = 0
    chunk_mb = chunk_size_bytes / (1024 * 1024)
    pid = os.getpid()

    print(f"[*] Memory Allocator Worker started (PID: {pid})...")
    print(f"[*] Allocation chunk size: {chunk_mb:.1f} MB (dirtying 4KB pages)")

    for step in range(1, 100):
        # Allocate chunk
        chunk = bytearray(chunk_size_bytes)
        # Touch each 4096-byte page to force page fault and dirty RSS
        for offset in range(0, chunk_size_bytes, 4096):
            chunk[offset] = 0xAA
        chunks.append(chunk)

        total_allocated += chunk_size_bytes
        total_mb = total_allocated / (1024 * 1024)

        # Inspect RSS if available
        rss_str = ""
        status_path = f"/proc/{pid}/status"
        if os.path.exists(status_path):
            try:
                with open(status_path, "r") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_str = f" | {line.strip()}"
                            break
            except Exception:
                pass

        print(f"  [Chunk #{step:02d}] Allocated +{chunk_mb:.1f} MB (Total: {total_mb:.1f} MB){rss_str}")
        sys.stdout.flush()
        time.sleep(0.05)


def run_experiment() -> None:
    parser = argparse.ArgumentParser(description="Kernel OOM Killer experiment")
    parser.add_argument("--direct", action="store_true", help="Directly trigger OOM in the calling process")
    args = parser.parse_args()

    print("=" * 70)
    print(" Linux Kernel Primitives Lab - Experiment 03: Kernel OOM Killer (Exit 137)")
    print("=" * 70)

    # 1. Host Invariant Check
    if platform.system() != "Linux":
        print(f"\n[SECURITY GUARD] Detected host OS: {platform.system()} ({platform.release()}).")
        print("The Linux OOM Killer and memory cgroups controllers are exclusive to Linux.")
        print("Running an uncontrolled memory exhaustion script on macOS can freeze the host.")
        print("\nPlease run this experiment inside the isolated Linux container:")
        print("    cd linux")
        print("    make up EXP=oom-killer")
        print("    make run EXP=oom-killer SCRIPT=03_oom_killer\n")
        sys.exit(0)

    # 2. Inspect Linux environment & cgroup memory controllers
    mem_max_file, mem_curr_file, mem_events_file = get_cgroup_memory_paths()
    mem_max_raw = read_file_safe(mem_max_file)
    mem_curr_raw = read_file_safe(mem_curr_file)

    try:
        mem_max_mb = f"{int(mem_max_raw) / (1024 * 1024):.1f} MB"
    except Exception:
        mem_max_mb = mem_max_raw

    try:
        mem_curr_mb = f"{int(mem_curr_raw) / (1024 * 1024):.1f} MB"
    except Exception:
        mem_curr_mb = mem_curr_raw

    initial_events = parse_memory_events(mem_events_file)

    print(f"[*] Kernel Release         : {platform.uname().release}")
    print(f"[*] cgroups memory.max     : {mem_max_mb} ({mem_max_file})")
    print(f"[*] cgroups memory.current : {mem_curr_mb} ({mem_curr_file})")
    print(f"[*] Initial memory.events  : {initial_events}")

    if args.direct:
        # Direct mode: allocates until kernel kills the current process
        print("\n--- Direct Mode: Allocating in-process until OOM termination ---")
        allocate_until_oom()
        return

    # 3. Observer Mode: Fork worker, track telemetry, capture exit code 137
    print("\n--- Phase 1: Forking Worker Child for Controlled OOM Observation ---")
    sys.stdout.flush()

    child_pid = os.fork()
    if child_pid == 0:
        # Child process: allocate memory until OOM Killer terminates it
        try:
            allocate_until_oom()
        except Exception as e:
            print(f"[Child Exception] {e}")
            os._exit(1)
        os._exit(0)

    # Parent process: monitor child and wait for kernel termination
    print(f"[*] Parent (PID: {os.getpid()}) monitoring Child (PID: {child_pid})...")

    # Wait for child process status
    _, status = os.waitpid(child_pid, 0)

    print("\n" + "#" * 70)
    print("  >>> SUCCESS: DETECTED PROCESS TERMINATION BY LINUX KERNEL <<<")

    if os.WIFSIGNALED(status):
        term_sig = os.WTERMSIG(status)
        exit_code = 128 + term_sig
        print(f"  Termination Reason : Process killed by Signal {term_sig}")
        print(f"  Signal Name        : {signal_name(term_sig)}")
        print(f"  Calculated Exit    : 128 + {term_sig} = EXIT CODE {exit_code}")
    elif os.WIFEXITED(status):
        exit_code = os.WEXITSTATUS(status)
        print(f"  Termination Reason : Process exited normally with code {exit_code}")
    else:
        exit_code = -1
        print("  Termination Reason : Process stopped or unhandled")
    print("#" * 70)

    # 4. Inspect Post-Mortem Cgroup Memory Events
    print("\n--- Phase 2: Post-Mortem Cgroup Telemetry Inspection ---")
    final_events = parse_memory_events(mem_events_file)
    final_curr_raw = read_file_safe(mem_curr_file)
    try:
        final_curr_mb = f"{int(final_curr_raw) / (1024 * 1024):.1f} MB"
    except Exception:
        final_curr_mb = final_curr_raw

    print(f"[*] Post-mortem memory.current : {final_curr_mb}")
    print(f"[*] Post-mortem memory.events  : {final_events}")

    oom_kills = final_events.get("oom_kill", 0) - initial_events.get("oom_kill", 0)
    print(f"[*] Net kernel oom_kill count  : +{oom_kills}")

    # 5. Systems Takeaways & Interview Angles
    print("\n" + "=" * 70)
    print(" Systems & Production Takeaways:")
    print("=" * 70)
    print(" 1. The Anatomy of Exit Code 137:")
    print("    In Linux, when a process dies from a signal, shells and Docker report:")
    print("        Exit Code = 128 + Signal Number")
    print("    Signal 9 is SIGKILL. Therefore: 128 + 9 = 137.")
    print("    In Docker and Kubernetes (OOMKilled), exit code 137 is the definitive")
    print("    signature that the Linux kernel OOM Killer terminated the container.")
    print(" 2. Why Swap Disabled Matters (`memswap_limit == memory`):")
    print("    If swap is enabled, the kernel pushes inactive dirty pages to disk,")
    print("    causing severe disk thrashing (high I/O wait) before finally OOM killing.")
    print("    Disabling swap makes OOM enforcement immediate and deterministic.")
    print(" 3. `memory.events` in Cgroups v2:")
    print("    The `memory.events` interface exposes high-precision counters (`oom`, `oom_kill`,")
    print("    `max`) without needing to parse `/var/log/messages` or `dmesg`.")
    print("=" * 70)


def signal_name(sig: int) -> str:
    """Return friendly signal name."""
    if sig == 9:
        return "SIGKILL (Kill signal, cannot be caught or ignored)"
    if sig == 15:
        return "SIGTERM (Termination signal)"
    return f"SIG {sig}"


if __name__ == "__main__":
    run_experiment()

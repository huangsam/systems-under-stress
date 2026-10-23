# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Experiment 02: File Descriptor & Inode Exhaustion (ulimit -n 64)
==============================================================
Hypothesis:
  When a process operates under a constrained file descriptor limit (`ulimit -n 64`
  or `RLIMIT_NOFILE = (64, 64)`), opening unclosed file descriptors in a loop
  will exhaust the per-process descriptor table.
  The kernel refuses further allocation with:
      OSError: [Errno 24] Too many open files (EMFILE)
  Real-time inspection of `/proc/self/fd/` reveals how file descriptors map
  to open files, pipes, and sockets until the table ceiling is reached.

Invariants & Guardrails:
  - Bounded to 64 file descriptors.
  - Inspects `/proc/self/fd/` safely.
  - Cleanly closes all allocated descriptors during teardown.
"""

import errno
import os
import platform
import resource


def inspect_proc_self_fd() -> dict[int, str]:
    """Inspect and resolve all open file descriptors for the current process."""
    fd_dir = "/proc/self/fd"
    if not os.path.exists(fd_dir):
        # Fallback for macOS / BSD systems
        fd_dir = "/dev/fd"

    results = {}
    if os.path.exists(fd_dir):
        try:
            for entry in os.listdir(fd_dir):
                if entry.isdigit():
                    fd_num = int(entry)
                    fd_path = os.path.join(fd_dir, entry)
                    try:
                        target = os.readlink(fd_path)
                    except Exception:
                        target = "[unresolvable]"
                    results[fd_num] = target
        except Exception as e:
            results[-1] = f"Error reading fd dir: {e}"
    return results


def read_sysfs_safe(path: str) -> str:
    """Read a kernel sysctl/sysfs file."""
    if os.path.exists(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception as e:
            return f"Error: {e}"
    return "N/A"


def run_experiment() -> None:
    print("=" * 70)
    print(" Linux Kernel Primitives Lab - Experiment 02: FD Exhaustion (ulimit -n 64)")
    print("=" * 70)

    # 1. Inspect initial environment
    soft_before, hard_before = resource.getrlimit(resource.RLIMIT_NOFILE)
    print(f"[*] Kernel System   : {platform.system()} {platform.release()}")
    print(f"[*] Current PID     : {os.getpid()}")
    print(f"[*] Initial RLIMIT  : soft={soft_before}, hard={hard_before}")

    # 2. Configure target limit to 64
    TARGET_LIMIT = 64
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (TARGET_LIMIT, TARGET_LIMIT))
        soft_cur, hard_cur = resource.getrlimit(resource.RLIMIT_NOFILE)
        print(f"[*] Enforced RLIMIT : soft={soft_cur}, hard={hard_cur}")
    except Exception as exc:
        print(f"[!] Warning: Could not adjust RLIMIT_NOFILE to {TARGET_LIMIT}: {exc}")
        TARGET_LIMIT = soft_before

    # 3. Baseline /proc/self/fd inspection
    print("\n--- Phase 1: Baseline /proc/self/fd Inspection ---")
    baseline_fds = inspect_proc_self_fd()
    print(f"[*] Baseline open descriptors ({len(baseline_fds)} active):")
    for fd, target in sorted(baseline_fds.items()):
        print(f"    fd {fd:>2} -> {target}")

    # 4. Descriptor Exhaustion Loop
    print(f"\n--- Phase 2: Opening Descriptors Until Limit ({TARGET_LIMIT}) ---")
    opened_files: list[object] = []
    hit_emfile = False

    try:
        for iteration in range(1, 100):
            try:
                target_path = "/dev/null"
                f = open(target_path)
                opened_files.append(f)
                fd_num = f.fileno()

                if iteration % 10 == 0 or iteration >= 55:
                    print(f"  [Allocated FD] Index #{iteration:02d} -> Assigned fd={fd_num:>2}")

            except OSError as exc:
                if exc.errno == errno.EMFILE:  # Errno 24: Too many open files
                    hit_emfile = True
                    print("\n" + "#" * 70)
                    print("  >>> SUCCESS: HIT PROCESS FILE DESCRIPTOR CEILING <<<")
                    print(f"  Exact Exception Caught : {type(exc).__name__}: [Errno {exc.errno}] {os.strerror(exc.errno)}")
                    print(f"  Total Open Files Held  : {len(opened_files)}")
                    print(f"  Failed Iteration Index : #{iteration}")
                    print("#" * 70)
                    break
                else:
                    print(f"\n[!] Unexpected OSError: {exc}")
                    raise

        if not hit_emfile:
            print(f"\n[!] Warning: Opened {len(opened_files)} files without hitting EMFILE.")

        # 5. Inspection at Capacity
        print("\n--- Phase 3: Inspecting FD Table at Capacity ---")
        print("[*] Note: Attempting opendir('/proc/self/fd') while at 100% capacity:")
        test_inspect = inspect_proc_self_fd()
        if -1 in test_inspect:
            print(f"    Expected Insight: {test_inspect[-1]}")
            print("    (When the FD table is completely full, even opendir() cannot allocate an FD!)")
            print("[*] Releasing exactly 1 FD to inspect /proc/self/fd snapshot...")
            if opened_files:
                last_f = opened_files.pop()
                last_f.close()

        exhausted_fds = inspect_proc_self_fd()
        print(f"[*] Active entries in fd table ({len(exhausted_fds)} active):")
        for fd, target in sorted(exhausted_fds.items()):
            if fd >= 0:
                print(f"    fd {fd:>2} -> {target}")

        # 6. System-Wide vs Process-Level Comparison
        if platform.system() == "Linux":
            file_nr = read_sysfs_safe("/proc/sys/fs/file-nr")
            file_max = read_sysfs_safe("/proc/sys/fs/file-max")
            print("\n--- Phase 4: Kernel System-Wide Limits (/proc/sys/fs/) ---")
            print(f"[*] /proc/sys/fs/file-nr  : {file_nr}")
            print("    (Format: <allocated_handles>  <allocated_unused>  <max_handles>)")
            print(f"[*] /proc/sys/fs/file-max : {file_max}")

    finally:
        # 7. Cleanup & Teardown
        print("\n--- Phase 5: Resource Cleanup & FD Table Release ---")
        closed_count = 0
        for f in opened_files:
            try:
                f.close()
                closed_count += 1
            except Exception:
                pass
        opened_files.clear()

        restored_fds = inspect_proc_self_fd()
        print("[*] Successfully closed remaining leaked file descriptors.")
        print(f"[*] Restored open descriptors in /proc/self/fd: {len(restored_fds)} active")

    # 8. Systems Takeaways & Interview Angles
    print("\n" + "=" * 70)
    print(" Systems & Production Takeaways:")
    print("=" * 70)
    print(" 1. EMFILE vs ENFILE:")
    print("    - EMFILE (Errno 24: 'Too many open files'): Process hit its local `ulimit -n`")
    print("      or `RLIMIT_NOFILE` limit. Other processes on the host can still open files.")
    print("    - ENFILE (Errno 23: 'File table overflow'): Entire operating system kernel")
    print("      exhausted global file handles (`fs.file-max`). Host-wide catastrophe.")
    print(" 2. Microservice Failure Modes:")
    print("    - Leaking TCP sockets, unclosed HTTP responses, or dangling database connections")
    print("      cause network servers (e.g. Nginx, Go HTTP, Node.js) to hit EMFILE.")
    print("    - Result: `accept()` syscall fails with EMFILE, dropping incoming user traffic.")
    print(" 3. Observability in Production:")
    print("    - `/proc/<pid>/fd/` gives immediate, zero-overhead visibility into every open")
    print("      handle without needing third-party binaries or intrusive ptrace debugging.")
    print("=" * 70)


if __name__ == "__main__":
    run_experiment()

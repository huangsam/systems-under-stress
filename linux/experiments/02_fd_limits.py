# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
# ]
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

from rich.console import Console

console = Console()


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
    console.print("=" * 70, markup=False, highlight=False, soft_wrap=True)
    console.print(" Linux Kernel Primitives Lab - Experiment 02: FD Exhaustion (ulimit -n 64)", markup=False, highlight=False, soft_wrap=True)
    console.print("=" * 70, markup=False, highlight=False, soft_wrap=True)

    # 1. Inspect initial environment
    soft_before, hard_before = resource.getrlimit(resource.RLIMIT_NOFILE)
    console.print(f"[*] Kernel System   : {platform.system()} {platform.release()}", markup=False, highlight=False, soft_wrap=True)
    console.print(f"[*] Current PID     : {os.getpid()}", markup=False, highlight=False, soft_wrap=True)
    console.print(f"[*] Initial RLIMIT  : soft={soft_before}, hard={hard_before}", markup=False, highlight=False, soft_wrap=True)

    # 2. Configure target limit to 64
    TARGET_LIMIT = 64
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (TARGET_LIMIT, TARGET_LIMIT))
        soft_cur, hard_cur = resource.getrlimit(resource.RLIMIT_NOFILE)
        console.print(f"[*] Enforced RLIMIT : soft={soft_cur}, hard={hard_cur}", markup=False, highlight=False, soft_wrap=True)
    except Exception as exc:
        console.print(f"[!] Warning: Could not adjust RLIMIT_NOFILE to {TARGET_LIMIT}: {exc}", markup=False, highlight=False, soft_wrap=True)
        TARGET_LIMIT = soft_before

    # 3. Baseline /proc/self/fd inspection
    console.print("\n--- Phase 1: Baseline /proc/self/fd Inspection ---", markup=False, highlight=False, soft_wrap=True)
    baseline_fds = inspect_proc_self_fd()
    console.print(f"[*] Baseline open descriptors ({len(baseline_fds)} active):", markup=False, highlight=False, soft_wrap=True)
    for fd, target in sorted(baseline_fds.items()):
        console.print(f"    fd {fd:>2} -> {target}", markup=False, highlight=False, soft_wrap=True)

    # 4. Descriptor Exhaustion Loop
    console.print(f"\n--- Phase 2: Opening Descriptors Until Limit ({TARGET_LIMIT}) ---", markup=False, highlight=False, soft_wrap=True)
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
                    console.print(f"  [Allocated FD] Index #{iteration:02d} -> Assigned fd={fd_num:>2}", markup=False, highlight=False, soft_wrap=True)

            except OSError as exc:
                if exc.errno == errno.EMFILE:  # Errno 24: Too many open files
                    hit_emfile = True
                    console.print("\n" + "#" * 70, markup=False, highlight=False, soft_wrap=True)
                    console.print("  >>> SUCCESS: HIT PROCESS FILE DESCRIPTOR CEILING <<<", markup=False, highlight=False, soft_wrap=True)
                    console.print(f"  Exact Exception Caught : {type(exc).__name__}: [Errno {exc.errno}] {os.strerror(exc.errno)}", markup=False, highlight=False, soft_wrap=True)
                    console.print(f"  Total Open Files Held  : {len(opened_files)}", markup=False, highlight=False, soft_wrap=True)
                    console.print(f"  Failed Iteration Index : #{iteration}", markup=False, highlight=False, soft_wrap=True)
                    console.print("#" * 70, markup=False, highlight=False, soft_wrap=True)
                    break
                else:
                    console.print(f"\n[!] Unexpected OSError: {exc}", markup=False, highlight=False, soft_wrap=True)
                    raise

        if not hit_emfile:
            console.print(f"\n[!] Warning: Opened {len(opened_files)} files without hitting EMFILE.", markup=False, highlight=False, soft_wrap=True)

        # 5. Inspection at Capacity
        console.print("\n--- Phase 3: Inspecting FD Table at Capacity ---", markup=False, highlight=False, soft_wrap=True)
        console.print("[*] Note: Attempting opendir('/proc/self/fd') while at 100% capacity:", markup=False, highlight=False, soft_wrap=True)
        test_inspect = inspect_proc_self_fd()
        if -1 in test_inspect:
            console.print(f"    Expected Insight: {test_inspect[-1]}", markup=False, highlight=False, soft_wrap=True)
            console.print("    (When the FD table is completely full, even opendir() cannot allocate an FD!)", markup=False, highlight=False, soft_wrap=True)
            console.print("[*] Releasing exactly 1 FD to inspect /proc/self/fd snapshot...", markup=False, highlight=False, soft_wrap=True)
            if opened_files:
                last_f = opened_files.pop()
                last_f.close()

        exhausted_fds = inspect_proc_self_fd()
        console.print(f"[*] Active entries in fd table ({len(exhausted_fds)} active):", markup=False, highlight=False, soft_wrap=True)
        for fd, target in sorted(exhausted_fds.items()):
            if fd >= 0:
                console.print(f"    fd {fd:>2} -> {target}", markup=False, highlight=False, soft_wrap=True)

        # 6. System-Wide vs Process-Level Comparison
        if platform.system() == "Linux":
            file_nr = read_sysfs_safe("/proc/sys/fs/file-nr")
            file_max = read_sysfs_safe("/proc/sys/fs/file-max")
            console.print("\n--- Phase 4: Kernel System-Wide Limits (/proc/sys/fs/) ---", markup=False, highlight=False, soft_wrap=True)
            console.print(f"[*] /proc/sys/fs/file-nr  : {file_nr}", markup=False, highlight=False, soft_wrap=True)
            console.print("    (Format: <allocated_handles>  <allocated_unused>  <max_handles>)", markup=False, highlight=False, soft_wrap=True)
            console.print(f"[*] /proc/sys/fs/file-max : {file_max}", markup=False, highlight=False, soft_wrap=True)

    finally:
        # 7. Cleanup & Teardown
        console.print("\n--- Phase 5: Resource Cleanup & FD Table Release ---", markup=False, highlight=False, soft_wrap=True)
        closed_count = 0
        for f in opened_files:
            try:
                f.close()
                closed_count += 1
            except Exception:
                pass
        opened_files.clear()

        restored_fds = inspect_proc_self_fd()
        console.print("[*] Successfully closed remaining leaked file descriptors.", markup=False, highlight=False, soft_wrap=True)
        console.print(f"[*] Restored open descriptors in /proc/self/fd: {len(restored_fds)} active", markup=False, highlight=False, soft_wrap=True)

    # 8. Systems Takeaways & Interview Angles
    console.print("\n" + "=" * 70, markup=False, highlight=False, soft_wrap=True)
    console.print(" Systems & Production Takeaways:", markup=False, highlight=False, soft_wrap=True)
    console.print("=" * 70, markup=False, highlight=False, soft_wrap=True)
    console.print(" 1. EMFILE vs ENFILE:", markup=False, highlight=False, soft_wrap=True)
    console.print("    - EMFILE (Errno 24: 'Too many open files'): Process hit its local `ulimit -n`", markup=False, highlight=False, soft_wrap=True)
    console.print("      or `RLIMIT_NOFILE` limit. Other processes on the host can still open files.", markup=False, highlight=False, soft_wrap=True)
    console.print("    - ENFILE (Errno 23: 'File table overflow'): Entire operating system kernel", markup=False, highlight=False, soft_wrap=True)
    console.print("      exhausted global file handles (`fs.file-max`). Host-wide catastrophe.", markup=False, highlight=False, soft_wrap=True)
    console.print(" 2. Microservice Failure Modes:", markup=False, highlight=False, soft_wrap=True)
    console.print("    - Leaking TCP sockets, unclosed HTTP responses, or dangling database connections", markup=False, highlight=False, soft_wrap=True)
    console.print("      cause network servers (e.g. Nginx, Go HTTP, Node.js) to hit EMFILE.", markup=False, highlight=False, soft_wrap=True)
    console.print("    - Result: `accept()` syscall fails with EMFILE, dropping incoming user traffic.", markup=False, highlight=False, soft_wrap=True)
    console.print(" 3. Observability in Production:", markup=False, highlight=False, soft_wrap=True)
    console.print("    - `/proc/<pid>/fd/` gives immediate, zero-overhead visibility into every open", markup=False, highlight=False, soft_wrap=True)
    console.print("      handle without needing third-party binaries or intrusive ptrace debugging.", markup=False, highlight=False, soft_wrap=True)
    console.print("=" * 70, markup=False, highlight=False, soft_wrap=True)


if __name__ == "__main__":
    run_experiment()

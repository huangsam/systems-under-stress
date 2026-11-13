# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
# ]
# ///
"""
Linux Kernel Primitives & Container Limits Crash Course

Demonstrates:
1. Kernel and OS environment inspection (/proc/version, uname).
2. Process namespace & memory mapping (/proc/self/status, VmRSS, Threads).
3. Process-level POSIX limits (getrlimit: NOFILE, NPROC, AS).
4. Cgroups v2 hierarchy (/sys/fs/cgroup: pids.max, memory.max, cpu.max).
5. File descriptor table mechanics (/proc/self/fd inspection).
"""

from __future__ import annotations

import os
import platform
import resource
import sys

from rich.console import Console
from rich.markup import escape

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def read_file_safe(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def main() -> None:
    plain_console.print("\n" + "=" * 70)
    plain_console.print("Linux Kernel Primitives & Container Limits Crash Course")
    plain_console.print("=" * 70 + "\n")

    current_os = platform.system()
    if current_os != "Linux":
        plain_console.print(f"[!] Note: Running on {current_os} host. Linux kernel inspection paths (/proc, /sys) require container execution.")
        plain_console.print("    Execute inside the lab container: cd linux && make up && make run SCRIPT=00_crash_course\n")

    # 1. Kernel & Host
    plain_console.print("[*] 1. Kernel Runtime & Architecture")
    uname = platform.uname()
    verify_phase(bool(uname.system) and bool(uname.release), "Phase 1 Kernel", f"missing uname metadata: {uname!r}")
    plain_console.print(f"    System: {uname.system} {uname.release} ({uname.machine})")
    proc_ver = read_file_safe("/proc/version")
    if current_os == "Linux":
        verify_phase(bool(proc_ver), "Phase 1 /proc/version", "expected readable /proc/version inside Linux container")
    if proc_ver:
        plain_console.print(f"    Kernel Build: {proc_ver.split('(')[0].strip()}")

    # 2. Process Namespace
    plain_console.print("\n[*] 2. Process Namespace & Status (/proc/self/status)")
    pid = os.getpid()
    ppid = os.getppid()
    verify_phase(pid > 0, "Phase 2 PID", f"expected positive PID, got {pid}")
    plain_console.print(f"    Current PID: {pid} (Parent PID: {ppid})")

    status = read_file_safe("/proc/self/status")
    status_dict: dict[str, str] = {}
    if status:
        for line in status.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                status_dict[k.strip()] = v.strip()
        for metric in ["Name", "State", "Threads", "VmSize", "VmRSS"]:
            if metric in status_dict:
                plain_console.print(f"    {metric:<10}: {status_dict[metric]}")
    if current_os == "Linux":
        verify_phase(bool(status) and "Name" in status_dict, "Phase 2 /proc/self/status", "expected process metadata in /proc/self/status")

    # 3. Process POSIX Limits
    plain_console.print("\n[*] 3. POSIX Resource Limits (getrlimit)")
    for res_name, res_id in [
        ("RLIMIT_NOFILE", resource.RLIMIT_NOFILE),
        ("RLIMIT_NPROC", getattr(resource, "RLIMIT_NPROC", None)),
        ("RLIMIT_AS", getattr(resource, "RLIMIT_AS", None)),
    ]:
        if res_id is not None:
            soft, hard = resource.getrlimit(res_id)
            if res_name == "RLIMIT_NOFILE":
                verify_phase(soft > 0 and hard > 0, "Phase 3 RLIMIT_NOFILE", f"expected positive NOFILE limits, got soft={soft}, hard={hard}")
            soft_str = "unlimited" if soft == resource.RLIM_INFINITY else str(soft)
            hard_str = "unlimited" if hard == resource.RLIM_INFINITY else str(hard)
            plain_console.print(f"    {res_name:<15}: soft={soft_str:<12} hard={hard_str}")

    # 4. Cgroups v2
    plain_console.print("\n[*] 4. Cgroups v2 Subsystem (/sys/fs/cgroup)")
    cgroup_controllers = read_file_safe("/sys/fs/cgroup/cgroup.controllers")
    if current_os == "Linux":
        verify_phase(cgroup_controllers is not None, "Phase 4 Cgroups v2", "expected /sys/fs/cgroup/cgroup.controllers in Linux container")
    if cgroup_controllers:
        plain_console.print(f"    Available Controllers: {cgroup_controllers}")

    pids_max = read_file_safe("/sys/fs/cgroup/pids.max")
    pids_cur = read_file_safe("/sys/fs/cgroup/pids.current")
    if pids_max and pids_cur:
        plain_console.print(f"    PIDs Limit: {pids_cur} / {pids_max} processes")

    mem_max = read_file_safe("/sys/fs/cgroup/memory.max")
    mem_cur = read_file_safe("/sys/fs/cgroup/memory.current")
    if mem_max and mem_cur:
        mem_max_fmt = f"{int(mem_max) // (1024 * 1024)} MB" if mem_max.isdigit() else mem_max
        mem_cur_fmt = f"{int(mem_cur) // (1024 * 1024)} MB" if mem_cur.isdigit() else mem_cur
        plain_console.print(f"    Memory Limit: {mem_cur_fmt} / {mem_max_fmt}")

    cpu_max = read_file_safe("/sys/fs/cgroup/cpu.max")
    if cpu_max:
        quota, period = cpu_max.split()
        if quota == "max":
            plain_console.print(f"    CPU Limit: unconstrained (period={period}us)")
        else:
            cpus = round(int(quota) / int(period), 2)
            plain_console.print(f"    CPU Limit: {cpus} cores (quota={quota}us, period={period}us)")

    # 5. File Descriptors
    plain_console.print("\n[*] 5. Open File Descriptors (/proc/self/fd)")
    fd_dir = "/proc/self/fd"
    if os.path.isdir(fd_dir):
        fds = os.listdir(fd_dir)
        verify_phase(len(fds) >= 3, "Phase 5 FDs", f"expected >= 3 open descriptors in /proc/self/fd, got {len(fds)}")
        plain_console.print(f"    Active Open Descriptors: {len(fds)} ({', '.join(fds[:8])}{'...' if len(fds) > 8 else ''})")
    else:
        verify_phase(current_os != "Linux", "Phase 5 FDs", "expected /proc/self/fd directory on Linux")
        plain_console.print("    (/proc/self/fd not present in current environment)")

    plain_console.print("\n[✓] Linux Kernel Crash Course completed successfully!\n")


if __name__ == "__main__":
    main()

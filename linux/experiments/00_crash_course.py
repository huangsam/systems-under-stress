# /// script
# requires-python = ">=3.11"
# dependencies = []
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


def read_file_safe(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def main() -> None:
    print("\n" + "=" * 70)
    print("Linux Kernel Primitives & Container Limits Crash Course")
    print("=" * 70 + "\n")

    current_os = platform.system()
    if current_os != "Linux":
        print(f"[!] Note: Running on {current_os} host. Linux kernel inspection paths (/proc, /sys) require container execution.")
        print("    Execute inside the lab container: cd linux && make up && make run SCRIPT=00_crash_course\n")

    # 1. Kernel & Host
    print("[*] 1. Kernel Runtime & Architecture")
    uname = platform.uname()
    print(f"    System: {uname.system} {uname.release} ({uname.machine})")
    proc_ver = read_file_safe("/proc/version")
    if proc_ver:
        print(f"    Kernel Build: {proc_ver.split('(')[0].strip()}")

    # 2. Process Namespace
    print("\n[*] 2. Process Namespace & Status (/proc/self/status)")
    pid = os.getpid()
    ppid = os.getppid()
    print(f"    Current PID: {pid} (Parent PID: {ppid})")

    status = read_file_safe("/proc/self/status")
    if status:
        status_dict = {}
        for line in status.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                status_dict[k.strip()] = v.strip()
        for metric in ["Name", "State", "Threads", "VmSize", "VmRSS"]:
            if metric in status_dict:
                print(f"    {metric:<10}: {status_dict[metric]}")

    # 3. Process POSIX Limits
    print("\n[*] 3. POSIX Resource Limits (getrlimit)")
    for res_name, res_id in [
        ("RLIMIT_NOFILE", resource.RLIMIT_NOFILE),
        ("RLIMIT_NPROC", getattr(resource, "RLIMIT_NPROC", None)),
        ("RLIMIT_AS", getattr(resource, "RLIMIT_AS", None)),
    ]:
        if res_id is not None:
            soft, hard = resource.getrlimit(res_id)
            soft_str = "unlimited" if soft == resource.RLIM_INFINITY else str(soft)
            hard_str = "unlimited" if hard == resource.RLIM_INFINITY else str(hard)
            print(f"    {res_name:<15}: soft={soft_str:<12} hard={hard_str}")

    # 4. Cgroups v2
    print("\n[*] 4. Cgroups v2 Subsystem (/sys/fs/cgroup)")
    cgroup_controllers = read_file_safe("/sys/fs/cgroup/cgroup.controllers")
    if cgroup_controllers:
        print(f"    Available Controllers: {cgroup_controllers}")

    pids_max = read_file_safe("/sys/fs/cgroup/pids.max")
    pids_cur = read_file_safe("/sys/fs/cgroup/pids.current")
    if pids_max and pids_cur:
        print(f"    PIDs Limit: {pids_cur} / {pids_max} processes")

    mem_max = read_file_safe("/sys/fs/cgroup/memory.max")
    mem_cur = read_file_safe("/sys/fs/cgroup/memory.current")
    if mem_max and mem_cur:
        mem_max_fmt = f"{int(mem_max) // (1024 * 1024)} MB" if mem_max.isdigit() else mem_max
        mem_cur_fmt = f"{int(mem_cur) // (1024 * 1024)} MB" if mem_cur.isdigit() else mem_cur
        print(f"    Memory Limit: {mem_cur_fmt} / {mem_max_fmt}")

    cpu_max = read_file_safe("/sys/fs/cgroup/cpu.max")
    if cpu_max:
        quota, period = cpu_max.split()
        if quota == "max":
            print(f"    CPU Limit: unconstrained (period={period}us)")
        else:
            cpus = round(int(quota) / int(period), 2)
            print(f"    CPU Limit: {cpus} cores (quota={quota}us, period={period}us)")

    # 5. File Descriptors
    print("\n[*] 5. Open File Descriptors (/proc/self/fd)")
    fd_dir = "/proc/self/fd"
    if os.path.isdir(fd_dir):
        fds = os.listdir(fd_dir)
        print(f"    Active Open Descriptors: {len(fds)} ({', '.join(fds[:8])}{'...' if len(fds) > 8 else ''})")
    else:
        print("    (/proc/self/fd not present in current environment)")

    print("\n[✓] Linux Kernel Crash Course completed successfully!\n")


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "redis>=5.0.0",
# ]
# ///
"""
Experiment 03: Redis Persistence Durability & Crash Recovery (SIGKILL / kill -9)

Tests data durability across persistence configurations:
1. RDB snapshots only: High throughput, but catastrophic loss of un-snapshotted writes.
2. AOF (appendfsync everysec): Near-zero loss (~1s window), high throughput.
3. AOF (appendfsync always): Zero loss, synchronous fsync on every write.

Execution:
Fires rapid writes, issues 'docker compose kill -s SIGKILL' mid-stream,
restarts the container, and verifies how many acknowledged writes survived.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

import redis

COMPOSE_DIR = Path(__file__).resolve().parent.parent


console = Console()


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=COMPOSE_DIR, capture_output=True, text=True, check=True)


def get_active_profile(client: redis.Redis) -> tuple[str, str]:
    info = client.info("persistence")
    aof_enabled = bool(info.get("aof_enabled", 0))
    if not aof_enabled:
        return "RDB Snapshots Only", "persistence-rdb"

    # Check aof_fsync via config get
    try:
        conf = client.config_get("appendfsync")
        fsync_mode = conf.get("appendfsync", "unknown")
    except Exception:
        fsync_mode = "everysec"

    if fsync_mode == "always":
        return "AOF (appendfsync always)", "persistence-always"
    return "AOF (appendfsync everysec)", "persistence-aof"


def main() -> None:
    client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

    try:
        client.ping()
    except redis.ConnectionError:
        console.print("[bold red]Error: Cannot connect to Redis on 127.0.0.1:6379.[/]")
        print("Please start a persistence profile first:")
        print("  make up EXP=persistence-rdb     # Test RDB crash data loss")
        print("  make up EXP=persistence-aof     # Test AOF everysec durability")
        print("  make up EXP=persistence-always  # Test AOF always zero-loss")
        sys.exit(1)

    label, profile_name = get_active_profile(client)

    console.print("[bold blue]" + "=" * 65 + "[/]")
    console.print("[bold green]Experiment 03: Redis Persistence & Crash Recovery[/]")
    console.print(f"  Active Mode    : [bold yellow]{label}[/]")
    print(f"  Profile Name   : {profile_name}")
    console.print("[bold blue]" + "=" * 65 + "[/]\n")

    # Clean existing data in-memory
    print("Flushing database for a clean test run...")
    client.flushall()

    # Phase 1: High-speed writes with pipeline
    TOTAL_WRITES = 5000
    print(f"[*] Phase 1: Writing up to {TOTAL_WRITES:,} keys sequentially...")

    pipe = client.pipeline(transaction=False)
    acknowledged = 0
    t0 = time.perf_counter()

    # Write in small batches so writes are confirmed over the wire
    BATCH_SIZE = 50
    target_kill_at = 3500

    for i in range(1, TOTAL_WRITES + 1):
        pipe.set(f"seq:{i:06d}", f"val_{i}_{time.time()}")
        if i % BATCH_SIZE == 0:
            pipe.execute()
            acknowledged = i

        if acknowledged >= target_kill_at:
            break

    write_elapsed = time.perf_counter() - t0
    rate = acknowledged / write_elapsed if write_elapsed > 0 else 0
    print(f"    Acknowledged {acknowledged:,} writes ({rate:,.0f} writes/sec).")

    # Phase 2: Ungraceful crash (SIGKILL / kill -9)
    console.print("\n[bold red][*] Phase 2: Killing container with SIGKILL (simulating ungraceful power loss)...[/]")
    run_cmd(["docker", "compose", "--profile", profile_name, "kill", "-s", "SIGKILL"])
    client.close()

    print("    Container terminated abruptly without shutdown flush.")
    time.sleep(1.0)

    # Phase 3: Restart container preserving volume
    print(f"\n[*] Phase 3: Restarting container via '{profile_name}' (mounting persisted volume)...")
    run_cmd(["docker", "compose", "--profile", profile_name, "up", "-d", "--wait"])

    # Reconnect
    client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    for _ in range(10):
        try:
            if client.ping():
                break
        except redis.ConnectionError:
            time.sleep(0.5)

    # Phase 4: Audit recovered data
    print("\n[*] Phase 4: Auditing recovered keys from disk...")
    survived = 0
    highest_survived = 0

    # Scan sequentially to check data continuity
    for i in range(1, acknowledged + 1):
        if client.exists(f"seq:{i:06d}"):
            survived += 1
            highest_survived = i

    lost_writes = acknowledged - survived
    loss_pct = (lost_writes / acknowledged * 100) if acknowledged > 0 else 0

    # Summary Report
    console.print("\n[bold green]" + "=" * 65)
    print("EXPERIMENT 03 SUMMARY REPORT")
    console.print("=" * 65 + "[/]")
    console.print(f"Persistence Configuration   : [bold yellow]{label}[/]")
    print(f"Writes Acknowledged by Redis : {acknowledged:,}")
    console.print(f"Writes Recovered After Crash : [bold green]{survived:,}[/]")
    print(f"Highest Key Recovered        : seq:{highest_survived:06d}")
    console.print(f"Total Lost Writes            : [bold red]{lost_writes:,}[/] ({loss_pct:.1f}%)")
    print("=" * 65)

    if lost_writes > 0:
        console.print(f"\n[bold red][!] DATA LOSS OBSERVED:[/] {lost_writes:,} confirmed writes disappeared on crash.")
        if "RDB" in label:
            print("Why? RDB only snapshots periodically (e.g. every 60s). All in-memory writes")
            print("since the last snapshot were completely lost when the process was killed.")
        elif "everysec" in label:
            print("Why? AOF 'everysec' buffers writes in an OS buffer and fsyncs once per second.")
            print("Writes from the final incomplete 1-second fsync window were lost.")
    else:
        console.print("\n[bold green][✓] ZERO DATA LOSS:[/] 100% of acknowledged writes were successfully recovered from disk!")
        print("Every single command was synchronously fsync'd before returning to the client.")

    client.close()


if __name__ == "__main__":
    main()

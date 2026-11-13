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
from rich.markup import escape

import redis

COMPOSE_DIR = Path(__file__).resolve().parent.parent


console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


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
        plain_console.print("Please start a persistence profile first:")
        plain_console.print("  make up EXP=persistence-rdb     # Test RDB crash data loss")
        plain_console.print("  make up EXP=persistence-aof     # Test AOF everysec durability")
        plain_console.print("  make up EXP=persistence-always  # Test AOF always zero-loss")
        sys.exit(1)

    label, profile_name = get_active_profile(client)
    verify_phase(
        profile_name in ("persistence-rdb", "persistence-aof", "persistence-always"),
        "Phase 0: Preconditions",
        f"Unexpected persistence profile '{profile_name}'",
    )

    console.print("=" * 65, style="bold blue")
    console.print("[bold green]Experiment 03: Redis Persistence & Crash Recovery[/]")
    console.print(f"  Active Mode    : [bold yellow]{label}[/]")
    plain_console.print(f"  Profile Name   : {profile_name}")
    console.print("=" * 65 + "\n", style="bold blue")

    # Clean existing data in-memory
    plain_console.print("Flushing database for a clean test run...")
    client.flushall()
    verify_phase(client.dbsize() == 0, "Phase 0: Reset", f"Expected dbsize=0 after FLUSHALL, got {client.dbsize()}")

    # Phase 1: High-speed writes with pipeline
    TOTAL_WRITES = 5000
    plain_console.print(f"[*] Phase 1: Writing up to {TOTAL_WRITES:,} keys sequentially...")

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
    plain_console.print(f"    Acknowledged {acknowledged:,} writes ({rate:,.0f} writes/sec).")
    verify_phase(
        acknowledged == target_kill_at and client.dbsize() == acknowledged,
        "Phase 1: Pre-Crash Writes",
        f"Expected {target_kill_at} acknowledged keys in DB, got acknowledged={acknowledged}, dbsize={client.dbsize()}",
    )

    # Phase 2: Ungraceful crash (SIGKILL / kill -9)
    console.print("\n[bold red][*] Phase 2: Killing container with SIGKILL (simulating ungraceful power loss)...[/]")
    run_cmd(["docker", "compose", "--profile", profile_name, "kill", "-s", "SIGKILL"])
    client.close()

    plain_console.print("    Container terminated abruptly without shutdown flush.")
    time.sleep(1.0)

    # Phase 3: Restart container preserving volume
    plain_console.print(f"\n[*] Phase 3: Restarting container via '{profile_name}' (mounting persisted volume)...")
    run_cmd(["docker", "compose", "--profile", profile_name, "up", "-d", "--wait"])

    # Reconnect
    client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    reconnected = False
    for _ in range(10):
        try:
            if client.ping():
                reconnected = True
                break
        except redis.ConnectionError:
            time.sleep(0.5)
    verify_phase(reconnected, "Phase 3: Restart & Reconnect", "Failed to reconnect to Redis after SIGKILL restart")

    # Phase 4: Audit recovered data
    plain_console.print("\n[*] Phase 4: Auditing recovered keys from disk...")
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
    console.print("\n" + "=" * 65, style="bold green")
    plain_console.print("EXPERIMENT 03 SUMMARY REPORT")
    console.print("=" * 65, style="bold green")
    console.print(f"Persistence Configuration   : [bold yellow]{label}[/]")
    plain_console.print(f"Writes Acknowledged by Redis : {acknowledged:,}")
    console.print(f"Writes Recovered After Crash : [bold green]{survived:,}[/]")
    plain_console.print(f"Highest Key Recovered        : seq:{highest_survived:06d}")
    console.print(f"Total Lost Writes            : [bold red]{lost_writes:,}[/] ({loss_pct:.1f}%)")
    plain_console.print("=" * 65)

    if profile_name == "persistence-always":
        verify_phase(
            lost_writes == 0 and survived == acknowledged,
            "Phase 4: Durability (appendfsync always)",
            f"Expected 0 lost writes under appendfsync=always, lost {lost_writes}/{acknowledged}",
        )
    elif profile_name == "persistence-rdb":
        verify_phase(
            lost_writes > 0,
            "Phase 4: Durability (RDB only)",
            f"Expected un-snapshotted RDB data loss after SIGKILL, but 0/{acknowledged} writes were lost",
        )
    elif profile_name == "persistence-aof":
        verify_phase(
            0 < survived <= acknowledged,
            "Phase 4: Durability (AOF everysec)",
            f"Expected partial/full AOF survival (survived > 0), got survived={survived}/{acknowledged}",
        )

    if lost_writes > 0:
        console.print(f"\n[bold red][!] DATA LOSS OBSERVED:[/] {lost_writes:,} confirmed writes disappeared on crash.")
        if "RDB" in label:
            plain_console.print("Why? RDB only snapshots periodically (e.g. every 60s). All in-memory writes")
            plain_console.print("since the last snapshot were completely lost when the process was killed.")
        elif "everysec" in label:
            plain_console.print("Why? AOF 'everysec' buffers writes in an OS buffer and fsyncs once per second.")
            plain_console.print("Writes from the final incomplete 1-second fsync window were lost.")
    else:
        console.print("\n[bold green][✓] ZERO DATA LOSS:[/] 100% of acknowledged writes were successfully recovered from disk!")
        plain_console.print("Every single command was synchronously fsync'd before returning to the client.")

    client.close()


if __name__ == "__main__":
    main()

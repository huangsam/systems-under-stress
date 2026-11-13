# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "redis>=5.0.0",
# ]
# ///
"""
Experiment 01: Memory Limits and Eviction Policies under Load

Demonstrates:
1. What happens when Redis memory exceeds 'maxmemory' (16MB).
2. Behavior under 'noeviction' (OOM error) vs 'allkeys-lru' (seamless eviction).
3. Hit ratios under skewed (Zipfian-like 80/20) access patterns.
"""

from __future__ import annotations

import random
import sys
import time

from redis.exceptions import OutOfMemoryError, ResponseError
from rich.console import Console
from rich.markup import escape

import redis

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def format_bytes(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


def main() -> None:
    client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

    try:
        client.ping()
    except redis.ConnectionError:
        console.print("[bold red]Error: Cannot connect to Redis on 127.0.0.1:6379.[/]")
        plain_console.print("Please start an experiment container first:")
        plain_console.print("  make up EXP=eviction-lru   # for LRU eviction")
        plain_console.print("  make up EXP=eviction-noeviction # for OOM failure testing")
        sys.exit(1)

    # 1. Inspect current server settings (Phase 0 Preconditions)
    info_mem = client.info("memory")
    max_memory = int(info_mem.get("maxmemory", 0))
    policy = info_mem.get("maxmemory_policy", "unknown")

    console.print("=" * 65, style="bold blue")
    console.print("[bold green]Redis Eviction Lab: Current Configuration[/]")
    plain_console.print(f"  Max Memory Limit : {format_bytes(max_memory)} ({max_memory:,} bytes)")
    console.print(f"  Eviction Policy  : [bold yellow]{policy}[/]")
    console.print("=" * 65 + "\n", style="bold blue")

    expected_max_memory = 16 * 1024 * 1024
    verify_phase(
        max_memory == expected_max_memory,
        "Phase 0: Preconditions",
        f"Expected maxmemory={expected_max_memory} (16MB), got {max_memory}. Start with EXP=eviction-lru, eviction-lfu, or eviction-noeviction.",
    )
    verify_phase(
        policy in ("allkeys-lru", "allkeys-lfu", "noeviction"),
        "Phase 0: Preconditions",
        f"Unsupported maxmemory-policy '{policy}'; expected allkeys-lru, allkeys-lfu, or noeviction.",
    )

    # 2. Reset database for clean run
    plain_console.print("Flushing keyspace for a clean test run...")
    client.flushall()
    verify_phase(client.dbsize() == 0, "Phase 0: Reset", f"Expected dbsize=0 after FLUSHALL, got {client.dbsize()}")

    # 3. Phase 1: Flood Redis with 10KB payloads until exceeding 16MB
    PAYLOAD_SIZE = 10 * 1024  # 10 KB
    dummy_payload = "X" * PAYLOAD_SIZE
    TOTAL_ITEMS = 2500  # 2500 * 10KB = ~25MB of raw data (exceeds 16MB maxmemory)

    plain_console.print(f"[*] Phase 1: Inserting {TOTAL_ITEMS:,} items (10KB each, ~25MB total data)...")

    written = 0
    oom_errors = 0
    start_time = time.time()

    for i in range(TOTAL_ITEMS):
        key = f"item:{i:05d}"
        try:
            client.set(key, dummy_payload)
            written += 1
        except (ResponseError, OutOfMemoryError) as err:
            err_str = str(err).lower()
            if "oom" in err_str or "maxmemory" in err_str or isinstance(err, OutOfMemoryError):
                oom_errors += 1
                if oom_errors == 1:
                    console.print(f"\n[bold red][!] FIRST OOM HIT at item {i}![/]")
                    plain_console.print(f"    Redis Error Message: {err}")
            else:
                raise err

        # Redis LRU clock has 1s granularity; sleep past a 1s boundary at mid-ingestion
        # checkpoints and refresh the hot set (item:00000..00099) so cold keys age out.
        if i in (999, 1749):
            time.sleep(1.1)
        if i in (99, 999, 1749, TOTAL_ITEMS - 1):
            pipe = client.pipeline(transaction=False)
            repeats = 3 if i == 99 else 1
            for _ in range(repeats):
                for h in range(100):
                    pipe.get(f"item:{h:05d}")
            pipe.execute()

        if (i + 1) % 250 == 0 or i == TOTAL_ITEMS - 1:
            curr_mem = client.info("memory").get("used_memory", 0)
            stats = client.info("stats")
            evicted = stats.get("evicted_keys", 0)
            plain_console.print(
                f"    Progress: {i + 1:>5}/{TOTAL_ITEMS} | Used Mem: {format_bytes(curr_mem):>8} | Evicted: {evicted:>5} | OOM Errors: {oom_errors:>4}"
            )

    duration = time.time() - start_time
    plain_console.print(f"\nPhase 1 Completed in {duration:.2f}s:")
    plain_console.print(f"  - Successfully written : {written:,}")
    plain_console.print(f"  - OOM rejections       : {oom_errors:,}")

    # 4. Phase 2: Skewed Read Test (Zipfian 80/20 simulation)
    # Hot keys: item:00000 to item:00099 (accessed 80% of the time)
    # Cold keys: item:00100 to item:02499 (accessed 20% of the time)
    plain_console.print("\n[*] Phase 2: Running 2,000 skewed reads (80% hot keys, 20% cold keys)...")

    # Access hot keys a few times first to establish frequency / recency
    for h in range(100):
        client.get(f"item:{h:05d}")

    hot_hits, hot_total = 0, 0
    cold_hits, cold_total = 0, 0

    for _ in range(2000):
        if random.random() < 0.80:
            # Hot key access
            k = f"item:{random.randint(0, 99):05d}"
            hot_total += 1
            if client.get(k) is not None:
                hot_hits += 1
        else:
            # Cold key access
            k = f"item:{random.randint(100, TOTAL_ITEMS - 1):05d}"
            cold_total += 1
            if client.get(k) is not None:
                cold_hits += 1

    hot_ratio = (hot_hits / hot_total * 100) if hot_total > 0 else 0
    cold_ratio = (cold_hits / cold_total * 100) if cold_total > 0 else 0

    # 5. Final Report
    final_mem = client.info("memory")
    final_stats = client.info("stats")

    console.print("\n" + "=" * 65, style="bold green")
    plain_console.print("EXPERIMENT 01 SUMMARY REPORT")
    console.print("=" * 65, style="bold green")
    console.print(f"Policy Under Test     : [bold yellow]{policy}[/]")
    plain_console.print(f"Peak Memory Reached   : {format_bytes(final_mem.get('used_memory_peak', 0))}")
    plain_console.print(f"Current Memory Used   : {format_bytes(final_mem.get('used_memory', 0))}")
    console.print(f"Total Keys Evicted    : [bold red]{final_stats.get('evicted_keys', 0):,}[/]")
    console.print(f"Hot Key Hit Ratio     : [bold green]{hot_ratio:.1f}%[/] ({hot_hits}/{hot_total})")
    plain_console.print(f"Cold Key Hit Ratio    : {cold_ratio:.1f}% ({cold_hits}/{cold_total})")
    plain_console.print("=" * 65)

    if policy == "noeviction":
        verify_phase(oom_errors > 0, "Phase 1: Saturation (noeviction)", f"Expected OOM errors > 0, got {oom_errors}")
        verify_phase(written > 0, "Phase 1: Saturation (noeviction)", "Expected some keys written before OOM")
        verify_phase(
            final_stats.get("evicted_keys", 0) == 0,
            "Phase 1: Saturation (noeviction)",
            f"Expected 0 evictions under noeviction, got {final_stats.get('evicted_keys', 0)}",
        )
        verify_phase(hot_ratio == 100.0, "Phase 2: Skewed Reads (noeviction)", f"Expected 100% hot key survival, got {hot_ratio:.1f}%")
        console.print("\n[bold yellow][Takeaway - noeviction][/]")
        plain_console.print("Redis refused all writes once maxmemory was reached with OOM errors.")
        plain_console.print("Read requests continue to succeed, but write-heavy services fail hard without application fallback.")
    elif "lru" in policy or "lfu" in policy:
        verify_phase(oom_errors == 0, f"Phase 1: Saturation ({policy})", f"Expected 0 OOM errors, got {oom_errors}")
        verify_phase(written == TOTAL_ITEMS, f"Phase 1: Saturation ({policy})", f"Expected {TOTAL_ITEMS} writes, got {written}")
        verify_phase(
            final_stats.get("evicted_keys", 0) > 0,
            f"Phase 1: Saturation ({policy})",
            f"Expected evicted_keys > 0, got {final_stats.get('evicted_keys', 0)}",
        )
        verify_phase(
            final_mem.get("used_memory", 0) <= max_memory,
            f"Phase 1: Memory Cap ({policy})",
            f"used_memory ({final_mem.get('used_memory', 0)}) exceeded maxmemory ({max_memory})",
        )
        verify_phase(
            hot_ratio > cold_ratio and hot_ratio >= 80.0,
            f"Phase 2: Skewed Reads ({policy})",
            f"Expected hot_ratio > cold_ratio and >= 80%, got hot={hot_ratio:.1f}%, cold={cold_ratio:.1f}%",
        )
        console.print(f"\n[bold yellow][Takeaway - {policy}][/]")
        plain_console.print("Redis safely capped memory by evicting older/idle keys to make room for new writes.")
        plain_console.print(f"Hot keys maintained a {hot_ratio:.1f}% hit rate while cold keys were recycled automatically.")


if __name__ == "__main__":
    main()

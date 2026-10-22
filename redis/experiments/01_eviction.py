# /// script
# requires-python = ">=3.12"
# dependencies = [
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
import redis
from redis.exceptions import ResponseError, OutOfMemoryError


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
        print("\033[1;31mError: Cannot connect to Redis on 127.0.0.1:6379.\033[0m")
        print("Please start an experiment container first:")
        print("  make up EXP=eviction-lru   # for LRU eviction")
        print("  make up EXP=eviction-noeviction # for OOM failure testing")
        sys.exit(1)

    # 1. Inspect current server settings
    info_mem = client.info("memory")
    max_memory = int(info_mem.get("maxmemory", 0))
    policy = info_mem.get("maxmemory_policy", "unknown")

    print("\033[1;34m" + "=" * 65 + "\033[0m")
    print(f"\033[1;32mRedis Eviction Lab: Current Configuration\033[0m")
    print(f"  Max Memory Limit : {format_bytes(max_memory)} ({max_memory:,} bytes)")
    print(f"  Eviction Policy  : \033[1;33m{policy}\033[0m")
    print("\033[1;34m" + "=" * 65 + "\033[0m\n")

    # 2. Reset database for clean run
    print("Flushing keyspace for a clean test run...")
    client.flushall()

    # 3. Phase 1: Flood Redis with 10KB payloads until exceeding 16MB
    PAYLOAD_SIZE = 10 * 1024  # 10 KB
    dummy_payload = "X" * PAYLOAD_SIZE
    TOTAL_ITEMS = 2500  # 2500 * 10KB = ~25MB of raw data (exceeds 16MB maxmemory)

    print(f"[*] Phase 1: Inserting {TOTAL_ITEMS:,} items (10KB each, ~25MB total data)...")

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
                    print(f"\n\033[1;31m[!] FIRST OOM HIT at item {i}!\033[0m")
                    print(f"    Redis Error Message: {err}")
            else:
                raise err

        if (i + 1) % 250 == 0 or i == TOTAL_ITEMS - 1:
            curr_mem = client.info("memory").get("used_memory", 0)
            stats = client.info("stats")
            evicted = stats.get("evicted_keys", 0)
            print(
                f"    Progress: {i+1:>5}/{TOTAL_ITEMS} | "
                f"Used Mem: {format_bytes(curr_mem):>8} | "
                f"Evicted: {evicted:>5} | "
                f"OOM Errors: {oom_errors:>4}"
            )

    duration = time.time() - start_time
    print(f"\nPhase 1 Completed in {duration:.2f}s:")
    print(f"  - Successfully written : {written:,}")
    print(f"  - OOM rejections       : {oom_errors:,}")

    # 4. Phase 2: Skewed Read Test (Zipfian 80/20 simulation)
    # Hot keys: item:00000 to item:00099 (accessed 80% of the time)
    # Cold keys: item:00100 to item:02499 (accessed 20% of the time)
    print("\n[*] Phase 2: Running 2,000 skewed reads (80% hot keys, 20% cold keys)...")

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

    print("\n" + "\033[1;32m" + "=" * 65)
    print("EXPERIMENT 01 SUMMARY REPORT")
    print("=" * 65 + "\033[0m")
    print(f"Policy Under Test     : \033[1;33m{policy}\033[0m")
    print(f"Peak Memory Reached   : {format_bytes(final_mem.get('used_memory_peak', 0))}")
    print(f"Current Memory Used   : {format_bytes(final_mem.get('used_memory', 0))}")
    print(f"Total Keys Evicted    : \033[1;31m{final_stats.get('evicted_keys', 0):,}\033[0m")
    print(f"Hot Key Hit Ratio     : \033[1;32m{hot_ratio:.1f}%\033[0m ({hot_hits}/{hot_total})")
    print(f"Cold Key Hit Ratio    : {cold_ratio:.1f}% ({cold_hits}/{cold_total})")
    print("=" * 65)

    if policy == "noeviction":
        print("\n\033[1;33m[Takeaway - noeviction]\033[0m")
        print("Redis refused all writes once maxmemory was reached with OOM errors.")
        print("Read requests continue to succeed, but write-heavy services fail hard without application fallback.")
    elif "lru" in policy:
        print("\n\033[1;33m[Takeaway - allkeys-lru]\033[0m")
        print("Redis safely capped memory by evicting older/idle keys to make room for new writes.")
        print(f"Hot keys maintained a {hot_ratio:.1f}% hit rate while cold keys were recycled automatically.")


if __name__ == "__main__":
    main()

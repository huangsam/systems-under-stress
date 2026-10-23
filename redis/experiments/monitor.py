# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "redis>=5.0.0",
# ]
# ///
"""Live Redis memory and eviction monitor."""

from __future__ import annotations

import sys
import time

import redis


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
        print("Error: Could not connect to Redis on 127.0.0.1:6379.", file=sys.stderr)
        print("Run 'make up EXP=eviction-lru' first.", file=sys.stderr)
        sys.exit(1)

    print("\033[1;36m=== Live Redis Stats Monitor (Ctrl+C to stop) ===\033[0m")
    header = f"{'Used Mem':>10} | {'Max Mem':>10} | {'Usage %':>8} | {'Evicted Keys':>14} | {'Total Keys':>12} | {'Hit Ratio':>10}"
    print(header)
    print("-" * len(header))

    last_evicted = 0
    while True:
        try:
            mem = client.info("memory")
            stats = client.info("stats")
            keyspace = client.info("keyspace")

            used = mem.get("used_memory", 0)
            max_mem = mem.get("maxmemory", 0)
            _policy = mem.get("maxmemory_policy", "none")
            evicted = stats.get("evicted_keys", 0)
            hits = stats.get("keyspace_hits", 0)
            misses = stats.get("keyspace_misses", 0)
            total_requests = hits + misses
            hit_ratio = (hits / total_requests * 100) if total_requests > 0 else 0.0

            db0 = keyspace.get("db0", {})
            total_keys = db0.get("keys", 0) if isinstance(db0, dict) else 0

            usage_pct = (used / max_mem * 100) if max_mem > 0 else 0.0
            evict_diff = evicted - last_evicted
            last_evicted = evicted

            evict_str = f"{evicted:,} (+{evict_diff})" if evict_diff > 0 else f"{evicted:,}"

            print(
                f"{format_bytes(used):>10} | {format_bytes(max_mem):>10} | {usage_pct:>7.1f}% | {evict_str:>14} | {total_keys:>12,} | {hit_ratio:>9.1f}%",
                end="\r",
                flush=True,
            )
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"\nError fetching stats: {e}")
            time.sleep(1.0)


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "redis>=5.0.0",
#     "rich>=13.7.0",
# ]
# ///
"""
Experiment 04: Redis Single-Threaded Event Loop Blocking

Demonstrates:
1. Redis executes commands sequentially on a single main event-loop thread.
2. Blocking O(N) commands (KEYS *) and synchronous big-key deletion (DEL)
   stall unrelated concurrent PING/GET requests for tens or hundreds of ms.
3. Non-blocking alternatives (incremental SCAN and asynchronous UNLINK) keep
   tail PING latency sub-millisecond / low-ms under the same workload.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass

from rich.console import Console
from rich.markup import escape
from rich.table import Table

import redis

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
NUM_KEYS = 40_000
BIG_HASH_FIELDS = 250_000


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


@dataclass
class ProbeStats:
    operation: str
    op_elapsed_ms: float
    ping_count: int
    avg_ping_ms: float
    max_ping_ms: float


def measure_with_ping_probe(label: str, fn) -> ProbeStats:
    """Runs `fn()` while a background thread measures Redis PING latency every 5ms."""
    stop = threading.Event()
    pings_ms: list[float] = []

    def probe_loop() -> None:
        probe = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5.0)
        while not stop.is_set():
            t0 = time.perf_counter()
            probe.ping()
            pings_ms.append((time.perf_counter() - t0) * 1000)
            time.sleep(0.005)

    t = threading.Thread(target=probe_loop, daemon=True)
    t.start()
    time.sleep(0.02)
    op_t0 = time.perf_counter()
    fn()
    op_elapsed_ms = (time.perf_counter() - op_t0) * 1000
    time.sleep(0.02)
    stop.set()
    t.join(timeout=2.0)

    return ProbeStats(
        operation=label,
        op_elapsed_ms=op_elapsed_ms,
        ping_count=len(pings_ms),
        avg_ping_ms=sum(pings_ms) / max(1, len(pings_ms)),
        max_ping_ms=max(pings_ms, default=0.0),
    )


def seed_small_keys(r: redis.Redis) -> None:
    pipe = r.pipeline(transaction=False)
    for i in range(NUM_KEYS):
        pipe.set(f"item:{i:06d}", "v")
        if (i + 1) % 5000 == 0:
            pipe.execute()
    pipe.execute()


def seed_big_hash(r: redis.Redis, key: str) -> None:
    pipe = r.pipeline(transaction=False)
    batch: dict[str, str] = {}
    for i in range(BIG_HASH_FIELDS):
        batch[f"f:{i}"] = "x" * 16
        if len(batch) == 5000:
            pipe.hset(key, mapping=batch)
            pipe.execute()
            batch.clear()
    if batch:
        pipe.hset(key, mapping=batch)
        pipe.execute()


def main() -> None:
    console.print("\n" + "=" * 72, style="bold cyan")
    console.print("  EXPERIMENT 04: REDIS SINGLE-THREADED EVENT LOOP BLOCKING", style="bold cyan")
    console.print("=" * 72, style="bold cyan")

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_timeout=10.0)
    try:
        r.ping()
    except redis.ConnectionError as exc:
        console.print(f"[bold red][!] Could not connect to Redis: {escape(str(exc))}[/]")
        plain_console.print("Start the lab container first: cd redis && make up EXP=stampede")
        sys.exit(1)

    r.flushdb()
    console.print(f"[dim]Seeding {NUM_KEYS:,} string keys for KEYS * vs SCAN...[/]")
    seed_small_keys(r)

    keys_stats = measure_with_ping_probe("KEYS item:* (blocking)", lambda: r.keys("item:*"))

    def run_scan() -> None:
        cursor = 0
        while True:
            cursor, _ = r.scan(cursor=cursor, match="item:*", count=500)
            if cursor == 0:
                break

    scan_stats = measure_with_ping_probe("SCAN count=500 (incremental)", run_scan)

    r.flushdb()
    console.print(f"[dim]Seeding two {BIG_HASH_FIELDS:,}-field hashes for DEL vs UNLINK...[/]")
    seed_big_hash(r, "big:hash:del")
    seed_big_hash(r, "big:hash:unlink")

    del_stats = measure_with_ping_probe("DEL big:hash:del (sync free)", lambda: r.delete("big:hash:del"))
    unlink_stats = measure_with_ping_probe("UNLINK big:hash:unlink (async)", lambda: r.unlink("big:hash:unlink"))

    r.flushdb()

    table = Table(title="\nEvent Loop Stall Comparison (Concurrent PING Probe)")
    table.add_column("Operation", style="cyan")
    table.add_column("Op Duration", justify="right")
    table.add_column("PING Samples", justify="right")
    table.add_column("Avg PING", justify="right")
    table.add_column("Max PING (Stall)", justify="right", style="bold")

    for st in (keys_stats, scan_stats, del_stats, unlink_stats):
        table.add_row(
            st.operation,
            f"{st.op_elapsed_ms:.1f} ms",
            str(st.ping_count),
            f"{st.avg_ping_ms:.2f} ms",
            f"{st.max_ping_ms:.1f} ms",
        )
    console.print(table)

    verify_phase(
        keys_stats.max_ping_ms > scan_stats.max_ping_ms,
        "Phase 1: KEYS * vs SCAN",
        f"Expected KEYS * max PING ({keys_stats.max_ping_ms:.1f}ms) > SCAN ({scan_stats.max_ping_ms:.1f}ms)",
    )
    verify_phase(
        del_stats.max_ping_ms > unlink_stats.max_ping_ms,
        "Phase 2: DEL vs UNLINK",
        f"Expected DEL max PING ({del_stats.max_ping_ms:.1f}ms) > UNLINK ({unlink_stats.max_ping_ms:.1f}ms)",
    )
    console.print("\n[bold green]✔ Experiment 04 completed and verified.[/]")


if __name__ == "__main__":
    main()

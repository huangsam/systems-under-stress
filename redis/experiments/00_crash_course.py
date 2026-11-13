# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "redis>=5.0",
# ]
# ///
"""
Redis Crash Course: In-Memory Data Structures, Pipelining & Diagnostics

Demonstrates:
1. Ping / Pong connectivity & Redis version check.
2. String operations with TTL expiration (SET, GET, EXPIRE, TTL).
3. Hash structures (HSET, HGETALL, HINCRBY).
4. Lists & Sets (LPUSH, LRANGE, SADD, SMEMBERS).
5. Atomic operations & Pipelining (batch round-trip reduction).
6. Diagnostic inspection (INFO memory, INFO stats, used_memory_human).
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.markup import escape

import redis

HOST = "127.0.0.1"
PORT = 6379


console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def main() -> None:
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("[bold green]Redis Crash Course: Data Structures, Pipelining & Diagnostics[/]")
    console.print("=" * 70 + "\n", style="bold cyan")

    r = redis.Redis(host=HOST, port=PORT, decode_responses=True)

    try:
        pong = r.ping()
        verify_phase(pong is True, "Phase 1: Connectivity", f"Expected PING=True, got {pong!r}")
        console.print(f"[bold blue][*] 1. Server Connectivity & Info (PING: {pong})[/]")
    except Exception as e:
        console.print(f"[bold red][!] Connection failed: {escape(str(e))}[/]")
        plain_console.print("Start the lab container first: cd redis && make up EXP=eviction-lru")
        sys.exit(1)

    info = r.info("server")
    verify_phase(bool(info.get("redis_version")), "Phase 1: Connectivity", "Missing redis_version in INFO server")
    plain_console.print(f"    Redis Version: {info.get('redis_version')}, Mode: {info.get('redis_mode')}")

    # 2. Strings & TTL
    console.print("\n[bold blue][*] 2. Strings & Key Expiration (TTL)[/]")
    r.set("cc:user:101:name", "Alice")
    r.set("cc:user:101:session", "token-xyz789", ex=10)
    name_val = r.get("cc:user:101:name")
    sess_val = r.get("cc:user:101:session")
    ttl = r.ttl("cc:user:101:session")
    verify_phase(name_val == "Alice", "Phase 2: Strings", f"Expected 'Alice', got {name_val!r}")
    verify_phase(sess_val == "token-xyz789", "Phase 2: Strings", f"Expected 'token-xyz789', got {sess_val!r}")
    verify_phase(0 < ttl <= 10, "Phase 2: TTL", f"Expected 0 < TTL <= 10, got {ttl}")
    plain_console.print(f"    SET cc:user:101:name -> '{name_val}'")
    plain_console.print(f"    SETEX cc:user:101:session -> '{sess_val}' (TTL remaining: {ttl}s)")

    # 3. Hashes
    console.print("\n[bold blue][*] 3. Hash Structures (HSET, HGETALL, HINCRBY)[/]")
    cart_key = "cc:cart:101"
    r.delete(cart_key)
    r.hset(cart_key, mapping={"item:laptop": "1", "item:mouse": "2"})
    r.hincrby(cart_key, "item:mouse", 1)
    cart_contents = r.hgetall(cart_key)
    verify_phase(
        cart_contents == {"item:laptop": "1", "item:mouse": "3"},
        "Phase 3: Hashes",
        f"Unexpected hash contents: {cart_contents}",
    )
    plain_console.print(f"    Cart contents for {cart_key}:")
    for k, v in cart_contents.items():
        plain_console.print(f"      - {k}: {v}")

    # 4. Sets and Lists
    console.print("\n[bold blue][*] 4. Lists & Sets[/]")
    queue_key = "cc:jobs:queue"
    r.delete(queue_key)
    r.rpush(queue_key, "job_alpha", "job_beta", "job_gamma")
    job_count = r.llen(queue_key)
    jobs = r.lrange(queue_key, 0, -1)
    verify_phase(
        job_count == 3 and jobs == ["job_alpha", "job_beta", "job_gamma"],
        "Phase 4: Lists",
        f"Unexpected list state: len={job_count}, items={jobs}",
    )
    plain_console.print(f"    List {queue_key} ({job_count} items): {jobs}")

    tags_key = "cc:article:42:tags"
    r.delete(tags_key)
    r.sadd(tags_key, "database", "redis", "in-memory", "redis")  # duplicate ignored
    members = r.smembers(tags_key)
    scard = r.scard(tags_key)
    verify_phase(
        scard == 3 and members == {"database", "redis", "in-memory"},
        "Phase 4: Sets",
        f"Unexpected set state: card={scard}, members={members}",
    )
    plain_console.print(f"    Set {tags_key} (cardinality {scard}): {members}")

    # 5. Pipelining
    console.print("\n[bold blue][*] 5. Atomic Counters & Pipelining[/]")
    pipe = r.pipeline(transaction=True)
    counter_key = "cc:metrics:pageviews"
    r.delete(counter_key)
    for _ in range(5):
        pipe.incr(counter_key)
    results = pipe.execute()
    verify_phase(
        results == [1, 2, 3, 4, 5],
        "Phase 5: Pipelining",
        f"Expected [1, 2, 3, 4, 5], got {results}",
    )
    plain_console.print(f"    Pipelined 5 INCR operations in 1 round-trip: final value = {results[-1]}")

    # 6. Diagnostics
    console.print("\n[bold blue][*] 6. Memory & Engine Diagnostics[/]")
    mem = r.info("memory")
    stats = r.info("stats")
    verify_phase(int(mem.get("used_memory", 0)) > 0, "Phase 6: Diagnostics", "used_memory must be > 0")
    verify_phase(int(stats.get("total_commands_processed", 0)) > 0, "Phase 6: Diagnostics", "total_commands_processed must be > 0")
    plain_console.print(f"    Used Memory: {mem.get('used_memory_human')} (Peak: {mem.get('used_memory_peak_human')})")
    plain_console.print(f"    Total Connections Received: {stats.get('total_connections_received')}")
    plain_console.print(f"    Total Commands Processed: {stats.get('total_commands_processed')}")
    plain_console.print(f"    Instantaneous Ops/sec: {stats.get('instantaneous_ops_per_sec')}")

    # Cleanup
    pattern = "cc:*"
    keys = r.keys(pattern)
    if keys:
        r.delete(*keys)
    remaining = r.keys(pattern)
    verify_phase(len(remaining) == 0, "Phase 6: Cleanup", f"Expected 0 remaining cc:* keys, found {len(remaining)}")
    plain_console.print(f"    Cleaned up {len(keys)} demo keys matching '{pattern}'.")

    console.print("\n[bold green][✓] Redis Crash Course completed successfully![/]\n")


if __name__ == "__main__":
    main()

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

import redis

HOST = "127.0.0.1"
PORT = 6379


console = Console()


def main() -> None:
    console.print("\n[bold cyan]" + "=" * 70 + "[/]")
    console.print("[bold green]Redis Crash Course: Data Structures, Pipelining & Diagnostics[/]")
    console.print("[bold cyan]" + "=" * 70 + "[/]\n")

    r = redis.Redis(host=HOST, port=PORT, decode_responses=True)

    try:
        pong = r.ping()
        console.print(f"[bold blue][*] 1. Server Connectivity & Info (PING: {pong})[/]")
    except Exception as e:
        console.print(f"[bold red][!] Connection failed: {e}[/]")
        print("Start the lab container first: cd redis && make up EXP=eviction-lru")
        sys.exit(1)

    info = r.info("server")
    print(f"    Redis Version: {info.get('redis_version')}, Mode: {info.get('redis_mode')}")

    # 2. Strings & TTL
    console.print("\n[bold blue][*] 2. Strings & Key Expiration (TTL)[/]")
    r.set("cc:user:101:name", "Alice")
    r.set("cc:user:101:session", "token-xyz789", ex=10)
    print(f"    SET cc:user:101:name -> '{r.get('cc:user:101:name')}'")
    ttl = r.ttl("cc:user:101:session")
    print(f"    SETEX cc:user:101:session -> '{r.get('cc:user:101:session')}' (TTL remaining: {ttl}s)")

    # 3. Hashes
    console.print("\n[bold blue][*] 3. Hash Structures (HSET, HGETALL, HINCRBY)[/]")
    cart_key = "cc:cart:101"
    r.hset(cart_key, mapping={"item:laptop": "1", "item:mouse": "2"})
    r.hincrby(cart_key, "item:mouse", 1)
    cart_contents = r.hgetall(cart_key)
    print(f"    Cart contents for {cart_key}:")
    for k, v in cart_contents.items():
        print(f"      - {k}: {v}")

    # 4. Sets and Lists
    console.print("\n[bold blue][*] 4. Lists & Sets[/]")
    queue_key = "cc:jobs:queue"
    r.delete(queue_key)
    r.rpush(queue_key, "job_alpha", "job_beta", "job_gamma")
    job_count = r.llen(queue_key)
    jobs = r.lrange(queue_key, 0, -1)
    print(f"    List {queue_key} ({job_count} items): {jobs}")

    tags_key = "cc:article:42:tags"
    r.delete(tags_key)
    r.sadd(tags_key, "database", "redis", "in-memory", "redis")  # duplicate ignored
    members = r.smembers(tags_key)
    print(f"    Set {tags_key} (cardinality {r.scard(tags_key)}): {members}")

    # 5. Pipelining
    console.print("\n[bold blue][*] 5. Atomic Counters & Pipelining[/]")
    pipe = r.pipeline(transaction=True)
    counter_key = "cc:metrics:pageviews"
    r.delete(counter_key)
    for _ in range(5):
        pipe.incr(counter_key)
    results = pipe.execute()
    print(f"    Pipelined 5 INCR operations in 1 round-trip: final value = {results[-1]}")

    # 6. Diagnostics
    console.print("\n[bold blue][*] 6. Memory & Engine Diagnostics[/]")
    mem = r.info("memory")
    stats = r.info("stats")
    print(f"    Used Memory: {mem.get('used_memory_human')} (Peak: {mem.get('used_memory_peak_human')})")
    print(f"    Total Connections Received: {stats.get('total_connections_received')}")
    print(f"    Total Commands Processed: {stats.get('total_commands_processed')}")
    print(f"    Instantaneous Ops/sec: {stats.get('instantaneous_ops_per_sec')}")

    # Cleanup
    pattern = "cc:*"
    keys = r.keys(pattern)
    if keys:
        r.delete(*keys)
    print(f"    Cleaned up {len(keys)} demo keys matching '{pattern}'.")

    console.print("\n[bold green][✓] Redis Crash Course completed successfully![/]\n")


if __name__ == "__main__":
    main()

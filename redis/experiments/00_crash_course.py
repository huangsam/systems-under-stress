# /// script
# requires-python = ">=3.12"
# dependencies = [
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

import redis

HOST = "127.0.0.1"
PORT = 6379


def main() -> None:
    print("\n\033[1;36m" + "=" * 70 + "\033[0m")
    print("\033[1;32mRedis Crash Course: Data Structures, Pipelining & Diagnostics\033[0m")
    print("\033[1;36m" + "=" * 70 + "\033[0m\n")

    r = redis.Redis(host=HOST, port=PORT, decode_responses=True)

    try:
        pong = r.ping()
        print(f"\033[1;34m[*] 1. Server Connectivity & Info (PING: {pong})\033[0m")
    except Exception as e:
        print(f"\033[1;31m[!] Connection failed: {e}\033[0m")
        print("Start the lab container first: cd redis && make up EXP=eviction-lru")
        sys.exit(1)

    info = r.info("server")
    print(f"    Redis Version: {info.get('redis_version')}, Mode: {info.get('redis_mode')}")

    # 2. Strings & TTL
    print("\n\033[1;34m[*] 2. Strings & Key Expiration (TTL)\033[0m")
    r.set("cc:user:101:name", "Alice")
    r.set("cc:user:101:session", "token-xyz789", ex=10)
    print(f"    SET cc:user:101:name -> '{r.get('cc:user:101:name')}'")
    ttl = r.ttl("cc:user:101:session")
    print(f"    SETEX cc:user:101:session -> '{r.get('cc:user:101:session')}' (TTL remaining: {ttl}s)")

    # 3. Hashes
    print("\n\033[1;34m[*] 3. Hash Structures (HSET, HGETALL, HINCRBY)\033[0m")
    cart_key = "cc:cart:101"
    r.hset(cart_key, mapping={"item:laptop": "1", "item:mouse": "2"})
    r.hincrby(cart_key, "item:mouse", 1)
    cart_contents = r.hgetall(cart_key)
    print(f"    Cart contents for {cart_key}:")
    for k, v in cart_contents.items():
        print(f"      - {k}: {v}")

    # 4. Sets and Lists
    print("\n\033[1;34m[*] 4. Lists & Sets\033[0m")
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
    print("\n\033[1;34m[*] 5. Atomic Counters & Pipelining\033[0m")
    pipe = r.pipeline(transaction=True)
    counter_key = "cc:metrics:pageviews"
    r.delete(counter_key)
    for _ in range(5):
        pipe.incr(counter_key)
    results = pipe.execute()
    print(f"    Pipelined 5 INCR operations in 1 round-trip: final value = {results[-1]}")

    # 6. Diagnostics
    print("\n\033[1;34m[*] 6. Memory & Engine Diagnostics\033[0m")
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

    print("\n\033[1;32m[✓] Redis Crash Course completed successfully!\033[0m\n")


if __name__ == "__main__":
    main()

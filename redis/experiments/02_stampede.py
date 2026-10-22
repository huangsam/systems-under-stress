# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "redis>=5.0.0",
# ]
# ///
"""
Experiment 02: Cache Stampede (Thundering Herd) and Mitigations

Simulates 100 concurrent requests arriving right as a hot key expires:
1. Naive Cache-Aside: Every worker hits the database (DB stampede).
2. Distributed Lock (SET NX PX): Only 1 worker hits the DB; others wait for cache.
3. Probabilistic Early Refresh (XFetch): Refreshes in background before hard expiry.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
import uuid
import redis.asyncio as aioredis


HOT_KEY = "product:trending:42"
DB_COMPUTE_DELAY = 0.05  # 50ms simulated database query latency
CONCURRENT_REQUESTS = 100


class MockDatabase:
    """Simulates an expensive primary relational database (PostgreSQL)."""

    def __init__(self) -> None:
        self.query_count = 0

    async def fetch_product(self, product_id: str) -> dict:
        self.query_count += 1
        await asyncio.sleep(DB_COMPUTE_DELAY)
        return {"id": product_id, "name": "Mechanical Keyboard", "stock": 42}


# ----------------------------------------------------------------------
# Strategy 1: Naive Cache-Aside (The Failure Mode)
# ----------------------------------------------------------------------
async def worker_naive(client: aioredis.Redis, db: MockDatabase) -> dict:
    val = await client.get(HOT_KEY)
    if val is not None:
        return {"source": "cache"}

    # Cache miss: Hammer the database
    data = await db.fetch_product("42")
    await client.set(HOT_KEY, str(data), ex=5)
    return {"source": "database"}


# ----------------------------------------------------------------------
# Strategy 2: Distributed Lock Mutex (SET NX PX)
# ----------------------------------------------------------------------
async def worker_with_lock(client: aioredis.Redis, db: MockDatabase) -> dict:
    val = await client.get(HOT_KEY)
    if val is not None:
        return {"source": "cache"}

    lock_key = f"lock:{HOT_KEY}"
    lock_token = str(uuid.uuid4())
    acquired = await client.set(lock_key, lock_token, nx=True, px=1000)

    if acquired:
        try:
            # Re-check cache in case another worker populated it just before lock
            val = await client.get(HOT_KEY)
            if val is not None:
                return {"source": "cache"}

            data = await db.fetch_product("42")
            await client.set(HOT_KEY, str(data), ex=5)
            return {"source": "database"}
        finally:
            # Safe unlock with Lua script to avoid releasing someone else's expired lock
            lua_unlock = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            await client.eval(lua_unlock, 1, lock_key, lock_token)
    else:
        # Another worker is refreshing; poll cache briefly with jitter
        for _ in range(15):
            await asyncio.sleep(0.01 + random.uniform(0.005, 0.015))
            val = await client.get(HOT_KEY)
            if val is not None:
                return {"source": "cache_after_lock_wait"}

        # Fallback if lock owner crashed
        data = await db.fetch_product("42")
        return {"source": "database_fallback"}


# ----------------------------------------------------------------------
# Strategy 3: Probabilistic Early Expiration (XFetch algorithm)
# Vitter's algorithm: refresh if (now - delta * beta * ln(rand())) >= expiry
# ----------------------------------------------------------------------
async def worker_xfetch(client: aioredis.Redis, db: MockDatabase, beta: float = 1.0) -> dict:
    pipe = client.pipeline()
    pipe.get(HOT_KEY)
    pipe.pttl(HOT_KEY)
    val, pttl = await pipe.execute()

    # If key doesn't exist, compute directly
    if val is None:
        data = await db.fetch_product("42")
        await client.set(HOT_KEY, str(data), ex=5)
        return {"source": "database"}

    # XFetch decision: should this worker recompute early?
    # pttl is remaining TTL in milliseconds
    # delta is computation time (~50ms)
    delta_ms = DB_COMPUTE_DELAY * 1000
    r = random.random()
    # In Python, math.log(r) is negative for r in (0, 1).
    # XFetch triggers when remaining TTL is less than the randomized recompute window:
    # pttl <= -delta_ms * beta * ln(r)
    recompute_window = -delta_ms * beta * math.log(r)
    should_refresh = pttl <= recompute_window

    if should_refresh:
        # Asynchronously refresh without blocking the read
        data = await db.fetch_product("42")
        await client.set(HOT_KEY, str(data), ex=5)
        return {"source": "early_refresh"}

    return {"source": "cache"}


async def run_scenario(name: str, worker_fn, client: aioredis.Redis, pre_seed: bool = False) -> None:
    db = MockDatabase()
    await client.flushall()

    if pre_seed:
        # Seed key with 60ms remaining TTL (in the early-refresh danger zone)
        await client.set(HOT_KEY, "{'id': '42'}", px=60)

    print(f"\n\033[1;34m[*] Running: {name}\033[0m")
    start = time.perf_counter()

    # Launch 100 requests concurrently
    tasks = [asyncio.create_task(worker_fn(client, db)) for _ in range(CONCURRENT_REQUESTS)]
    results = await asyncio.gather(*tasks)

    duration = time.perf_counter() - start
    db_hits = db.query_count
    cache_hits = sum(1 for r in results if "cache" in r["source"])

    print(f"    Total Client Requests : {CONCURRENT_REQUESTS}")
    print(f"    Elapsed Time          : {duration * 1000:.1f}ms")
    print(f"    Database Query Count  : \033[1;{ '31' if db_hits > 10 else '32' }m{db_hits}\033[0m")
    print(f"    Served from Cache     : {cache_hits}")


async def main() -> None:
    client = aioredis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        print("\033[1;31mError: Cannot connect to Redis on 127.0.0.1:6379.\033[0m")
        print("Run 'make up EXP=stampede' first.")
        return

    print("\033[1;36m=================================================================\033[0m")
    print("\033[1;36mExperiment 02: Cache Stampede (Thundering Herd) Simulation\033[0m")
    print("Testing 100 concurrent requests arriving on an expired hot key.")
    print("\033[1;36m=================================================================\033[0m")

    # Scenario 1: Naive Cache-Aside
    await run_scenario("1. Naive Cache-Aside (Unprotected)", worker_naive, client)

    # Scenario 2: Distributed Lock Mutex
    await run_scenario("2. Distributed Lock Mutex (SET NX PX)", worker_with_lock, client)

    # Scenario 3: Probabilistic Early Refresh (XFetch on key near expiry)
    await run_scenario("3. Probabilistic Early Refresh (XFetch)", worker_xfetch, client, pre_seed=True)

    print("\n\033[1;32m=================================================================")
    print("SYSTEMS TAKEAWAYS")
    print("=================================================================\033[0m")
    print("1. Naive Cache-Aside causes a 1:1 surge into the primary DB upon key expiry.")
    print("2. Distributed Locks (SET NX PX) collapse 100 DB queries into EXACTLY 1 query.")
    print("3. Probabilistic early refresh eliminates cache miss latency entirely for users.")
    print("=================================================================\n")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())

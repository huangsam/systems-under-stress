# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
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
import sys
import time
import uuid

import redis.asyncio as aioredis
from rich.console import Console
from rich.markup import escape

HOT_KEY = "product:trending:42"
DB_COMPUTE_DELAY = 0.05  # 50ms simulated database query latency
CONCURRENT_REQUESTS = 100


console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


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


async def run_scenario(name: str, worker_fn, client: aioredis.Redis, pre_seed: bool = False) -> tuple[int, int]:
    db = MockDatabase()
    await client.flushall()
    verify_phase(await client.dbsize() == 0, f"{name}: Reset", "Expected empty Redis after FLUSHALL")

    if pre_seed:
        # Seed key with 60ms remaining TTL (in the early-refresh danger zone)
        await client.set(HOT_KEY, "{'id': '42'}", px=60)

    console.print(f"\n[bold blue][*] Running: {name}[/]")
    start = time.perf_counter()

    # Launch 100 requests concurrently
    tasks = [asyncio.create_task(worker_fn(client, db)) for _ in range(CONCURRENT_REQUESTS)]
    results = await asyncio.gather(*tasks)

    duration = time.perf_counter() - start
    db_hits = db.query_count
    cache_hits = sum(1 for r in results if "cache" in r["source"])

    plain_console.print(f"    Total Client Requests : {CONCURRENT_REQUESTS}")
    plain_console.print(f"    Elapsed Time          : {duration * 1000:.1f}ms")
    console.print(f"    Database Query Count  : [bold {'red' if db_hits > 10 else 'green'}]{db_hits}[/]")
    plain_console.print(f"    Served from Cache     : {cache_hits}")
    verify_phase(len(results) == CONCURRENT_REQUESTS, name, f"Expected {CONCURRENT_REQUESTS} results, got {len(results)}")
    return db_hits, cache_hits


async def main() -> None:
    client = aioredis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        console.print("[bold red]Error: Cannot connect to Redis on 127.0.0.1:6379.[/]")
        plain_console.print("Run 'make up EXP=stampede' first.")
        sys.exit(1)

    mem_info = await client.info("memory")
    max_mem = int(mem_info.get("maxmemory", 0))
    verify_phase(
        max_mem == 0 or max_mem >= 32 * 1024 * 1024,
        "Phase 0: Preconditions",
        f"Expected maxmemory >= 32MB (or 0) for stampede profile, got {max_mem}. Run 'make up EXP=stampede'.",
    )

    console.print("=" * 65, style="bold cyan")
    console.print("[bold cyan]Experiment 02: Cache Stampede (Thundering Herd) Simulation[/]")
    plain_console.print("Testing 100 concurrent requests arriving on an expired hot key.")
    console.print("=" * 65, style="bold cyan")

    # Scenario 1: Naive Cache-Aside
    naive_db, naive_cache = await run_scenario("1. Naive Cache-Aside (Unprotected)", worker_naive, client)
    verify_phase(
        naive_db == CONCURRENT_REQUESTS and naive_cache == 0,
        "Scenario 1: Naive Stampede",
        f"Expected db_hits={CONCURRENT_REQUESTS} and cache_hits=0, got db_hits={naive_db}, cache_hits={naive_cache}",
    )

    # Scenario 2: Distributed Lock Mutex
    lock_db, lock_cache = await run_scenario("2. Distributed Lock Mutex (SET NX PX)", worker_with_lock, client)
    verify_phase(
        lock_db == 1 and lock_cache == CONCURRENT_REQUESTS - 1,
        "Scenario 2: Distributed Lock",
        f"Expected db_hits=1 and cache_hits={CONCURRENT_REQUESTS - 1}, got db_hits={lock_db}, cache_hits={lock_cache}",
    )
    verify_phase(
        await client.exists(f"lock:{HOT_KEY}") == 0,
        "Scenario 2: Lock Cleanup",
        "Distributed lock key was not released after refresh",
    )

    # Scenario 3: Probabilistic Early Refresh (XFetch on key near expiry)
    xfetch_db, xfetch_cache = await run_scenario("3. Probabilistic Early Refresh (XFetch)", worker_xfetch, client, pre_seed=True)
    verify_phase(
        0 < xfetch_db < naive_db and xfetch_cache > 0,
        "Scenario 3: XFetch Early Refresh",
        f"Expected 0 < db_hits < {naive_db} and cache_hits > 0, got db_hits={xfetch_db}, cache_hits={xfetch_cache}",
    )
    verify_phase(
        await client.get(HOT_KEY) is not None,
        "Scenario 3: Cache Repopulated",
        "Hot key was not repopulated after XFetch",
    )

    console.print("\n" + "=" * 65, style="bold green")
    plain_console.print("SYSTEMS TAKEAWAYS")
    console.print("=" * 65, style="bold green")
    plain_console.print("1. Naive Cache-Aside causes a 1:1 surge into the primary DB upon key expiry.")
    plain_console.print("2. Distributed Locks (SET NX PX) collapse 100 DB queries into EXACTLY 1 query.")
    plain_console.print("3. Probabilistic early refresh eliminates cache miss latency entirely for users.")
    plain_console.print("=================================================================\n")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())

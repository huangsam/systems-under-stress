# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
#     "redis>=5.0.0",
# ]
# ///
"""
Experiment 01: Client Connection Pool Starvation Under Downstream Latency Spikes

Demonstrates:
1. Microservice worker concurrency with a bounded BlockingConnectionPool (max_connections=5).
2. Healthy baseline: 15 concurrent requests recycle 5 connections in ~10ms with zero errors.
3. Chaos injection: 200ms latency toxic injected via Toxiproxy.
4. Cascading failure: The first 5 worker threads hold all available connections; queued threads
   breach the 80ms pool timeout and fail with ConnectionError ("No connection available.").
5. Systems takeaway: Why sub-second latency spikes in downstream dependencies silently
   starve upstream connection pools and thread pools, causing sudden service brownouts.
"""

from __future__ import annotations

import concurrent.futures
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Add experiments dir to sys.path so toxi_client can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent))

import toxi_client
from rich.console import Console
from rich.markup import escape
from rich.table import Table

import redis

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


REDIS_HOST = "127.0.0.1"
REDIS_PORT = 26379
POOL_SIZE = 5
POOL_TIMEOUT = 0.08  # 80ms acquisition timeout
WORKER_COUNT = 15


@dataclass
class TaskResult:
    worker_id: int
    acquired: bool
    duration_ms: float
    status: str
    error: str = ""


def worker_task(worker_id: int, pool: redis.BlockingConnectionPool) -> TaskResult:
    client = redis.Redis(connection_pool=pool)
    t0 = time.perf_counter()
    try:
        client.get("test_key")
        t1 = time.perf_counter()
        return TaskResult(
            worker_id=worker_id,
            acquired=True,
            duration_ms=(t1 - t0) * 1000,
            status="SUCCESS",
        )
    except redis.exceptions.ConnectionError as exc:
        t1 = time.perf_counter()
        return TaskResult(
            worker_id=worker_id,
            acquired=False,
            duration_ms=(t1 - t0) * 1000,
            status="TIMEOUT",
            error=str(exc),
        )
    except Exception as exc:
        t1 = time.perf_counter()
        return TaskResult(
            worker_id=worker_id,
            acquired=False,
            duration_ms=(t1 - t0) * 1000,
            status="ERROR",
            error=type(exc).__name__,
        )


def run_concurrency_batch(pool: redis.BlockingConnectionPool) -> list[TaskResult]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKER_COUNT) as executor:
        futures = [executor.submit(worker_task, i + 1, pool) for i in range(WORKER_COUNT)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    results.sort(key=lambda r: r.worker_id)
    return results


def main() -> None:
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("  EXPERIMENT 01: Connection Pool Starvation Under Latency Spikes  ")
    console.print(f"  Pool Limit: {POOL_SIZE} connections | Pool Timeout: {POOL_TIMEOUT * 1000:.0f}ms | Concurrency: {WORKER_COUNT} threads")
    console.print("=" * 70 + "\n", style="bold cyan")

    toxi_client.ensure_proxy()
    toxi_client.reset_all()

    # Pre-populate a test key
    admin = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
    admin.set("test_key", "healthy_payload")
    verify_phase(admin.get("test_key") == b"healthy_payload", "Phase 0 Seed", "expected test_key=='healthy_payload' via proxy")

    # -------------------------------------------------------------
    # Phase 1: Healthy Baseline
    # -------------------------------------------------------------
    console.print("[bold yellow]Phase 1: Healthy Baseline (Sub-millisecond Downstream)[/]")
    console.print(f"  Dispatching {WORKER_COUNT} concurrent threads against {POOL_SIZE}-connection pool...")

    baseline_pool = redis.BlockingConnectionPool(
        host=REDIS_HOST,
        port=REDIS_PORT,
        max_connections=POOL_SIZE,
        timeout=POOL_TIMEOUT,
    )

    t0 = time.perf_counter()
    baseline_results = run_concurrency_batch(baseline_pool)
    t1 = time.perf_counter()
    baseline_wall_ms = (t1 - t0) * 1000

    b_success = sum(1 for r in baseline_results if r.status == "SUCCESS")
    verify_phase(b_success == WORKER_COUNT, "Phase 1 Baseline", f"expected {WORKER_COUNT}/{WORKER_COUNT} healthy completions, got {b_success}")
    console.print(f"  ✔ All {b_success}/{WORKER_COUNT} tasks completed in [bold green]{baseline_wall_ms:.2f} ms[/].")
    console.print("  ✔ Sub-millisecond datastore response allows instant connection recycling.\n")

    # -------------------------------------------------------------
    # Phase 2: Chaos Injection - 200ms Downstream Latency
    # -------------------------------------------------------------
    console.print("[bold yellow]Phase 2: Injecting 200ms Downstream Latency Toxic via Toxiproxy[/]")
    try:
        toxi_client.add_latency(name="pool_starve_toxic", latency_ms=200, jitter_ms=10)
        console.print(f"  ✔ 200ms latency active. Connection acquisition timeout is set to {POOL_TIMEOUT * 1000:.0f}ms.")
        console.print(f"  Dispatching {WORKER_COUNT} concurrent threads...")

        chaos_pool = redis.BlockingConnectionPool(
            host=REDIS_HOST,
            port=REDIS_PORT,
            max_connections=POOL_SIZE,
            timeout=POOL_TIMEOUT,
        )

        t0 = time.perf_counter()
        chaos_results = run_concurrency_batch(chaos_pool)
        t1 = time.perf_counter()
        chaos_wall_ms = (t1 - t0) * 1000

        # Display comparison table
        table = Table(title="Thread Execution Waterfall (Phase 2: Degraded Latency)", border_style="dim")
        table.add_column("Worker ID", justify="center")
        table.add_column("Pool Acquired", justify="center")
        table.add_column("Wait / RTT", justify="right")
        table.add_column("Status", justify="center")
        table.add_column("Error Signature", justify="left")

        for r in chaos_results:
            c_status = "[bold green]SUCCESS[/]" if r.status == "SUCCESS" else "[bold red]FAILED[/]"
            c_acq = "[green]YES (Slot 1-5)[/]" if r.acquired else "[red]NO (Timed Out)[/]"
            err_msg = r.error if r.error else "[dim]-[/]"
            table.add_row(
                f"Worker-{r.worker_id:02d}",
                c_acq,
                f"{r.duration_ms:.1f} ms",
                c_status,
                err_msg,
            )

        console.print(table)
        console.print()

        # Metrics summary
        c_success = sum(1 for r in chaos_results if r.status == "SUCCESS")
        c_timeout = sum(1 for r in chaos_results if r.status == "TIMEOUT")

        console.print(f"[bold]Batch Wall Time:[/] {chaos_wall_ms:.1f} ms")
        console.print(f"  [green]✔ Succeeded (Held Connection):[/] {c_success} / {WORKER_COUNT}")
        console.print(f"  [red]✖ Failed (Pool Timeout Breach):[/] {c_timeout} / {WORKER_COUNT}\n")
        verify_phase(
            1 <= c_success <= POOL_SIZE and c_timeout >= WORKER_COUNT - POOL_SIZE and c_success + c_timeout == WORKER_COUNT,
            "Phase 2 Pool Starvation",
            f"expected <= {POOL_SIZE} successes and >= {WORKER_COUNT - POOL_SIZE} pool timeouts, got success={c_success}, timeout={c_timeout}",
        )
    finally:
        # Clean up
        toxi_client.reset_all()

    verify_phase(admin.ping() is True, "Phase 2 Recovery", "expected Redis ping recovery after toxic reset")

    # System Takeaways
    console.print("=" * 70, style="bold cyan")
    console.print("[bold]Production & Systems Takeaways:[/]")
    console.print("=" * 70, style="bold cyan")
    plain_console.print("1. Little's Law in Connection Pools: Concurrency = Throughput × Latency.")
    plain_console.print("   When datastore latency rises from 1ms to 200ms (200x), connection holding time jumps 200x.")
    plain_console.print(f"2. Because max_connections={POOL_SIZE}, only {POOL_SIZE} worker threads can make progress simultaneously.")
    plain_console.print(
        f"3. The remaining {WORKER_COUNT - POOL_SIZE} threads queue up. When queue wait time exceeds pool timeout ({POOL_TIMEOUT * 1000:.0f}ms),"
    )
    plain_console.print("   the application sheds load with 'ConnectionError: No connection available.'.")
    plain_console.print("4. Remediation: Set pool timeouts thoughtfully, configure circuit breakers (e.g. Hystrix/resilience4j),")
    plain_console.print("   and size connection pools according to upstream thread pools and peak latency budgets.")
    console.print()


if __name__ == "__main__":
    main()

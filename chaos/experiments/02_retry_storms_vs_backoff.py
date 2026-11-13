# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
#     "redis>=5.0.0",
# ]
# ///
"""
Experiment 02: Transient Outage Mitigation - Naive Retry Storms vs Full Jitter

Demonstrates:
1. Simulating a transient link failure (TCP reset_peer) lasting 0.8 seconds.
2. Strategy A (Naive Immediate Retry): 25 concurrent workers burn through all retry
   budgets in <50ms, creating a self-inflicted thundering herd (100+ burst reqs)
   and failing with a 100% error rate before the link recovers.
3. Strategy B (Exponential Backoff with Full Jitter - AWS Architecture Pattern):
   Spreads retries across time, smooths traffic spikes, and recovers 100% of tasks
   as soon as the link heals.
4. Systems takeaway: Why unjittered retries turn minor downstream hiccups into total
   cascading outages, and how Full Jitter mathematically desynchronizes workloads.
"""

from __future__ import annotations

import concurrent.futures
import random
import sys
import threading
import time
from dataclasses import dataclass, field
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
CLIENT_COUNT = 25
MAX_RETRIES = 5
OUTAGE_DURATION = 0.8  # seconds


@dataclass
class RetryRecord:
    client_id: int
    attempts: int = 0
    timestamps: list[float] = field(default_factory=list)
    success: bool = False
    total_time_ms: float = 0.0


def naive_retry_client(client_id: int) -> RetryRecord:
    record = RetryRecord(client_id=client_id)
    t0 = time.perf_counter()

    for attempt in range(MAX_RETRIES):
        record.attempts += 1
        record.timestamps.append(time.perf_counter() - t0)
        try:
            # Disable redis-py's internal retry to observe raw application-level retry behavior
            r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=0.2, retry=None)
            r.ping()
            record.success = True
            break
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
            if attempt == MAX_RETRIES - 1:
                break
            # Naive immediate retry: 0 delay!

    record.total_time_ms = (time.perf_counter() - t0) * 1000
    return record


def jitter_backoff_client(client_id: int, base: float = 0.15, cap: float = 1.5) -> RetryRecord:
    record = RetryRecord(client_id=client_id)
    t0 = time.perf_counter()

    for attempt in range(MAX_RETRIES):
        record.attempts += 1
        record.timestamps.append(time.perf_counter() - t0)
        try:
            r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=0.2, retry=None)
            r.ping()
            record.success = True
            break
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
            if attempt == MAX_RETRIES - 1:
                break
            # Full Jitter backoff: sleep_t = Uniform(0, min(cap, base * 2^attempt))
            max_backoff = min(cap, base * (2**attempt))
            sleep_t = random.uniform(0, max_backoff)
            time.sleep(sleep_t)

    record.total_time_ms = (time.perf_counter() - t0) * 1000
    return record


def main() -> None:
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("  EXPERIMENT 02: Retry Storms vs Exponential Backoff with Jitter  ")
    console.print(f"  Concurrency: {CLIENT_COUNT} clients | Max Retries: {MAX_RETRIES} | Outage Window: {OUTAGE_DURATION}s")
    console.print("=" * 70 + "\n", style="bold cyan")

    toxi_client.ensure_proxy()
    toxi_client.reset_all()

    # -------------------------------------------------------------
    # Scenario A: Naive Immediate Retry (Self-Inflicted DDoS)
    # -------------------------------------------------------------
    console.print("[bold yellow]Scenario A: Naive Immediate Retries (0 Backoff)[/]")
    console.print(f"  Triggering link outage (TCP reset_peer) for {OUTAGE_DURATION}s...")
    heal_timer = threading.Timer(OUTAGE_DURATION, toxi_client.reset_all)
    try:
        toxi_client.add_reset_peer(name="reset_outage")
        heal_timer.start()

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=CLIENT_COUNT) as executor:
            naive_records = list(executor.map(naive_retry_client, range(1, CLIENT_COUNT + 1)))
        naive_wall_ms = (time.perf_counter() - t0) * 1000
    finally:
        heal_timer.cancel()
        heal_timer.join()
        toxi_client.reset_all()

    naive_success = sum(1 for r in naive_records if r.success)
    naive_total_reqs = sum(r.attempts for r in naive_records)
    naive_pct = (naive_success / CLIENT_COUNT) * 100

    console.print(f"  ✖ Wall Time: [bold red]{naive_wall_ms:.1f} ms[/]")
    console.print(f"  ✖ Success Count: [bold red]{naive_success} / {CLIENT_COUNT}[/] ({naive_pct:.0f}% recovery)")
    console.print(f"  ✖ Total Requests Fired: [bold red]{naive_total_reqs}[/] in {naive_wall_ms:.1f}ms (exhausted before link healed!)\n")
    verify_phase(
        naive_success == 0 and naive_total_reqs == CLIENT_COUNT * MAX_RETRIES,
        "Scenario A Naive Retry Storm",
        f"expected 0 recoveries and {CLIENT_COUNT * MAX_RETRIES} exhausted attempts, got success={naive_success}, reqs={naive_total_reqs}",
    )

    # -------------------------------------------------------------
    # Scenario B: Exponential Backoff with Full Jitter
    # -------------------------------------------------------------
    console.print("[bold yellow]Scenario B: Exponential Backoff with Full Jitter (AWS Pattern)[/]")
    console.print(f"  Triggering link outage (TCP reset_peer) for {OUTAGE_DURATION}s...")
    heal_timer = threading.Timer(OUTAGE_DURATION, toxi_client.reset_all)
    try:
        toxi_client.add_reset_peer(name="reset_outage")
        heal_timer.start()

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=CLIENT_COUNT) as executor:
            jitter_records = list(executor.map(jitter_backoff_client, range(1, CLIENT_COUNT + 1)))
        jitter_wall_ms = (time.perf_counter() - t0) * 1000
    finally:
        heal_timer.cancel()
        heal_timer.join()
        toxi_client.reset_all()

    jitter_success = sum(1 for r in jitter_records if r.success)
    jitter_total_reqs = sum(r.attempts for r in jitter_records)
    jitter_pct = (jitter_success / CLIENT_COUNT) * 100

    console.print(f"  ✔ Wall Time: [bold green]{jitter_wall_ms:.1f} ms[/]")
    console.print(f"  ✔ Success Count: [bold green]{jitter_success} / {CLIENT_COUNT}[/] ({jitter_pct:.0f}% recovery)")
    console.print(f"  ✔ Total Requests Fired: [bold green]{jitter_total_reqs}[/] (desynchronized retries)\n")
    verify_phase(
        jitter_success > naive_success and jitter_success >= CLIENT_COUNT // 2,
        "Scenario B Full Jitter Recovery",
        f"expected >= {CLIENT_COUNT // 2}/{CLIENT_COUNT} recoveries with jitter, got {jitter_success}/{CLIENT_COUNT}",
    )

    # -------------------------------------------------------------
    # Comparative Matrix
    # -------------------------------------------------------------
    comp_table = Table(
        title="Retry Strategy Comparison Under 0.8s Transient Link Outage",
        border_style="dim",
    )
    comp_table.add_column("Strategy", style="bold")
    comp_table.add_column("Success Rate", justify="center")
    comp_table.add_column("Total Requests", justify="right")
    comp_table.add_column("Burst Profile", justify="left")
    comp_table.add_column("Outcome", justify="center")

    comp_table.add_row(
        "Naive Immediate",
        f"[red]{naive_success}/{CLIENT_COUNT} ({naive_pct:.0f}%)[/]",
        f"[red]{naive_total_reqs}[/]",
        f"Synchronized cluster ({naive_total_reqs} reqs in {naive_wall_ms:.0f}ms)",
        "[bold red]FAILED[/]",
    )
    comp_table.add_row(
        "Full Jitter Backoff",
        f"[green]{jitter_success}/{CLIENT_COUNT} ({jitter_pct:.0f}%)[/]",
        f"[green]{jitter_total_reqs}[/]",
        f"Desynchronized ({jitter_total_reqs} reqs spread over {jitter_wall_ms / 1000:.1f}s)",
        "[bold green]RECOVERED[/]",
    )

    console.print(comp_table)
    console.print()

    # Systems takeaways
    console.print("=" * 70, style="bold cyan")
    console.print("[bold]Production & Systems Takeaways:[/]")
    console.print("=" * 70, style="bold cyan")
    plain_console.print("1. The Thundering Herd / Retry Storm Trap:")
    plain_console.print("   When an upstream service has a 1-second transient hiccup, clients retrying immediately")
    plain_console.print("   waste their entire retry budget in milliseconds, guaranteeing 100% failure.")
    plain_console.print("2. Why Jitter is Mandatory:")
    plain_console.print("   Pure exponential backoff (without random jitter) causes all clients to retry in lockstep")
    plain_console.print("   waves (at t=100ms, t=200ms, t=400ms), repeatedly re-crashing the upstream service.")
    plain_console.print("3. Full Jitter Formula (AWS Architecture Center):")
    plain_console.print("   sleep = random.uniform(0, min(cap, base * 2^attempt)).")
    plain_console.print("   Spreading retries uniformly over the backoff interval breaks phase synchronization")
    plain_console.print("   and converts spiky retry spikes into flat, manageable background traffic.")
    console.print()


if __name__ == "__main__":
    main()

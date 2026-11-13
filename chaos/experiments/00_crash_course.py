# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
#     "redis>=5.0.0",
# ]
# ///
"""
Network Chaos Crash Course: Dynamic Fault Injection with Toxiproxy

Demonstrates:
1. Toxiproxy architecture & control plane API verification.
2. Baseline latency & throughput through loopback proxy (127.0.0.1:26379).
3. Dynamic Latency & Jitter injection (testing network delay).
4. Bandwidth throttling toxic (simulating congested links).
5. Dynamic toxic removal & sub-second recovery verification.
"""

from __future__ import annotations

import sys
import time
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


def main() -> None:
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("[bold green]Network Chaos Crash Course: Toxiproxy Fault Injection[/]")
    console.print("=" * 70 + "\n", style="bold cyan")

    # Step 1: Initialize Proxy
    console.print("[bold yellow]Step 1: Verifying Toxiproxy Control Plane & Upstream Proxy[/]")
    try:
        proxy_info = toxi_client.ensure_proxy()
        toxi_client.reset_all()
        console.print(f"  ✔ Toxiproxy active at [cyan]{toxi_client.TOXIPROXY_URL}[/]")
        console.print(
            f"  ✔ Proxy '[bold]{proxy_info.get('name')}[/]' listening on [cyan]{toxi_client.LISTEN_ADDR}[/] -> upstream [cyan]{toxi_client.UPSTREAM_ADDR}[/]\n"
        )
    except Exception as exc:
        console.print(f"[bold red]Failed to connect to Toxiproxy: {exc}[/]")
        console.print("[dim]Make sure to run: make up EXP=chaos[/]")
        sys.exit(1)

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5.0)

    # Step 2: Baseline Performance
    console.print("[bold yellow]Step 2: Measuring Baseline Round-Trip Latency (No Toxics)[/]")
    latencies: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        pong = r.ping()
        t1 = time.perf_counter()
        verify_phase(pong is True, "Step 2 Ping", f"expected PONG (True), got {pong!r}")
        latencies.append((t1 - t0) * 1000)

    avg_baseline = sum(latencies) / len(latencies)
    verify_phase(avg_baseline < 50.0, "Step 2 Baseline RTT", f"expected baseline RTT < 50ms, got {avg_baseline:.2f}ms")
    console.print(f"  ✔ 5 Pings completed. Average baseline RTT: [bold green]{avg_baseline:.2f} ms[/]\n")

    try:
        # Step 3: Latency & Jitter Injection
        console.print("[bold yellow]Step 3: Injecting Downstream Latency (120ms + 20ms Jitter)[/]")
        toxi_client.add_latency(name="latency_demo", latency_ms=120, jitter_ms=20)
        console.print("  ✔ Toxic 'latency_demo' active on downstream response stream.")

        lat_table = Table(title="Latency Comparison (Baseline vs Injected Toxic)", border_style="dim")
        lat_table.add_column("Iteration", justify="center")
        lat_table.add_column("Baseline RTT", justify="right")
        lat_table.add_column("Injected RTT", justify="right")
        lat_table.add_column("Added Overhead", justify="right")

        injected_latencies: list[float] = []
        for i in range(1, 6):
            t0 = time.perf_counter()
            r.ping()
            t1 = time.perf_counter()
            injected_ms = (t1 - t0) * 1000
            injected_latencies.append(injected_ms)
            lat_table.add_row(
                str(i),
                f"{latencies[i - 1]:.2f} ms",
                f"[bold yellow]{injected_ms:.2f} ms[/]",
                f"+{injected_ms - latencies[i - 1]:.2f} ms",
            )

        console.print(lat_table)
        console.print()
        avg_injected = sum(injected_latencies) / len(injected_latencies)
        verify_phase(
            avg_injected > avg_baseline + 80.0,
            "Step 3 Latency Injection",
            f"expected average injected RTT ({avg_injected:.2f}ms) > baseline + 80ms ({avg_baseline + 80.0:.2f}ms)",
        )

        # Step 4: Bandwidth Throttling
        console.print("[bold yellow]Step 4: Bandwidth Throttling (Simulating Saturated Link)[/]")
        toxi_client.reset_all()
        # Inject 30 KB/s rate limit
        toxi_client.add_bandwidth(name="bw_demo", rate_kb=30)
        console.print("  ✔ Bandwidth throttled to [bold cyan]30 KB/s[/] on downstream stream.")

        # Write a 60KB payload and measure read time
        payload = b"X" * 60_000
        r.set("bulk_payload", payload)

        t0 = time.perf_counter()
        retrieved = r.get("bulk_payload")
        t1 = time.perf_counter()
        duration_s = t1 - t0
        effective_rate = (len(retrieved or b"") / 1024) / duration_s if duration_s > 0 else 0

        verify_phase(
            retrieved == payload and duration_s >= 1.0,
            "Step 4 Bandwidth Throttle",
            f"expected 60KB exact payload in >=1.0s at 30KB/s, got bytes={len(retrieved or b'')}, duration={duration_s:.2f}s",
        )
        console.print(f"  ✔ Retrieved 60KB payload in [bold yellow]{duration_s:.2f} s[/] (~{effective_rate:.1f} KB/s)")
        console.print()
    finally:
        # Step 5: Toxic Removal & Recovery
        console.print("[bold yellow]Step 5: Clearing Toxics & Verifying System Recovery[/]")
        toxi_client.reset_all()

    t0 = time.perf_counter()
    pong_after = r.ping()
    t1 = time.perf_counter()
    recovered_ms = (t1 - t0) * 1000
    remaining_toxics = toxi_client.list_toxics()
    verify_phase(
        pong_after is True and recovered_ms < 50.0 and len(remaining_toxics) == 0,
        "Step 5 Recovery",
        f"expected fast ping and 0 toxics after reset, got pong={pong_after!r}, rtt={recovered_ms:.2f}ms, toxics={remaining_toxics}",
    )

    console.print("  ✔ All toxics wiped via [cyan]POST /reset[/].")
    console.print(f"  ✔ Post-recovery RTT: [bold green]{recovered_ms:.2f} ms[/] (back to normal sub-millisecond)\n")

    console.print("=" * 70, style="bold cyan")
    console.print("[bold green]Toxiproxy Crash Course Completed Successfully![/]")
    console.print("=" * 70 + "\n", style="bold cyan")


if __name__ == "__main__":
    main()

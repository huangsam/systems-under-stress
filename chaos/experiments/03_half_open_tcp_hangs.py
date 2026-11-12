# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
#     "redis>=5.0.0",
# ]
# ///
"""
Experiment 03: Silent Half-Open TCP Socket Hangs & Missing Read Timeouts

Demonstrates:
1. Simulating a silent cloud network drop (AWS NAT Gateway state table timeout,
   inter-AZ packet black hole where no TCP FIN or RST is ever transmitted).
2. Toxiproxy toxic: timeout with 0 delay (silently swallows all egress packets).
3. Client A (Naive Client: socket_timeout=None):
   - Thread issues a read and hangs indefinitely in kernel socket.recv().
   - The thread remains completely blocked after 2.5 seconds (in production, Linux
     TCP retransmission timer will freeze this thread for 15 to 20 minutes!).
4. Client B (Resilient Client: socket_timeout=1.0s):
   - Thread detects the silent black hole in exactly 1.0s.
   - Throws redis.exceptions.TimeoutError and cleanly releases execution context.
5. Systems takeaway: Why every network client in production MUST set explicit
   socket read timeouts, and the mechanics of TCP keepalive probes.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Add experiments dir to sys.path so toxi_client can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent))

import toxi_client
from rich.console import Console
from rich.table import Table

import redis

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 26379
HANG_OBSERVE_WINDOW = 2.5  # seconds to observe hung thread


@dataclass
class ClientProbeResult:
    label: str
    timeout_cfg: str
    elapsed_ms: float
    thread_state: str
    status: str
    exception_caught: str = ""


def test_naive_client() -> ClientProbeResult:
    result = ClientProbeResult(
        label="Naive Client",
        timeout_cfg="socket_timeout=None",
        elapsed_ms=0.0,
        thread_state="UNKNOWN",
        status="PENDING",
    )

    t0 = time.perf_counter()

    def run_query():
        # Establish connection first, then attempt command under toxic
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=None, retry=None)
        try:
            r.ping()
            result.status = "UNEXPECTED_SUCCESS"
        except Exception as exc:
            result.status = "CAUGHT_EXCEPTION"
            result.exception_caught = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=run_query, daemon=True)
    worker.start()
    worker.join(timeout=HANG_OBSERVE_WINDOW)

    t1 = time.perf_counter()
    result.elapsed_ms = (t1 - t0) * 1000

    if worker.is_alive():
        result.thread_state = "FROZEN (STILL HUNG)"
        result.status = "HUNG_INDEFINITELY"
        result.exception_caught = "None (Blocked in socket.recv)"
    else:
        result.thread_state = "TERMINATED"

    return result


def test_resilient_client() -> ClientProbeResult:
    result = ClientProbeResult(
        label="Resilient Client",
        timeout_cfg="socket_timeout=1.0s",
        elapsed_ms=0.0,
        thread_state="UNKNOWN",
        status="PENDING",
    )

    t0 = time.perf_counter()

    def run_query():
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=1.0, retry=None)
        try:
            r.ping()
            result.status = "UNEXPECTED_SUCCESS"
        except Exception as exc:
            result.status = "TIMED_OUT_CLEANLY"
            result.exception_caught = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=run_query, daemon=True)
    worker.start()
    worker.join(timeout=HANG_OBSERVE_WINDOW)

    t1 = time.perf_counter()
    result.elapsed_ms = (t1 - t0) * 1000
    result.thread_state = "FREED (THREAD RECYCLED)" if not worker.is_alive() else "STILL_ALIVE"

    return result


def main() -> None:
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("  EXPERIMENT 03: Silent Half-Open TCP Socket Hangs  ")
    console.print("  Simulating Cloud Packet Black Hole (No TCP FIN/RST)  ")
    console.print("=" * 70 + "\n", style="bold cyan")

    toxi_client.ensure_proxy()
    toxi_client.reset_all()

    # Pre-warm connection before adding toxic so socket is established
    warmup = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=2.0)
    warmup.ping()

    # Inject silent timeout toxic: drops all packets without sending FIN/RST
    console.print("[bold yellow]Phase 1: Injecting Silent Network Black Hole Toxic (timeout=0)[/]")
    toxi_client.add_timeout(name="black_hole_toxic", timeout_ms=0)
    console.print("  ✔ Downstream packets are silently discarded without closing TCP socket.")
    console.print("  ✔ Simulates stateful firewall expiration or silent cloud NAT gateway drops.\n")

    # -------------------------------------------------------------
    # Test Client A: Naive (No Timeout)
    # -------------------------------------------------------------
    console.print(f"[bold yellow]Phase 2: Probing Client A (socket_timeout=None) for {HANG_OBSERVE_WINDOW}s...[/]")
    naive_res = test_naive_client()
    console.print(f"  ✖ Worker thread remained: [bold red]{naive_res.thread_state}[/] after {naive_res.elapsed_ms:.1f}ms.")
    console.print("  ✖ Socket blocked in kernel recv(). Zero signals received.\n")

    # -------------------------------------------------------------
    # Test Client B: Resilient (Explicit Timeout)
    # -------------------------------------------------------------
    console.print("[bold yellow]Phase 3: Probing Client B (socket_timeout=1.0s)...[/]")
    resilient_res = test_resilient_client()
    console.print(f"  ✔ Worker thread state: [bold green]{resilient_res.thread_state}[/] in {resilient_res.elapsed_ms:.1f}ms.")
    console.print(f"  ✔ Caught expected error: [bold green]{resilient_res.exception_caught}[/]\n")

    # Clean up
    toxi_client.reset_all()

    # -------------------------------------------------------------
    # Comparison Matrix
    # -------------------------------------------------------------
    comp_table = Table(title="TCP Silent Drop Behavior Comparison", border_style="dim")
    comp_table.add_column("Client Type", style="bold")
    comp_table.add_column("Socket Timeout", justify="center")
    comp_table.add_column("Observed Wait", justify="right")
    comp_table.add_column("Thread State After 2.5s", justify="center")
    comp_table.add_column("Failure Signature", justify="left")

    comp_table.add_row(
        naive_res.label,
        naive_res.timeout_cfg,
        f"{naive_res.elapsed_ms:.0f} ms",
        f"[bold red]{naive_res.thread_state}[/]",
        f"[red]{naive_res.exception_caught}[/]",
    )
    comp_table.add_row(
        resilient_res.label,
        resilient_res.timeout_cfg,
        f"{resilient_res.elapsed_ms:.0f} ms",
        f"[bold green]{resilient_res.thread_state}[/]",
        f"[green]{resilient_res.exception_caught}[/]",
    )

    console.print(comp_table)
    console.print()

    # Systems takeaways
    console.print("=" * 70, style="bold cyan")
    console.print("[bold]Production & Systems Takeaways:[/]")
    console.print("=" * 70, style="bold cyan")
    plain_console.print("1. Why TCP Does Not Know You Are Disconnected:")
    plain_console.print("   TCP is fundamentally a silent protocol. If an intermediate router or cloud NAT gateway")
    plain_console.print("   drops state without sending a TCP RST packet, the socket will wait indefinitely for ACK/data.")
    plain_console.print("2. The Linux Default TCP Retransmission Trap:")
    plain_console.print("   By default, Linux retries unacknowledged packets 15 times with exponential backoff (tcp_retries2 = 15).")
    plain_console.print("   This means without an application-level socket_timeout, a thread will hang for ~924 seconds (15+ minutes)!")
    plain_console.print("3. Defense in Depth:")
    plain_console.print("   - connect_timeout: Protects initial TCP SYN handshake (e.g. 1.0s).")
    plain_console.print("   - socket_timeout (read timeout): Protects ongoing query execution (e.g. 2.0s).")
    plain_console.print("   - TCP Keepalive: Enables kernel-level background probes (SO_KEEPALIVE, tcp_keepalive_time).")
    console.print()


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "psycopg[binary]>=3.1.0",
#     "rich>=13.7.0",
# ]
# ///
"""
Experiment 05: PostgreSQL Migration Lock Queues (ACCESS EXCLUSIVE Pileup)

Demonstrates:
1. Why a harmless-looking ALTER TABLE behind a slow SELECT stalls ALL traffic:
   - Long-running reader holds AccessShareLock on the target table.
   - ALTER TABLE requests AccessExclusiveLock and blocks in the lock queue.
   - Subsequent normal SELECT/INSERT queries (which only need AccessShareLock /
     RowExclusiveLock, both compatible with the reader) queue BEHIND the waiting
     ALTER TABLE due to PostgreSQL's FIFO lock queue.
2. Live inspection of pg_stat_activity and pg_blocking_pids() during the pileup.
3. Remediation: SET LOCAL lock_timeout = '200ms' causes the migration to fail
   fast with SQLSTATE 55P03 (LockNotAvailable) instead of piling up app traffic.
"""

from __future__ import annotations

import concurrent.futures
import sys
import threading
import time
from dataclasses import dataclass

import psycopg
from psycopg.errors import LockNotAvailable
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)

DB_CONN = "postgresql://postgres:postgres@127.0.0.1:5432/lab_db"
TABLE_NAME = "lock_queue_orders"
NUM_APP_WORKERS = 4


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


@dataclass
class WorkerResult:
    worker_id: int
    op: str
    elapsed_ms: float
    ok: bool
    error: str = ""


def setup_table() -> None:
    with psycopg.connect(DB_CONN, autocommit=True, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_NAME};")
            cur.execute(f"""
                CREATE TABLE {TABLE_NAME} (
                    id serial PRIMARY KEY,
                    amount numeric(10, 2) NOT NULL
                );
            """)
            cur.execute(f"INSERT INTO {TABLE_NAME} (amount) SELECT 10.00 FROM generate_series(1, 50);")


def run_long_reader(hold_seconds: float, reader_ready: threading.Event, stop_early: threading.Event) -> None:
    with psycopg.connect(DB_CONN, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME};")
            cur.fetchone()
            reader_ready.set()
            deadline = time.monotonic() + hold_seconds
            while time.monotonic() < deadline and not stop_early.is_set():
                time.sleep(0.05)
        conn.commit()


def run_app_worker(worker_id: int) -> WorkerResult:
    op = "SELECT" if worker_id % 2 == 0 else "INSERT"
    t0 = time.perf_counter()
    try:
        with psycopg.connect(DB_CONN, autocommit=True, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                if op == "SELECT":
                    cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME};")
                    cur.fetchone()
                else:
                    cur.execute(f"INSERT INTO {TABLE_NAME} (amount) VALUES (42.50);")
        return WorkerResult(worker_id, op, (time.perf_counter() - t0) * 1000, True)
    except Exception as exc:
        return WorkerResult(worker_id, op, (time.perf_counter() - t0) * 1000, False, str(exc))


def snapshot_blocked_queries() -> list[tuple[int, str, str | None, list[int], str]]:
    with psycopg.connect(DB_CONN, autocommit=True, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    pid,
                    wait_event,
                    state,
                    pg_blocking_pids(pid) AS blockers,
                    left(query, 60) AS query_preview
                FROM pg_stat_activity
                WHERE datname = 'lab_db'
                  AND pid <> pg_backend_pid()
                  AND wait_event_type = 'Lock'
                ORDER BY pid;
            """)
            return cur.fetchall()


def phase1_unbounded_pileup() -> list[WorkerResult]:
    console.print("\n[bold red]Phase 1: Unbounded ALTER TABLE (No lock_timeout)[/]")
    console.print("[dim]  1. Reader acquires AccessShareLock and holds transaction open for 2.5s.[/]")
    console.print("[dim]  2. Migration runs ALTER TABLE (requests AccessExclusiveLock -> queues behind reader).[/]")
    console.print("[dim]  3. 4 app workers (SELECT/INSERT) arrive 0.25s later -> queue behind ALTER TABLE.[/]\n")

    reader_ready = threading.Event()
    stop_reader = threading.Event()
    migration_elapsed_ms: list[float] = []

    def run_migration() -> None:
        t0 = time.perf_counter()
        with psycopg.connect(DB_CONN, autocommit=True, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN note text DEFAULT '';")
        migration_elapsed_ms.append((time.perf_counter() - t0) * 1000)

    reader_thread = threading.Thread(target=run_long_reader, args=(2.5, reader_ready, stop_reader), daemon=True)
    reader_thread.start()
    reader_ready.wait(timeout=5.0)

    mig_thread = threading.Thread(target=run_migration, daemon=True)
    mig_thread.start()
    time.sleep(0.25)

    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_APP_WORKERS) as pool:
        futures = [pool.submit(run_app_worker, i) for i in range(NUM_APP_WORKERS)]
        time.sleep(0.35)
        blocked_rows = snapshot_blocked_queries()
        results = [f.result() for f in futures]

    mig_thread.join(timeout=10.0)
    reader_thread.join(timeout=10.0)

    table = Table(title="Live pg_stat_activity Lock Queue Snapshot (Phase 1)")
    table.add_column("PID", style="cyan")
    table.add_column("Wait Event", style="yellow")
    table.add_column("Blocking PIDs", style="red")
    table.add_column("Query Preview", style="white")
    for pid, wait_ev, _state, blockers, q in blocked_rows:
        table.add_row(str(pid), str(wait_ev), str(blockers), q.strip())
    console.print(table)

    min_worker_ms = min(r.elapsed_ms for r in results)
    console.print(
        f"  [yellow]Migration wait:[/] {migration_elapsed_ms[0]:.1f}ms | "
        f"[red]App workers blocked:[/] {len(blocked_rows) - 1}/{NUM_APP_WORKERS} "
        f"(min latency: {min_worker_ms:.1f}ms)"
    )

    verify_phase(
        len(blocked_rows) >= NUM_APP_WORKERS + 1,
        "Phase 1: Lock Queue Snapshot",
        f"Expected migration + {NUM_APP_WORKERS} app workers in lock queue, saw {len(blocked_rows)}",
    )
    verify_phase(
        all(r.ok and r.elapsed_ms >= 1500.0 for r in results),
        "Phase 1: App Latency Stall",
        f"Expected all app workers stalled >=1500ms behind ALTER TABLE, got {[round(r.elapsed_ms, 1) for r in results]}",
    )
    return results


def phase2_safe_lock_timeout() -> list[WorkerResult]:
    console.print("\n[bold green]Phase 2: Safe Migration with SET LOCAL lock_timeout = '200ms'[/]")
    console.print("[dim]  1. Reader holds AccessShareLock for 2.0s.[/]")
    console.print("[dim]  2. Migration sets lock_timeout=200ms and aborts cleanly (SQLSTATE 55P03).[/]")
    console.print("[dim]  3. 4 app workers execute immediately without queuing behind the migration.[/]\n")

    reader_ready = threading.Event()
    stop_reader = threading.Event()
    reader_thread = threading.Thread(target=run_long_reader, args=(2.0, reader_ready, stop_reader), daemon=True)
    reader_thread.start()
    reader_ready.wait(timeout=5.0)

    caught_code = None
    t0 = time.perf_counter()
    try:
        with psycopg.connect(DB_CONN, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '200ms';")
                cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN tag text DEFAULT '';")
            conn.commit()
    except LockNotAvailable as exc:
        caught_code = exc.sqlstate
    mig_ms = (time.perf_counter() - t0) * 1000

    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_APP_WORKERS) as pool:
        results = list(pool.map(run_app_worker, range(NUM_APP_WORKERS)))

    stop_reader.set()
    reader_thread.join(timeout=5.0)

    max_worker_ms = max(r.elapsed_ms for r in results)
    console.print(
        f"  [green]Migration aborted cleanly:[/] SQLSTATE {caught_code} in {mig_ms:.1f}ms | "
        f"[green]App workers max latency (while reader active):[/] {max_worker_ms:.1f}ms"
    )

    verify_phase(
        caught_code == "55P03" and mig_ms < 800.0,
        "Phase 2: Fast Lock Timeout",
        f"Expected LockNotAvailable (55P03) <800ms, got sqlstate={caught_code} in {mig_ms:.1f}ms",
    )
    verify_phase(
        all(r.ok and r.elapsed_ms < 500.0 for r in results),
        "Phase 2: Unblocked App Traffic",
        f"Expected all app workers <500ms, got {[round(r.elapsed_ms, 1) for r in results]}",
    )
    return results


def main() -> None:
    console.print("\n" + "=" * 72, style="bold cyan")
    console.print("  EXPERIMENT 05: MIGRATION LOCK QUEUES (ACCESS EXCLUSIVE PILEUP)", style="bold cyan")
    console.print("=" * 72, style="bold cyan")

    try:
        setup_table()
    except Exception as exc:
        console.print(f"[bold red][!] Could not connect to PostgreSQL: {escape(str(exc))}[/]")
        plain_console.print("Start the lab container first: cd postgres && make up EXP=locks")
        sys.exit(1)

    p1 = phase1_unbounded_pileup()
    p2 = phase2_safe_lock_timeout()

    summary = Table(title="\nSummary: Unbounded ALTER TABLE vs lock_timeout")
    summary.add_column("Scenario", style="cyan")
    summary.add_column("App Workers OK", justify="center")
    summary.add_column("Min Worker Latency", justify="right")
    summary.add_column("Max Worker Latency", justify="right")
    summary.add_row(
        "Unbounded ALTER TABLE",
        f"{sum(r.ok for r in p1)}/{len(p1)}",
        f"{min(r.elapsed_ms for r in p1):.1f} ms",
        f"{max(r.elapsed_ms for r in p1):.1f} ms",
    )
    summary.add_row(
        "SET LOCAL lock_timeout='200ms'",
        f"{sum(r.ok for r in p2)}/{len(p2)}",
        f"{min(r.elapsed_ms for r in p2):.1f} ms",
        f"{max(r.elapsed_ms for r in p2):.1f} ms",
    )
    console.print(summary)
    console.print("\n[bold green]✔ Experiment 05 completed and verified.[/]")


if __name__ == "__main__":
    main()

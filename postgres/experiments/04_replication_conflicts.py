# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "psycopg[binary]>=3.1.0",
# ]
# ///
"""
Experiment 04: PostgreSQL Physical Streaming Replication & Standby Query Conflicts

Demonstrates:
1. Baseline Streaming Replication:
   - Primary streams WAL to Standby via walreceiver.
   - Measures replication state, LSN positions, and baseline byte lag (0 bytes).
2. Standby Query Cancellation Conflict (Recovery Conflict):
   - Standby holds a snapshot (REPEATABLE READ) scanning table accounts.
   - Primary deletes 10,000 rows and executes VACUUM accounts, generating cleanup WAL.
   - Standby recovery exceeds max_standby_streaming_delay (1.0s) waiting for snapshot.
   - Standby cancels the query with:
     canceling statement due to conflict with recovery
     DETAIL: User query might have needed to see row versions that must be removed.
3. Remediation via hot_standby_feedback:
   - Enabling hot_standby_feedback advertises the standby's active xmin horizon to primary.
   - Primary defers tuple pruning during VACUUM, allowing the standby query to complete.
"""

from __future__ import annotations

import sys
import threading
import time

import psycopg
from psycopg import errors
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

PRIMARY_CONN = "postgresql://postgres:postgres@127.0.0.1:5432/lab_db"
STANDBY_CONN = "postgresql://postgres:postgres@127.0.0.1:5433/lab_db"

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def set_standby_feedback(enabled: bool) -> None:
    """Toggles hot_standby_feedback dynamically on the standby."""
    val = "on" if enabled else "off"
    with psycopg.connect(STANDBY_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"ALTER SYSTEM SET hot_standby_feedback = '{val}';")
            cur.execute("SELECT pg_reload_conf();")
    # Wait for SIGHUP reload and WAL sender communication
    time.sleep(1.0)


def wait_for_standby_sync(timeout: float = 10.0) -> None:
    """Waits until the standby has caught up with the primary's current WAL position."""
    start = time.perf_counter()
    with psycopg.connect(PRIMARY_CONN, autocommit=True) as p_conn:
        with p_conn.cursor() as p_cur:
            p_cur.execute("SELECT pg_current_wal_lsn();")
            primary_lsn = p_cur.fetchone()[0]

    while time.perf_counter() - start < timeout:
        with psycopg.connect(STANDBY_CONN, autocommit=True) as s_conn:
            with s_conn.cursor() as s_cur:
                s_cur.execute("SELECT pg_last_wal_replay_lsn();")
                standby_lsn = s_cur.fetchone()[0]
                if standby_lsn is not None and standby_lsn >= primary_lsn:
                    return
        time.sleep(0.1)


def seed_accounts(num_rows: int = 10_000) -> None:
    """Seeds accounts on primary and waits for replication catch-up."""
    with psycopg.connect(PRIMARY_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE accounts RESTART IDENTITY;")
            cur.execute(
                """
                INSERT INTO accounts (owner, balance)
                SELECT 'user_' || i, 100.00
                FROM generate_series(1, %s) AS i;
                """,
                (num_rows,),
            )
    wait_for_standby_sync()


# ----------------------------------------------------------------------
# Part 1: Replication Status & Baseline Lag Verification
# ----------------------------------------------------------------------
def test_replication_status() -> None:
    console.print("\n[bold blue][*] Part 1: Physical Streaming Replication Status Verification...[/]")

    with psycopg.connect(PRIMARY_CONN, autocommit=True) as p_conn:
        with p_conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    application_name,
                    client_addr,
                    state,
                    sync_state,
                    sent_lsn,
                    replay_lsn,
                    COALESCE(pg_wal_lsn_diff(sent_lsn, replay_lsn), 0) AS replay_lag_bytes
                FROM pg_stat_replication;
                """
            )
            rep_row = cur.fetchone()

    with psycopg.connect(STANDBY_CONN, autocommit=True) as s_conn:
        with s_conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pg_is_in_recovery(),
                    pg_last_wal_receive_lsn(),
                    pg_last_wal_replay_lsn();
                """
            )
            standby_row = cur.fetchone()

    if not rep_row:
        console.print("[bold red]✖ Error: No active walreceiver detected on Primary![/]")
        sys.exit(1)

    app_name, client_ip, state, sync_state, sent_lsn, replay_lsn, lag_bytes = rep_row
    in_recovery, recv_lsn, replayed_lsn = standby_row

    table = Table(title="PostgreSQL Streaming Replication Status", box=box.ROUNDED)
    table.add_column("Node", style="bold cyan", no_wrap=True)
    table.add_column("Role", style="bold", no_wrap=True)
    table.add_column("Address", style="dim", no_wrap=True)
    table.add_column("State / Mode", style="green", no_wrap=True)
    table.add_column("Replication LSN", style="yellow", no_wrap=True)
    table.add_column("Byte Lag", justify="right", style="bold magenta", no_wrap=True)

    table.add_row(
        "postgres-primary",
        "Primary (Read-Write)",
        "127.0.0.1:5432",
        f"{state} ({sync_state})",
        f"sent: {sent_lsn}",
        f"{lag_bytes} B",
    )
    table.add_row(
        "postgres-standby",
        "Hot Standby (Read-Only)",
        "127.0.0.1:5433",
        f"recovery: {in_recovery}",
        f"replay: {replayed_lsn}",
        f"{lag_bytes} B",
    )

    console.print(table)
    verify_phase(
        state == "streaming" and in_recovery is True and lag_bytes == 0,
        "Part 1: Streaming Replication",
        f"Expected state='streaming', in_recovery=True, lag_bytes=0; got state={state!r}, in_recovery={in_recovery}, lag_bytes={lag_bytes}",
    )
    plain_console.print("  [✓] Replication link healthy. Baseline byte lag is 0 bytes.")


# ----------------------------------------------------------------------
# Part 2: Standby Query Cancellation Conflict (Recovery Conflict)
# ----------------------------------------------------------------------
def test_standby_query_conflict() -> None:
    console.print("\n[bold blue][*] Part 2: Standby Query Cancellation Conflict (hot_standby_feedback = off)...[/]")
    set_standby_feedback(False)

    console.print("  [>] Seeding 10,000 accounts on Primary...")
    seed_accounts(10_000)

    # Verify rows on Standby
    with psycopg.connect(STANDBY_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM accounts;")
            cnt = cur.fetchone()[0]
            plain_console.print(f"  [>] Standby verified row count: {cnt:,} accounts")
            verify_phase(cnt == 10_000, "Part 2: Standby Seed Sync", f"Expected 10,000 rows on standby, got {cnt}")
            cur.execute("SELECT confl_snapshot FROM pg_stat_database_conflicts WHERE datname = current_database();")
            conflicts_before = cur.fetchone()[0]

    snapshot_held = threading.Event()
    conflict_error: Exception | None = None

    def standby_query_worker() -> None:
        nonlocal conflict_error
        try:
            with psycopg.connect(STANDBY_CONN, autocommit=False) as conn:
                conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                with conn.cursor() as cur:
                    # Establish snapshot
                    cur.execute("SELECT count(*) FROM accounts;")
                    initial_count = cur.fetchone()[0]
                    plain_console.print(f"  [Standby] Transaction opened (REPEATABLE READ). Initial count: {initial_count:,} rows.")
                    snapshot_held.set()

                    # Hold snapshot to trigger conflict when cleanup WAL arrives
                    plain_console.print("  [Standby] Holding snapshot; sleeping 2.5s (exceeds max_standby_streaming_delay=1s)...")
                    time.sleep(2.5)

                    # Querying again triggers statement cancellation if WAL replay conflicted
                    cur.execute("SELECT count(*) FROM accounts;")
                    conn.commit()
        except (errors.QueryCanceled, errors.SerializationFailure, psycopg.OperationalError) as exc:
            conflict_error = exc
        except Exception as exc:
            conflict_error = exc

    worker_thread = threading.Thread(target=standby_query_worker)
    worker_thread.start()

    # Wait until standby transaction holds its snapshot
    snapshot_held.wait()
    time.sleep(0.2)

    console.print("  [Primary] Concurrently deleting 10,000 rows and executing VACUUM accounts...")
    with psycopg.connect(PRIMARY_CONN, autocommit=True) as p_conn:
        with p_conn.cursor() as cur:
            cur.execute("DELETE FROM accounts;")
            plain_console.print("  [Primary] Rows deleted. Now running VACUUM accounts (pruning dead tuples)...")
            cur.execute("VACUUM accounts;")
            plain_console.print("  [Primary] VACUUM complete. Cleanup WAL emitted to standby.")

    worker_thread.join()

    if conflict_error is not None:
        err_msg = str(conflict_error).strip()
        console.print("\n  [bold red][!] RECOVERY CONFLICT OCCURRED AS HYPOTHESIZED:[/]")
        console.print(f"    Error Class : [bold yellow]{type(conflict_error).__name__}[/]")
        for line in err_msg.splitlines():
            plain_console.print(f"    {line}")

        verify_phase(
            "conflict with recovery" in err_msg.lower(),
            "Part 2: Recovery Conflict Signature",
            f"Expected 'conflict with recovery' error, got: {err_msg}",
        )
        plain_console.print("\n  [✓] Successfully reproduced standby query cancellation conflict (SQLSTATE 40001).")
    else:
        verify_phase(False, "Part 2: Recovery Conflict", "Standby query was NOT canceled as expected!")

    with psycopg.connect(STANDBY_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT confl_snapshot FROM pg_stat_database_conflicts WHERE datname = current_database();")
            conflicts_after = cur.fetchone()[0]
    verify_phase(
        conflicts_after > conflicts_before,
        "Part 2: Standby Conflict Telemetry",
        f"Expected pg_stat_database_conflicts.confl_snapshot to increment (before={conflicts_before}, after={conflicts_after})",
    )


# ----------------------------------------------------------------------
# Part 3: Remediation via hot_standby_feedback = on
# ----------------------------------------------------------------------
def test_remediation_hot_standby_feedback() -> None:
    console.print("\n[bold blue][*] Part 3: Remediation via hot_standby_feedback = on...[/]")
    set_standby_feedback(True)
    plain_console.print("  [>] hot_standby_feedback enabled on Standby (xmin advertised to Primary).")

    try:
        console.print("  [>] Re-seeding 10,000 accounts on Primary...")
        seed_accounts(10_000)

        snapshot_held = threading.Event()
        query_succeeded = False
        caught_error: Exception | None = None
        observed_counts: tuple[int, int] | None = None

        def standby_query_worker() -> None:
            nonlocal query_succeeded, caught_error, observed_counts
            try:
                with psycopg.connect(STANDBY_CONN, autocommit=False) as conn:
                    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                    with conn.cursor() as cur:
                        cur.execute("SELECT count(*) FROM accounts;")
                        initial_count = cur.fetchone()[0]
                        plain_console.print(f"  [Standby] Transaction opened (REPEATABLE READ). Initial count: {initial_count:,} rows.")
                        snapshot_held.set()

                        plain_console.print("  [Standby] Holding snapshot; sleeping 2.5s (exceeds max_standby_streaming_delay=1s)...")
                        time.sleep(2.5)

                        cur.execute("SELECT count(*) FROM accounts;")
                        second_count = cur.fetchone()[0]
                        conn.commit()
                        plain_console.print(f"  [Standby] Query completed successfully! Snapshot preserved {second_count:,} rows.")
                        observed_counts = (initial_count, second_count)
                        query_succeeded = True
            except Exception as exc:
                caught_error = exc

        worker_thread = threading.Thread(target=standby_query_worker)
        worker_thread.start()

        snapshot_held.wait()
        time.sleep(0.5)

        # Check that primary sees standby backend_xmin
        with psycopg.connect(PRIMARY_CONN, autocommit=True) as p_conn:
            with p_conn.cursor() as cur:
                cur.execute("SELECT backend_xmin FROM pg_stat_replication;")
                row = cur.fetchone()
                xmin = row[0] if row else None
                plain_console.print(f"  [Primary] Standby active backend_xmin recognized: {xmin}")
                verify_phase(xmin is not None, "Part 3: Feedback Horizon", "Primary did not observe standby backend_xmin")

        console.print("  [Primary] Concurrently deleting 10,000 rows and executing VACUUM accounts...")
        with psycopg.connect(PRIMARY_CONN, autocommit=True) as p_conn:
            with p_conn.cursor() as cur:
                cur.execute("DELETE FROM accounts;")
                plain_console.print("  [Primary] Rows deleted. Running VACUUM accounts...")
                cur.execute("VACUUM accounts;")
                plain_console.print("  [Primary] VACUUM deferred dead-tuple cleanup because standby xmin horizon is active.")

        worker_thread.join()

        verify_phase(
            query_succeeded and caught_error is None and observed_counts == (10_000, 10_000),
            "Part 3: Hot Standby Feedback Remediation",
            f"Expected standby query to preserve 10,000 rows without error; got success={query_succeeded}, counts={observed_counts}, err={caught_error}",
        )
        console.print("\n  [bold green][✓] REMEDIATION VERIFIED:[/]")
        plain_console.print("  hot_standby_feedback = on prevented the primary from vacuuming dead tuples,")
        plain_console.print("  eliminating the recovery conflict and allowing the standby analytical query to finish.")
    finally:
        # Reset feedback to off for clean lab state
        set_standby_feedback(False)


def main() -> None:
    try:
        with psycopg.connect(PRIMARY_CONN, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_is_in_recovery(), current_setting('wal_level');")
                in_rec, wal_lvl = cur.fetchone()
                verify_phase(
                    in_rec is False and wal_lvl == "replica",
                    "Phase 0: Primary Preconditions",
                    f"Expected primary (in_recovery=False, wal_level='replica'), got in_recovery={in_rec}, wal_level={wal_lvl!r}",
                )
    except SystemExit:
        raise
    except Exception:
        console.print("[bold red]Error: Cannot connect to PostgreSQL Primary on 127.0.0.1:5432/lab_db.[/]")
        plain_console.print("Please start the replication profile first:")
        plain_console.print("  cd postgres && make up EXP=replication")
        sys.exit(1)

    try:
        with psycopg.connect(STANDBY_CONN, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_is_in_recovery(), current_setting('max_standby_streaming_delay');")
                in_rec, delay = cur.fetchone()
                verify_phase(
                    in_rec is True and delay == "1s",
                    "Phase 0: Standby Preconditions",
                    f"Expected standby (in_recovery=True, max_standby_streaming_delay='1s'), got in_recovery={in_rec}, delay={delay!r}",
                )
    except SystemExit:
        raise
    except Exception:
        console.print("[bold red]Error: Cannot connect to PostgreSQL Standby on 127.0.0.1:5433/lab_db.[/]")
        plain_console.print("Please ensure both containers are healthy:")
        plain_console.print("  cd postgres && make up EXP=replication")
        sys.exit(1)

    console.print("=" * 68, style="bold cyan")
    console.print("[bold cyan]Experiment 04: Streaming Replication & Standby Query Conflicts[/]")
    plain_console.print("Stress-testing replication lag, recovery conflicts, and feedback remediation.")
    console.print("=" * 68, style="bold cyan")

    # Part 1: Replication status
    test_replication_status()

    # Part 2: Standby query cancellation
    test_standby_query_conflict()

    # Part 3: Remediation via hot_standby_feedback
    test_remediation_hot_standby_feedback()

    console.print("\n" + "=" * 68, style="bold green")
    plain_console.print("SYSTEMS TAKEAWAYS")
    console.print("=" * 68, style="bold green")
    plain_console.print("1. Physical Streaming Replication replicates page-level changes (WAL), not logical SQL.")
    plain_console.print("2. Standby Query Cancellation occurs when primary VACUUM removes dead row versions")
    plain_console.print("   that a long-running standby query still needs to see according to its MVCC snapshot.")
    plain_console.print("3. max_standby_streaming_delay (e.g. 1s) bounds how long WAL replay will pause before")
    plain_console.print("   terminating the conflicting user query with SQLSTATE 40001 (conflict with recovery).")
    plain_console.print("4. Remediation (hot_standby_feedback = on) sends the standby xmin horizon to the primary,")
    plain_console.print("   preventing VACUUM from pruning tuples needed by standby queries. Tradeoff: can cause")
    plain_console.print("   table and WAL bloat on the primary if standby queries run for excessive durations.")
    plain_console.print("====================================================================\n")


if __name__ == "__main__":
    main()

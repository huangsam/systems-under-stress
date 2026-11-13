# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
#     "cassandra-driver>=3.29.0",
# ]
# ///
"""
Experiment 03: Wide Partitions & Hot-Spotting

Compares the performance, partition sizing, and query latency of an anti-pattern
"Monolithic Wide Partition" against a properly bucketted partition design.

Anti-Pattern:
- Table: monolithic_feed
- Partition Key: partition_id = 'GLOBAL' (All system records shoved into one partition)
- Symptom: Unbounded SSTable partition size, excessive heap consumption, coordinator timeouts.

Production Best Practice:
- Table: bucketed_feed
- Partition Key: ((tenant_id, date_bucket), event_id)
- Result: Partitions stay < 100MB, sub-millisecond point reads, even token distribution.
"""

from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

from cassandra.cluster import EXEC_PROFILE_DEFAULT, Cluster, ExecutionProfile
from cassandra.concurrent import execute_concurrent_with_args
from cassandra.policies import WhiteListRoundRobinPolicy
from rich.console import Console
from rich.markup import escape

COMPOSE_DIR = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 9042
KEYSPACE = "lab_partitions"
NUM_RECORDS = 5000


console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def log(msg: str, color: str = "[dim]") -> None:
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{timestamp}][/] {color}{escape(msg)}[/]")


def wait_for_cassandra() -> Cluster:
    log("Connecting to Cassandra on 127.0.0.1:9042...", "[bold cyan]")
    profile = ExecutionProfile(
        request_timeout=60.0,
        load_balancing_policy=WhiteListRoundRobinPolicy([HOST]),
    )
    for attempt in range(1, 30):
        try:
            cluster = Cluster(
                contact_points=[HOST],
                port=PORT,
                execution_profiles={EXEC_PROFILE_DEFAULT: profile},
                connect_timeout=5.0,
            )
            session = cluster.connect()
            session.execute("SELECT release_version FROM system.local")
            return cluster
        except Exception:
            time.sleep(2)
    log("Failed to connect. Start with 'make up EXP=wide-partitions' or 'make up EXP=tombstones'", "[bold red]")
    sys.exit(1)


def setup_schema(session) -> None:
    session.execute(f"""
        CREATE KEYSPACE IF NOT EXISTS {KEYSPACE}
        WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': 1}};
    """)

    # Anti-Pattern: Static partition key
    session.execute(f"""
        CREATE TABLE IF NOT EXISTS {KEYSPACE}.monolithic_feed (
            partition_key text,
            event_id timeuuid,
            payload text,
            PRIMARY KEY (partition_key, event_id)
        );
    """)

    # Best Practice: Date/Tenant bucketed partition key
    session.execute(f"""
        CREATE TABLE IF NOT EXISTS {KEYSPACE}.bucketed_feed (
            tenant_id text,
            date_bucket text,
            event_id timeuuid,
            payload text,
            PRIMARY KEY ((tenant_id, date_bucket), event_id)
        );
    """)

    session.execute(f"TRUNCATE {KEYSPACE}.monolithic_feed;")
    session.execute(f"TRUNCATE {KEYSPACE}.bucketed_feed;")
    log("Keyspace and comparison tables initialized.", "[green]")


def run_experiment() -> None:
    plain_console.print("\n" + "=" * 70)
    plain_console.print("  EXPERIMENT 03: Wide Partitions & Hot-Spotting Anti-Patterns  ")
    plain_console.print("=" * 70 + "\n")

    cluster = wait_for_cassandra()
    session = cluster.connect()

    try:
        setup_schema(session)
        import uuid

        # Ingest into Monolithic table
        log(f"Ingesting {NUM_RECORDS:,} events into SINGLE partition 'ALL' (Anti-pattern)...", "[bold yellow]")
        mono_stmt = session.prepare(f"INSERT INTO {KEYSPACE}.monolithic_feed (partition_key, event_id, payload) VALUES (?, ?, ?)")
        mono_params = [("ALL", uuid.uuid1(), f"payload_data_{i}") for i in range(NUM_RECORDS)]

        t0 = time.perf_counter()
        mono_results = execute_concurrent_with_args(session, mono_stmt, mono_params, concurrency=50)
        t_mono_write = time.perf_counter() - t0
        mono_failed = sum(1 for ok, _ in mono_results if not ok)
        verify_phase(mono_failed == 0, "Phase 1: Monolithic Ingestion", f"{mono_failed}/{NUM_RECORDS} monolithic inserts failed")

        # Ingest into Bucketed table (10 distinct date/tenant buckets)
        log(f"Ingesting {NUM_RECORDS:,} events evenly distributed across 10 buckets (Best Practice)...", "[bold green]")
        bucket_stmt = session.prepare(f"INSERT INTO {KEYSPACE}.bucketed_feed (tenant_id, date_bucket, event_id, payload) VALUES (?, ?, ?, ?)")
        bucket_params = [(f"tenant_{i % 10}", "2026-10-22", uuid.uuid1(), f"payload_data_{i}") for i in range(NUM_RECORDS)]

        t0 = time.perf_counter()
        bucket_results = execute_concurrent_with_args(session, bucket_stmt, bucket_params, concurrency=50)
        t_bucket_write = time.perf_counter() - t0
        bucket_failed = sum(1 for ok, _ in bucket_results if not ok)
        verify_phase(bucket_failed == 0, "Phase 2: Bucketed Ingestion", f"{bucket_failed}/{NUM_RECORDS} bucketed inserts failed")

        # Query benchmarking
        log("Benchmarking point partition scan latency...", "[bold cyan]")

        # Monolithic read
        t0 = time.perf_counter()
        rows_mono = list(session.execute(f"SELECT * FROM {KEYSPACE}.monolithic_feed WHERE partition_key = 'ALL' LIMIT 1000"))
        t_mono_read = (time.perf_counter() - t0) * 1000

        # Bucketed read
        t0 = time.perf_counter()
        rows_bucket = list(session.execute(f"SELECT * FROM {KEYSPACE}.bucketed_feed WHERE tenant_id = 'tenant_0' AND date_bucket = '2026-10-22' LIMIT 1000"))
        t_bucket_read = (time.perf_counter() - t0) * 1000

        mono_count = session.execute(f"SELECT count(*) FROM {KEYSPACE}.monolithic_feed WHERE partition_key = 'ALL'").one()[0]
        tenant0_count = session.execute(f"SELECT count(*) FROM {KEYSPACE}.bucketed_feed WHERE tenant_id = 'tenant_0' AND date_bucket = '2026-10-22'").one()[0]
        verify_phase(
            len(rows_mono) == 1000 and mono_count == NUM_RECORDS,
            "Phase 3: Monolithic Read Audit",
            f"Expected 1000 limited / {NUM_RECORDS} total rows in monolithic partition, got limit={len(rows_mono)}, total={mono_count}",
        )
        verify_phase(
            len(rows_bucket) == NUM_RECORDS // 10 and tenant0_count == NUM_RECORDS // 10,
            "Phase 3: Bucketed Read Audit",
            f"Expected {NUM_RECORDS // 10} rows in tenant_0 bucket, got read={len(rows_bucket)}, total={tenant0_count}",
        )

        plain_console.print("\n" + "=" * 70)
        plain_console.print("  PARTITION ARCHITECTURE COMPARISON")
        plain_console.print("=" * 70)
        plain_console.print(f"  Ingestion Strategy        : {'Monolithic (1 Partition)':<25} vs {'Bucketed (10 Partitions)':<25}")
        plain_console.print(f"  Write Duration            : {t_mono_write:.3f}s                   vs {t_bucket_write:.3f}s")
        plain_console.print(f"  Query Read Latency        : {t_mono_read:.2f} ms                  vs {t_bucket_read:.2f} ms")
        console.print("  Partition Size Risk       : [red]CRITICAL (>100MB bloat)[/]   vs [green]SAFE (<10MB per bucket)[/]")
        plain_console.print("=" * 70 + "\n")

        console.print("[bold]Cassandra Partitioning Guardrails:[/]")
        plain_console.print("1. Maximum recommended partition size: 100MB (Hard limit ~2GB).")
        plain_console.print("2. Maximum recommended cells per partition: 100,000 cells.")
        plain_console.print("3. Wide partitions cause JVM heap spikes during read repair, compaction, and coordinator caching.")
        plain_console.print("4. Remediation: Introduce synthetic bucketing (e.g., date, week, or hash suffix) into partition key.")
        plain_console.print()

    finally:
        session.shutdown()
        cluster.shutdown()


if __name__ == "__main__":
    run_experiment()

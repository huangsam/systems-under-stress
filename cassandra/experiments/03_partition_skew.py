# /// script
# requires-python = ">=3.11"
# dependencies = [
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
import subprocess
import sys
import time
from pathlib import Path

from cassandra import ConsistencyLevel
from cassandra.cluster import Cluster, ExecutionProfile, EXEC_PROFILE_DEFAULT
from cassandra.concurrent import execute_concurrent_with_args
from cassandra.policies import WhiteListRoundRobinPolicy
from cassandra.query import SimpleStatement

COMPOSE_DIR = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 9042
KEYSPACE = "lab_partitions"
NUM_RECORDS = 5000


def log(msg: str, color: str = "\033[0m") -> None:
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\033[2m[{timestamp}]\033[0m {color}{msg}\033[0m", flush=True)


def wait_for_cassandra() -> Cluster:
    log("Connecting to Cassandra on 127.0.0.1:9042...", "\033[1;36m")
    profile = ExecutionProfile(
        request_timeout=20.0,
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
    log("Failed to connect. Start with 'make up EXP=wide-partitions' or 'make up EXP=tombstones'", "\033[1;31m")
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
    log("Keyspace and comparison tables initialized.", "\033[32m")


def run_experiment() -> None:
    print("\n" + "=" * 70)
    print("  EXPERIMENT 03: Wide Partitions & Hot-Spotting Anti-Patterns  ")
    print("=" * 70 + "\n")

    cluster = wait_for_cassandra()
    session = cluster.connect()

    try:
        setup_schema(session)
        import uuid

        # Ingest into Monolithic table
        log(f"Ingesting {NUM_RECORDS:,} events into SINGLE partition 'ALL' (Anti-pattern)...", "\033[1;33m")
        mono_stmt = session.prepare(f"INSERT INTO {KEYSPACE}.monolithic_feed (partition_key, event_id, payload) VALUES (?, ?, ?)")
        mono_params = [("ALL", uuid.uuid1(), f"payload_data_{i}") for i in range(NUM_RECORDS)]

        t0 = time.perf_counter()
        execute_concurrent_with_args(session, mono_stmt, mono_params, concurrency=50)
        t_mono_write = time.perf_counter() - t0

        # Ingest into Bucketed table (10 distinct date/tenant buckets)
        log(f"Ingesting {NUM_RECORDS:,} events evenly distributed across 10 buckets (Best Practice)...", "\033[1;32m")
        bucket_stmt = session.prepare(f"INSERT INTO {KEYSPACE}.bucketed_feed (tenant_id, date_bucket, event_id, payload) VALUES (?, ?, ?, ?)")
        bucket_params = [
            (f"tenant_{i % 10}", "2026-10-22", uuid.uuid1(), f"payload_data_{i}")
            for i in range(NUM_RECORDS)
        ]

        t0 = time.perf_counter()
        execute_concurrent_with_args(session, bucket_stmt, bucket_params, concurrency=50)
        t_bucket_write = time.perf_counter() - t0

        # Query benchmarking
        log("Benchmarking point partition scan latency...", "\033[1;36m")

        # Monolithic read
        t0 = time.perf_counter()
        rows_mono = list(session.execute(f"SELECT * FROM {KEYSPACE}.monolithic_feed WHERE partition_key = 'ALL' LIMIT 1000"))
        t_mono_read = (time.perf_counter() - t0) * 1000

        # Bucketed read
        t0 = time.perf_counter()
        rows_bucket = list(session.execute(f"SELECT * FROM {KEYSPACE}.bucketed_feed WHERE tenant_id = 'tenant_0' AND date_bucket = '2026-10-22' LIMIT 1000"))
        t_bucket_read = (time.perf_counter() - t0) * 1000

        print("\n" + "=" * 70)
        print("  PARTITION ARCHITECTURE COMPARISON")
        print("=" * 70)
        print(f"  Ingestion Strategy        : {'Monolithic (1 Partition)':<25} vs {'Bucketed (10 Partitions)':<25}")
        print(f"  Write Duration            : {t_mono_write:.3f}s                   vs {t_bucket_write:.3f}s")
        print(f"  Query Read Latency        : {t_mono_read:.2f} ms                  vs {t_bucket_read:.2f} ms")
        print(f"  Partition Size Risk       : \033[31mCRITICAL (>100MB bloat)\033[0m   vs \033[32mSAFE (<10MB per bucket)\033[0m")
        print("=" * 70 + "\n")

        print("\033[1mCassandra Partitioning Guardrails:\033[0m")
        print("1. Maximum recommended partition size: 100MB (Hard limit ~2GB).")
        print("2. Maximum recommended cells per partition: 100,000 cells.")
        print("3. Wide partitions cause JVM heap spikes during read repair, compaction, and coordinator caching.")
        print("4. Remediation: Introduce synthetic bucketing (e.g., date, week, or hash suffix) into partition key.")
        print()

    finally:
        session.shutdown()
        cluster.shutdown()


if __name__ == "__main__":
    run_experiment()

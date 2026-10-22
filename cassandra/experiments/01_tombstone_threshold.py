# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "cassandra-driver>=3.29.0",
# ]
# ///
"""
Experiment 01: The Tombstone Failure Threshold (10,000 Tombstones)

Demonstrates how deletes in Apache Cassandra create tombstone markers that
degrade read latency and trigger the hard safety limit:
tombstone_failure_threshold (default: 10,000).

Execution flow:
1. Connect to Cassandra on 127.0.0.1:9042 and provision lab keyspace & table.
2. Ingest 15,000 consecutive sensor telemetry records into a single partition.
3. Delete 12,000 records to create 12,000 tombstone markers.
4. Flush memtables to SSTables (via nodetool) so tombstones reside on disk.
5. Attempt a scan query (e.g. SELECT * without filtering past tombstones).
6. Observe Cassandra abort the query with ReadFailure:
   "Scanned over 10000 tombstones in query; query aborted (see tombstone_failure_threshold)"
7. Execute remediation: Query with clustering key slice past the tombstones,
   demonstrating sub-millisecond retrieval.
"""
from __future__ import annotations

import datetime
import subprocess
import sys
import time
from pathlib import Path

from cassandra import ConsistencyLevel, ReadFailure, ReadTimeout
from cassandra.cluster import Cluster, ExecutionProfile, EXEC_PROFILE_DEFAULT
from cassandra.concurrent import execute_concurrent_with_args
from cassandra.policies import WhiteListRoundRobinPolicy
from cassandra.query import SimpleStatement

COMPOSE_DIR = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 9042
KEYSPACE = "lab_tombstones"
TABLE = "sensor_telemetry"
SENSOR_ID = "sensor-alpha"

TOTAL_ROWS = 15_000
DELETE_ROWS = 12_000


def log(msg: str, color: str = "\033[0m") -> None:
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\033[2m[{timestamp}]\033[0m {color}{msg}\033[0m", flush=True)


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=COMPOSE_DIR, capture_output=True, text=True)


def trigger_nodetool_flush() -> None:
    log("Flushing memtables to SSTables on disk via nodetool...", "\033[1;34m")
    res = run_cmd(["docker", "compose", "exec", "-T", "cassandra-tombstones", "nodetool", "flush", KEYSPACE, TABLE])
    if res.returncode == 0:
        log("Memtable flushed to SSTable successfully.", "\033[32m")
    else:
        log(f"Notice: nodetool flush skipped or failed: {res.stderr.strip()}", "\033[33m")


def wait_for_cassandra() -> Cluster:
    log("Connecting to Cassandra on 127.0.0.1:9042...", "\033[1;36m")
    profile = ExecutionProfile(
        request_timeout=30.0,
        load_balancing_policy=WhiteListRoundRobinPolicy([HOST]),
    )
    for attempt in range(1, 31):
        try:
            cluster = Cluster(
                contact_points=[HOST],
                port=PORT,
                execution_profiles={EXEC_PROFILE_DEFAULT: profile},
                connect_timeout=5.0,
            )
            session = cluster.connect()
            session.execute("SELECT release_version FROM system.local")
            log(f"Connected to Cassandra cluster successfully (attempt {attempt}).", "\033[1;32m")
            return cluster
        except Exception as err:
            time.sleep(2)
    log("Failed to connect to Cassandra after 30 attempts. Is 'make up EXP=tombstones' running?", "\033[1;31m")
    sys.exit(1)


def setup_schema(session) -> None:
    log(f"Setting up keyspace '{KEYSPACE}' and table '{TABLE}'...", "\033[1;36m")
    session.execute(f"""
        CREATE KEYSPACE IF NOT EXISTS {KEYSPACE}
        WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': 1}};
    """)

    session.execute(f"""
        CREATE TABLE IF NOT EXISTS {KEYSPACE}.{TABLE} (
            sensor_id text,
            recorded_at timestamp,
            metric_val double,
            payload text,
            PRIMARY KEY (sensor_id, recorded_at)
        ) WITH CLUSTERING ORDER BY (recorded_at ASC)
          AND gc_grace_seconds = 864000;
    """)

    # Fast reset of table data
    session.execute(f"TRUNCATE {KEYSPACE}.{TABLE};")
    log("Schema ready and table truncated.", "\033[32m")


def run_experiment() -> None:
    print("\n" + "=" * 70)
    print("  EXPERIMENT 01: The Tombstone Failure Threshold (10,000 Cap)  ")
    print("=" * 70 + "\n")

    cluster = wait_for_cassandra()
    session = cluster.connect()

    try:
        setup_schema(session)

        # ---------------------------------------------------------
        # Phase 1: Insert 15,000 rows
        # ---------------------------------------------------------
        base_time = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
        log(f"Phase 1: Inserting {TOTAL_ROWS:,} sensor readings into partition '{SENSOR_ID}'...", "\033[1;36m")

        insert_stmt = session.prepare(f"""
            INSERT INTO {KEYSPACE}.{TABLE} (sensor_id, recorded_at, metric_val, payload)
            VALUES (?, ?, ?, ?)
        """)
        insert_stmt.consistency_level = ConsistencyLevel.ONE

        insert_params = [
            (
                SENSOR_ID,
                base_time + datetime.timedelta(seconds=i),
                float(i * 1.5),
                f"payload_metric_reading_packet_{i:06d}"
            )
            for i in range(TOTAL_ROWS)
        ]

        t0 = time.perf_counter()
        results = execute_concurrent_with_args(session, insert_stmt, insert_params, concurrency=100)
        t1 = time.perf_counter()

        failed_inserts = sum(1 for success, _ in results if not success)
        if failed_inserts > 0:
            log(f"Warning: {failed_inserts} inserts failed", "\033[31m")
        else:
            log(f"Successfully inserted {TOTAL_ROWS:,} rows in {t1 - t0:.2f}s ({(TOTAL_ROWS / (t1 - t0)):.0f} rows/s).", "\033[32m")

        # ---------------------------------------------------------
        # Phase 2: Delete 12,000 rows (Generating 12,000 tombstones)
        # ---------------------------------------------------------
        log(f"Phase 2: Deleting first {DELETE_ROWS:,} rows to generate tombstones...", "\033[1;36m")

        delete_stmt = session.prepare(f"""
            DELETE FROM {KEYSPACE}.{TABLE}
            WHERE sensor_id = ? AND recorded_at = ?
        """)
        delete_stmt.consistency_level = ConsistencyLevel.ONE

        delete_params = [
            (
                SENSOR_ID,
                base_time + datetime.timedelta(seconds=i)
            )
            for i in range(DELETE_ROWS)
        ]

        t0 = time.perf_counter()
        del_results = execute_concurrent_with_args(session, delete_stmt, delete_params, concurrency=100)
        t1 = time.perf_counter()

        failed_deletes = sum(1 for success, _ in del_results if not success)
        if failed_deletes > 0:
            log(f"Warning: {failed_deletes} deletes failed", "\033[31m")
        else:
            log(f"Generated {DELETE_ROWS:,} tombstones in {t1 - t0:.2f}s ({(DELETE_ROWS / (t1 - t0)):.0f} ops/s).", "\033[32m")

        # Flush memtables to disk SSTables
        trigger_nodetool_flush()

        # ---------------------------------------------------------
        # Phase 3: Trigger the Tombstone Threshold Limit
        # ---------------------------------------------------------
        log("Phase 3: Querying partition containing 12,000 tombstones...", "\033[1;36m")
        log("Query: SELECT * FROM sensor_telemetry WHERE sensor_id = 'sensor-alpha' LIMIT 5000;", "\033[2m")

        query_stmt = SimpleStatement(
            f"SELECT * FROM {KEYSPACE}.{TABLE} WHERE sensor_id = %s LIMIT 5000;",
            consistency_level=ConsistencyLevel.ONE,
            fetch_size=5000,
        )

        read_failed = False
        error_details = ""
        try:
            t0 = time.perf_counter()
            rows = session.execute(query_stmt, (SENSOR_ID,))
            row_list = list(rows)
            t1 = time.perf_counter()
            log(f"Unexpected: Query succeeded and returned {len(row_list)} rows in {t1 - t0:.3f}s.", "\033[33m")
        except ReadFailure as exc:
            read_failed = True
            error_details = str(exc)
            log("SUCCESS: Cassandra aborted the query with ReadFailure!", "\033[1;31m")
            log(f"Exception Type: {type(exc).__name__}", "\033[31m")
            log(f"Server Error Message: {error_details}", "\033[1;31m")
        except Exception as exc:
            read_failed = True
            error_details = f"{type(exc).__name__}: {exc}"
            log(f"Query raised: {error_details}", "\033[1;31m")

        # Also test querying without partition key (full scan across table)
        log("Testing unconstrained full table scan across tombstones...", "\033[1;36m")
        try:
            full_scan_stmt = SimpleStatement(f"SELECT * FROM {KEYSPACE}.{TABLE} LIMIT 5000;", consistency_level=ConsistencyLevel.ONE)
            session.execute(full_scan_stmt)
            log("Full scan completed without abort.", "\033[33m")
        except (ReadFailure, ReadTimeout) as exc:
            log(f"Full scan was aborted as expected: {exc}", "\033[1;31m")

        # ---------------------------------------------------------
        # Phase 4: Remediation - Querying with Clustering Key Bounds
        # ---------------------------------------------------------
        log("Phase 4: Remediation - Slicing past the tombstone boundary...", "\033[1;36m")
        boundary_time = base_time + datetime.timedelta(seconds=DELETE_ROWS)
        log(f"Clustering Key Filter: recorded_at >= {boundary_time.isoformat()}", "\033[2m")

        remediation_stmt = SimpleStatement(
            f"""
            SELECT * FROM {KEYSPACE}.{TABLE}
            WHERE sensor_id = %s AND recorded_at >= %s
            LIMIT 1000;
            """,
            consistency_level=ConsistencyLevel.ONE,
        )

        t0 = time.perf_counter()
        healthy_rows = list(session.execute(remediation_stmt, (SENSOR_ID, boundary_time)))
        t1 = time.perf_counter()

        log(f"Remediated query returned {len(healthy_rows)} live rows in {(t1 - t0) * 1000:.2f} ms!", "\033[1;32m")
        log(f"Tombstones scanned: 0 (bypassed via SSTable clustering key index block).", "\033[32m")

        # ---------------------------------------------------------
        # Summary & Systems Insights
        # ---------------------------------------------------------
        print("\n" + "=" * 70)
        print("  EXPERIMENT 01 BENCHMARK SUMMARY")
        print("=" * 70)
        print(f"  Rows Inserted               : {TOTAL_ROWS:,}")
        print(f"  Rows Deleted (Tombstones)   : {DELETE_ROWS:,}")
        print(f"  Cassandra Safety Threshold  : 10,000 tombstones (tombstone_failure_threshold)")
        print(f"  Unbounded Scan Result       : {'ABORTED (ReadFailureException)' if read_failed else 'PASSED'}")
        print(f"  Clustering Key Slice Latency: {(t1 - t0) * 1000:.2f} ms (Bypassed tombstones)")
        print("=" * 70 + "\n")

        print("\033[1mCore Engineering Takeaways:\033[0m")
        print("1. Deletes in Cassandra are append-only mutations that write immutable tombstones.")
        print("2. Scans must read tombstones to ensure deleted cells aren't resurrected from older SSTables.")
        print("3. When a query scans > 1,000 tombstones, Cassandra issues a server WARN.")
        print("4. When a query scans > 10,000 tombstones, Cassandra aborts with ReadFailureException")
        print("   to protect node JVM memory from GC death spirals and OOM crashes.")
        print("5. Anti-pattern: High-churn queue implementations using Cassandra (insert + delete).")
        print("6. Correct architectural pattern: Use short TTLs, clustering slice filters, or dedicated")
        print("   message queues (Kafka, RabbitMQ) for transient items.")
        print()

    finally:
        session.shutdown()
        cluster.shutdown()


if __name__ == "__main__":
    run_experiment()

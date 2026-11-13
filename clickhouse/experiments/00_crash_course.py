# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "clickhouse-connect>=0.7.0",
# ]
# ///
"""
ClickHouse Crash Course: Columnar Storage, Vectorized Aggregation & Parts Mechanics

Demonstrates:
1. HTTP connectivity & engine version check.
2. MergeTree table creation with primary key ordering.
3. Columnar batch insertion (block write).
4. Vectorized analytical aggregations (group by, quantiles, rates).
5. Storage introspection (system.parts, active part count, compression ratios).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import clickhouse_connect
from rich.console import Console
from rich.markup import escape

HOST = "127.0.0.1"
PORT = 8123
TABLE_NAME = "crash_course_telemetry"


console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def main() -> None:
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("[bold green]ClickHouse Crash Course: Columnar Engine & Vectorized Analytics[/]")
    console.print("=" * 70 + "\n", style="bold cyan")

    try:
        client = clickhouse_connect.get_client(host=HOST, port=PORT, username="default", password="")
    except Exception as e:
        console.print(f"[bold red][!] Connection failed: {escape(str(e))}[/]")
        plain_console.print("Start the lab container first: cd clickhouse && make up EXP=parts-limit")
        sys.exit(1)

    # 1. Connectivity & Version
    console.print("[bold blue][*] 1. Server Version & Connectivity[/]")
    version = client.command("SELECT version()")
    verify_phase(bool(version), "Phase 1: Connectivity", "Empty ClickHouse version string")
    plain_console.print(f"    ClickHouse Version: {version}")

    # 2. Table Creation
    console.print("\n[bold blue][*] 2. Creating MergeTree Engine Table[/]")
    client.command(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    client.command(f"""
        CREATE TABLE {TABLE_NAME} (
            timestamp DateTime CODEC(DoubleDelta, ZSTD(1)),
            device_id LowCardinality(String),
            metric_type LowCardinality(String),
            value Float64 CODEC(Gorilla, ZSTD(1))
        ) ENGINE = MergeTree()
        ORDER BY (metric_type, device_id, timestamp);
    """)
    exists = client.command(f"EXISTS TABLE {TABLE_NAME}")
    verify_phase(exists == 1, "Phase 2: Table DDL", f"Expected EXISTS TABLE=1, got {exists}")
    plain_console.print(f"    Created table '{TABLE_NAME}' with specialized codecs (DoubleDelta, Gorilla, LowCardinality).")

    # 3. Columnar Batch Insert
    console.print("\n[bold blue][*] 3. Columnar Batch Insert (Block Append)[/]")
    base_time = int(datetime.now(UTC).timestamp())
    column_data = []
    devices = ["sensor-alpha", "sensor-beta", "sensor-gamma"]
    metrics = ["temperature", "humidity", "voltage"]

    for i in range(10_000):
        dev = devices[i % len(devices)]
        met = metrics[(i // 3) % len(metrics)]
        val = 20.0 + (i % 100) * 0.1
        column_data.append([base_time + (i // 10), dev, met, val])

    client.insert(
        TABLE_NAME,
        column_data,
        column_names=["timestamp", "device_id", "metric_type", "value"],
    )
    row_count = client.command(f"SELECT count() FROM {TABLE_NAME}")
    verify_phase(row_count == 10_000, "Phase 3: Batch Insert", f"Expected 10,000 rows, got {row_count}")
    plain_console.print(f"    Appended {len(column_data):,} rows in a single compressed columnar block.")

    # 4. Vectorized Analytical Aggregation
    console.print("\n[bold blue][*] 4. Vectorized Analytical Aggregations[/]")
    query = f"""
        SELECT
            metric_type,
            count() AS count,
            round(min(value), 2) AS min_val,
            round(avg(value), 2) AS avg_val,
            round(max(value), 2) AS max_val,
            round(quantile(0.95)(value), 2) AS p95_val
        FROM {TABLE_NAME}
        GROUP BY metric_type
        ORDER BY metric_type;
    """
    results = client.query(query)
    verify_phase(
        len(results.result_rows) == 3 and sum(r[1] for r in results.result_rows) == 10_000,
        "Phase 4: Aggregation",
        f"Expected 3 metric groups totaling 10,000 rows, got {results.result_rows}",
    )
    plain_console.print("    Aggregated metrics across 10,000 measurements:")
    for row in results.result_rows:
        plain_console.print(f"      - {row[0]}: count={row[1]}, min={row[2]}, avg={row[3]}, max={row[4]}, p95={row[5]}")

    # 5. Diagnostics: system.parts
    console.print("\n[bold blue][*] 5. Storage Introspection (system.parts)[/]")
    parts_query = f"""
        SELECT
            name,
            part_type,
            rows,
            formatReadableSize(data_compressed_bytes) AS compressed,
            formatReadableSize(data_uncompressed_bytes) AS uncompressed
        FROM system.parts
        WHERE table = '{TABLE_NAME}' AND active = 1;
    """
    parts = client.query(parts_query)
    verify_phase(
        len(parts.result_rows) >= 1 and sum(p[2] for p in parts.result_rows) == 10_000,
        "Phase 5: Storage Parts",
        f"Expected active parts covering 10,000 rows, got {parts.result_rows}",
    )
    for p in parts.result_rows:
        plain_console.print(f"    Active Part '{p[0]}': type={p[1]}, rows={p[2]}, compressed={p[3]}, uncompressed={p[4]}")

    # Cleanup
    client.command(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    verify_phase(client.command(f"EXISTS TABLE {TABLE_NAME}") == 0, "Phase 5: Cleanup", f"Table '{TABLE_NAME}' still exists after DROP")
    plain_console.print(f"    Cleaned up demo table '{TABLE_NAME}'.")

    console.print("\n[bold green][✓] ClickHouse Crash Course completed successfully![/]\n")


if __name__ == "__main__":
    main()

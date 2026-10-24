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

HOST = "127.0.0.1"
PORT = 8123
TABLE_NAME = "crash_course_telemetry"


console = Console()


def main() -> None:
    console.print("\n[bold cyan]" + "=" * 70 + "[/]")
    console.print("[bold green]ClickHouse Crash Course: Columnar Engine & Vectorized Analytics[/]")
    console.print("[bold cyan]" + "=" * 70 + "[/]\n")

    try:
        client = clickhouse_connect.get_client(host=HOST, port=PORT, username="default", password="")
    except Exception as e:
        console.print(f"[bold red][!] Connection failed: {e}[/]")
        print("Start the lab container first: cd clickhouse && make up EXP=parts-limit")
        sys.exit(1)

    # 1. Connectivity & Version
    console.print("[bold blue][*] 1. Server Version & Connectivity[/]")
    version = client.command("SELECT version()")
    print(f"    ClickHouse Version: {version}")

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
    print(f"    Created table '{TABLE_NAME}' with specialized codecs (DoubleDelta, Gorilla, LowCardinality).")

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
    print(f"    Appended {len(column_data):,} rows in a single compressed columnar block.")

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
    print("    Aggregated metrics across 10,000 measurements:")
    for row in results.result_rows:
        print(f"      - {row[0]}: count={row[1]}, min={row[2]}, avg={row[3]}, max={row[4]}, p95={row[5]}")

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
    for p in parts.result_rows:
        print(f"    Active Part '{p[0]}': type={p[1]}, rows={p[2]}, compressed={p[3]}, uncompressed={p[4]}")

    # Cleanup
    client.command(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    print(f"    Cleaned up demo table '{TABLE_NAME}'.")

    console.print("\n[bold green][✓] ClickHouse Crash Course completed successfully![/]\n")


if __name__ == "__main__":
    main()

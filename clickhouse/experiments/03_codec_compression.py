# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "clickhouse-connect>=0.7.0",
# ]
# ///
"""
Experiment 03: Columnar Compression Codecs & LowCardinality Benchmarking

Demonstrates:
1. Why columnar storage achieves 10x-20x disk compression over row-oriented stores.
2. Comparing disk footprint across 500,000 telemetry events:
   - Uncompressed: CODEC(NONE) on raw String and DateTime.
   - Default LZ4: Standard ClickHouse block compression.
   - Specialized Codecs: CODEC(Delta, ZSTD(3)) on monotonic timestamps,
     CODEC(Gorilla, ZSTD) on gauges, and LowCardinality(String) dictionary encoding.
3. Inspecting exact byte footprints via 'system.columns'.
"""

from __future__ import annotations

import sys
import time

import clickhouse_connect

NUM_ROWS = 500_000


def format_bytes(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} TB"


def main() -> None:
    try:
        client = clickhouse_connect.get_client(host="127.0.0.1", port=8123)
    except Exception as e:
        print(f"\033[1;31mError: Could not connect to ClickHouse on 127.0.0.1:8123 ({e})\033[0m")
        print("Start the container first: make up EXP=compression")
        sys.exit(1)

    print("\033[1;36m======================================================================\033[0m")
    print("\033[1;36mExperiment 03: ClickHouse Columnar Codecs & LowCardinality Compression\033[0m")
    print(f"Benchmarking {NUM_ROWS:,} rows across Uncompressed, LZ4, and Delta+ZSTD.")
    print("\033[1;36m======================================================================\033[0m\n")

    # 1. Create three tables with identical logical schemas but different physical encodings
    client.command("DROP TABLE IF EXISTS telemetry_none")
    client.command("DROP TABLE IF EXISTS telemetry_lz4")
    client.command("DROP TABLE IF EXISTS telemetry_optimized")

    # Table A: Uncompressed (CODEC(NONE))
    client.command("""
    CREATE TABLE telemetry_none (
        ts DateTime CODEC(NONE),
        region String CODEC(NONE),
        status_code String CODEC(NONE),
        cpu_usage Float64 CODEC(NONE),
        request_bytes UInt64 CODEC(NONE)
    ) ENGINE = MergeTree()
    ORDER BY (region, ts)
    """)

    # Table B: Default ClickHouse (LZ4)
    client.command("""
    CREATE TABLE telemetry_lz4 (
        ts DateTime CODEC(LZ4),
        region String CODEC(LZ4),
        status_code String CODEC(LZ4),
        cpu_usage Float64 CODEC(LZ4),
        request_bytes UInt64 CODEC(LZ4)
    ) ENGINE = MergeTree()
    ORDER BY (region, ts)
    """)

    # Table C: Domain-Optimized Codecs (Delta+ZSTD, LowCardinality, Gorilla)
    client.command("""
    CREATE TABLE telemetry_optimized (
        ts DateTime CODEC(Delta(4), ZSTD(3)),
        region LowCardinality(String) CODEC(ZSTD(3)),
        status_code LowCardinality(String) CODEC(ZSTD(3)),
        cpu_usage Float64 CODEC(Gorilla, ZSTD(3)),
        request_bytes UInt64 CODEC(T64, ZSTD(3))
    ) ENGINE = MergeTree()
    ORDER BY (region, ts)
    """)

    # 2. Populate all three tables with the exact same 500,000 synthetic rows
    seed_sql = f"""
    SELECT
        toDateTime('2026-01-01 00:00:00') + number AS ts,
        arrayElement(['us-east-1', 'us-west-2', 'eu-central-1', 'ap-northeast-1'], (number % 4) + 1) AS region,
        arrayElement(['200_OK', '201_CREATED', '400_BAD_REQUEST', '500_INTERNAL_ERROR'], (number % 4) + 1) AS status_code,
        50.0 + (number % 100) * 0.05 AS cpu_usage,
        1024 + (number % 50) * 64 AS request_bytes
    FROM numbers({NUM_ROWS})
    """

    for tbl in ["telemetry_none", "telemetry_lz4", "telemetry_optimized"]:
        print(f"[*] Inserting {NUM_ROWS:,} rows into '{tbl}' and compacting parts...")
        t0 = time.perf_counter()
        client.command(f"INSERT INTO {tbl} {seed_sql}")
        client.command(f"OPTIMIZE TABLE {tbl} FINAL")
        print(f"    Completed in {(time.perf_counter() - t0) * 1000:.0f}ms")

    # 3. Query system.columns for exact physical vs uncompressed disk sizes
    print("\n\033[1;34m[*] Auditing Disk Storage via system.columns:\033[0m\n")
    rows = client.query("""
    SELECT
        table,
        sum(data_uncompressed_bytes) AS raw_bytes,
        sum(data_compressed_bytes) AS disk_bytes,
        round(sum(data_uncompressed_bytes) / sum(data_compressed_bytes), 2) AS ratio
    FROM system.columns
    WHERE database = currentDatabase()
      AND table IN ('telemetry_none', 'telemetry_lz4', 'telemetry_optimized')
    GROUP BY table
    ORDER BY disk_bytes DESC
    """).result_rows

    header = f"{'Table Variant':<24} | {'Raw Uncompressed':>16} | {'On-Disk Compressed':>18} | {'Compression Ratio':>18}"
    print(header)
    print("-" * len(header))
    for tbl, raw_b, disk_b, ratio in rows:
        print(f"{tbl:<24} | {format_bytes(raw_b):>16} | {format_bytes(disk_b):>18} | {ratio:>17.2f}x")

    # 4. Per-column breakdown on telemetry_optimized vs telemetry_none
    print("\n\033[1;34m[*] Per-Column Compression Breakdown (Optimized vs Uncompressed):\033[0m\n")
    col_rows = client.query("""
    SELECT
        o.name AS column_name,
        o.type AS optimized_type,
        o.compression_codec AS codec,
        n.data_compressed_bytes AS none_bytes,
        o.data_compressed_bytes AS opt_bytes,
        round(n.data_compressed_bytes / o.data_compressed_bytes, 1) AS savings_factor
    FROM system.columns AS o
    JOIN system.columns AS n
      ON o.name = n.name AND n.table = 'telemetry_none'
    WHERE o.table = 'telemetry_optimized'
    ORDER BY savings_factor DESC
    """).result_rows

    col_header = f"{'Column':<15} | {'Type':<24} | {'Codec':<24} | {'None Size':>11} | {'Opt Size':>11} | {'Savings':>9}"
    print(col_header)
    print("-" * len(col_header))
    for name, ctype, codec, n_b, o_b, factor in col_rows:
        print(f"{name:<15} | {ctype:<24} | {codec:<24} | {format_bytes(n_b):>11} | {format_bytes(o_b):>11} | {factor:>8.1f}x")

    print("\n\033[1;32m======================================================================")
    print("SYSTEMS TAKEAWAYS")
    print("======================================================================\033[0m")
    print("1. Delta + ZSTD on monotonic timestamps stores only frame-to-frame differences")
    print("   (e.g. +1, +1, +1), collapsing megabytes of timestamps into a few kilobytes.")
    print("2. LowCardinality(String) replaces repeated strings with integer dictionary IDs,")
    print("   reducing both disk I/O and GROUP BY hash-table memory by 10x+.")
    print("3. Less disk size = fewer IOPS = more data resident in the Linux page cache.")
    print("======================================================================\n")


if __name__ == "__main__":
    main()

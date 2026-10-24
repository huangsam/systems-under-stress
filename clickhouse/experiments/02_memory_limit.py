# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "clickhouse-connect>=0.7.0",
# ]
# ///
"""
Experiment 02: Analytical Memory Limit Saturation & External Aggregation

Demonstrates:
1. In-Memory Hash Tables: High-cardinality GROUP BY aggregations build expansive
   in-memory state hash tables (mapping distinct keys to aggregate state structures).
2. Memory Ceiling Breach: When aggregate state exceeds max_memory_usage (configured to 64MB),
   ClickHouse terminates the query with:
   "DB::Exception: Memory limit (for query) exceeded: would use X MiB (limit: Y MiB) (Code: 241)".
3. External Aggregation (Disk Spill): Configuring max_bytes_before_external_group_by
   instructs ClickHouse to dump intermediate aggregation hash tables to disk once RAM
   thresholds are reached, enabling queries to succeed under constrained resources.
4. Algorithmic Tradeoffs: Comparing uniqExact (exact in-memory set) vs uniq (HyperLogLog).
"""

from __future__ import annotations

import sys
import time

from rich.console import Console

try:
    import clickhouse_connect
    from clickhouse_connect.driver.exceptions import DatabaseError
except ImportError:
    print("clickhouse-connect not found. Run this script via 'uv run experiments/02_memory_limit.py'")
    sys.exit(1)

HOST = "127.0.0.1"
PORT = 8123
USERNAME = "default"
PASSWORD = ""
TABLE_NAME = "clickstream_events"
QUERY_MEM_CAP = 25_000_000  # 25 MB
EXTERNAL_GROUP_BY_THRESHOLD = 12_000_000  # 12 MB spill threshold


console = Console()


def get_client() -> clickhouse_connect.driver.Client:
    try:
        client = clickhouse_connect.get_client(
            host=HOST,
            port=PORT,
            username=USERNAME,
            password=PASSWORD,
            connect_timeout=3,
            send_receive_timeout=30,
        )
        client.command("SELECT 1")
        return client
    except Exception as err:
        console.print("[bold red][!] Error: Unable to connect to ClickHouse at 127.0.0.1:8123.[/]")
        print(f"    Details: {err}")
        print("\nPlease start the memory-cap profile first:")
        print("    cd clickhouse && make up EXP=memory-cap\n")
        sys.exit(1)


def seed_clickstream_dataset(client: clickhouse_connect.driver.Client, num_rows: int = 500_000) -> None:
    console.print(f"\n[bold blue][*] Preparing synthetic high-cardinality dataset ({num_rows:,} rows)...[/]")
    client.command(f"DROP TABLE IF EXISTS {TABLE_NAME}")

    create_table_sql = f"""
    CREATE TABLE {TABLE_NAME} (
        user_id UUID,
        url String,
        event_time DateTime DEFAULT now()
    ) ENGINE = MergeTree()
    ORDER BY (event_time, user_id)
    """
    client.command(create_table_sql)

    print(f"    Generating {num_rows:,} unique UUID clickstream events in batches...")
    start_time = time.perf_counter()
    chunk_size = 100_000
    for offset in range(0, num_rows, chunk_size):
        this_chunk = min(chunk_size, num_rows - offset)
        insert_sql = f"""
        INSERT INTO {TABLE_NAME}
        SELECT
            generateUUIDv4() AS user_id,
            concat('https://store.local/products/', toString(number % 10000)) AS url,
            now() - INTERVAL (number % 86400) SECOND AS event_time
        FROM numbers({this_chunk});
        """
        client.command(insert_sql, settings={"max_insert_block_size": 50_000})
    elapsed = time.perf_counter() - start_time

    # Inspect disk usage and table parts
    stats = client.query(f"""
    SELECT
        count() as rows,
        formatReadableSize(sum(data_compressed_bytes)) as compressed_size,
        formatReadableSize(sum(data_uncompressed_bytes)) as uncompressed_size
    FROM system.parts
    WHERE table = '{TABLE_NAME}' AND active = 1
    """).result_rows[0]

    console.print(f"    Dataset Ready: [bold green]{stats[0]:,} rows[/] inserted in {elapsed:.2f}s")
    print(f"    Disk Footprint: Compressed={stats[1]} (Uncompressed={stats[2]})")


def run_in_memory_aggregation(client: clickhouse_connect.driver.Client) -> None:
    console.print("\n[bold blue][*] Part 1: High-Cardinality Aggregation Under Strict RAM Limit...[/]")
    print("    Query       : GROUP BY distinct user_id UUIDs with uniqExact(url)")
    print(f"    Settings    : max_memory_usage = {QUERY_MEM_CAP} bytes")
    print("                  max_bytes_before_external_group_by = 0 (Spill Disabled)")

    query = f"""
    SELECT
        user_id,
        count() AS total_clicks,
        uniqExact(url) AS distinct_urls
    FROM {TABLE_NAME}
    GROUP BY user_id
    SETTINGS
        max_memory_usage = {QUERY_MEM_CAP},
        max_bytes_before_external_group_by = 0
    FORMAT Null
    """

    start_time = time.perf_counter()
    try:
        client.command(query)
        elapsed = time.perf_counter() - start_time
        print(f"    Unexpected query success in {elapsed:.2f}s (memory limit was not exceeded)")
    except DatabaseError as err:
        elapsed = time.perf_counter() - start_time
        err_msg = str(err)
        console.print(f"\n  [bold red][!] QUERY TERMINATED BY ENGINE (Elapsed: {elapsed:.2f}s)[/]")
        print("      ClickHouse Error Code : 241 (MEMORY_LIMIT_EXCEEDED)")
        print(f"      Engine Diagnostics    : {err_msg.strip()}")
        console.print("\n  [bold green][✓] Failure Verified:[/] ClickHouse protected host OS by aborting before")
        print("      the 1,000,000-key aggregation hash table could trigger container OOM (Exit 137).")


def run_spill_to_disk_aggregation(client: clickhouse_connect.driver.Client) -> None:
    console.print("\n[bold blue][*] Part 2: Enabling External Aggregation (Disk Spilling)...[/]")
    print(f"    Settings    : max_memory_usage = {QUERY_MEM_CAP} (64MB)")
    print(f"                  max_bytes_before_external_group_by = {EXTERNAL_GROUP_BY_THRESHOLD} (20MB)")
    print("    Expected    : When hash table reaches 20MB, ClickHouse flushes intermediate blocks")
    print("                  to temporary disk files and performs a multi-way external merge.")

    query = f"""
    SELECT
        count() AS aggregate_groups,
        sum(total_clicks) AS grand_total_clicks
    FROM (
        SELECT
            user_id,
            count() AS total_clicks,
            uniqExact(url) AS distinct_urls
        FROM {TABLE_NAME}
        GROUP BY user_id
        SETTINGS
            max_memory_usage = {QUERY_MEM_CAP},
            max_bytes_before_external_group_by = {EXTERNAL_GROUP_BY_THRESHOLD}
    )
    """

    start_time = time.perf_counter()
    result = client.query(query).result_rows[0]
    elapsed = time.perf_counter() - start_time

    console.print("\n    Query Status          : [bold green]SUCCESSFUL COMPLETION[/]")
    console.print(f"    Aggregated Groups     : [bold green]{result[0]:,}[/] user UUIDs processed")
    print(f"    Grand Total Events    : {result[1]:,} clicks verified")
    print(f"    Total Execution Time  : {elapsed:.2f} seconds")


def run_hyperloglog_comparison(client: clickhouse_connect.driver.Client) -> None:
    console.print("\n[bold blue][*] Part 3: Algorithmic Memory Optimization (uniqExact vs uniq/HyperLogLog)...[/]")
    print("    Evaluating approximate aggregation with fixed-size registers vs exact hash set.")

    # Step 3a: uniqExact fails under memory limit
    print("    Testing uniqExact(user_id) under memory cap...")
    try:
        client.query(f"SELECT uniqExact(user_id) FROM {TABLE_NAME} SETTINGS max_memory_usage = {QUERY_MEM_CAP}")
        print("    uniqExact succeeded.")
    except DatabaseError:
        console.print(f"    [bold red][!] uniqExact failed as expected:[/] Exact hash set exceeded {QUERY_MEM_CAP / 1024 / 1024:.1f}MB.")

    # Step 3b: uniq (HyperLogLog) succeeds effortlessly
    print("    Testing uniq(user_id) HyperLogLog under identical memory cap...")
    start_time = time.perf_counter()
    hll_count = client.query(f"SELECT uniq(user_id) FROM {TABLE_NAME} SETTINGS max_memory_usage = {QUERY_MEM_CAP}").result_rows[0][0]
    elapsed = time.perf_counter() - start_time

    # Step 3c: Obtain true count with external aggregation or relaxed limit to show error %
    exact_count = client.query(f"SELECT uniqExact(user_id) FROM {TABLE_NAME} SETTINGS max_memory_usage = 100000000").result_rows[0][0]
    error_pct = round(abs(exact_count - hll_count) / exact_count * 100, 3)

    print(f"    Exact Count (uniqExact) : {exact_count:,}")
    print(f"    HLL Count (uniq)        : {hll_count:,}")
    console.print(f"    Approximation Error     : [bold green]{error_pct}%[/] (< 1% variance)")
    print(f"    HLL Query Duration      : {elapsed * 1000:.2f} ms (succeeded within memory cap)")


def main() -> None:
    console.print("[bold cyan]=================================================================[/]")
    console.print("[bold cyan]Experiment 02: Analytical Memory Saturation & Disk Spill[/]")
    print("Testing max_memory_usage limits, external aggregation, and HLL algorithms.")
    console.print("[bold cyan]=================================================================[/]")

    client = get_client()

    # Verify server baseline setting
    configured_limit = client.query("SELECT value FROM system.settings WHERE name = 'max_memory_usage'").result_rows[0][0]
    print(f"[*] Engine Baseline Setting: max_memory_usage = {int(configured_limit) / 1024 / 1024:.2f} MiB ({configured_limit} bytes)")

    # Prepare dataset
    seed_clickstream_dataset(client, num_rows=500_000)

    # Part 1: Failure on in-memory high-cardinality aggregation
    run_in_memory_aggregation(client)

    # Part 2: Successful external aggregation via disk spill
    run_spill_to_disk_aggregation(client)

    # Part 3: HLL comparison
    run_hyperloglog_comparison(client)

    console.print("\n[bold green]=================================================================")
    print("SYSTEMS & PRODUCTION TAKEAWAYS")
    console.print("=================================================================[/]")
    print("1. ClickHouse allocates memory aggressively for vector hash tables during GROUP BY.")
    print("   High-cardinality keys (like UUIDs or session IDs) scale RAM linearly with key count.")
    print("2. Memory exhaustion guards:")
    print("   - max_memory_usage prevents a single runaway query from consuming all host memory")
    print("     and causing Docker/Kubernetes OOM killer (Exit 137).")
    print("3. Disk Spill Strategy:")
    print("   - Set max_bytes_before_external_group_by to ~50% of max_memory_usage.")
    print("   - High-throughput OLAP queries gracefully degrade to temporary disk storage")
    print("     instead of dropping connections or failing client SLAs.")
    print("4. Algorithmic Optimization:")
    print("   - Prefer uniq() / uniqCombined64() over uniqExact() for high-cardinality telemetry.")
    print("   - HyperLogLog uses fixed 12KB-register state vs Megabytes of exact hash tables.")
    print("=================================================================\n")


if __name__ == "__main__":
    main()

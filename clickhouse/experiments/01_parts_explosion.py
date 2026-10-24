# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "clickhouse-connect>=0.7.0",
# ]
# ///
"""
Experiment 01: The "Too Many Parts" Failure in ClickHouse MergeTree

Demonstrates:
1. Architectural Cause: Every INSERT into a MergeTree engine creates a distinct,
   immutable data part on disk. ClickHouse background worker threads asynchronously
   compact and merge small parts into larger ones.
2. Limit Saturation (Parts Explosion): When an application sends rapid un-batched
   single-row inserts, parts accumulate faster than background merge threads can merge them.
   Once active parts exceed parts_to_throw_insert, ClickHouse rejects writes with:
   "DB::Exception: Too many parts in all data parts in table ... (Code: 252)".
3. The Vectorized Solution (Batched Inserts): Inserting 2,000 rows in a single batch
   creates exactly 1 active data part, executing in sub-second time.
"""

from __future__ import annotations

import sys
import time

from rich.console import Console

try:
    import clickhouse_connect
    from clickhouse_connect.driver.exceptions import DatabaseError
except ImportError:
    print("clickhouse-connect not found. Run this script via 'uv run experiments/01_parts_explosion.py'")
    sys.exit(1)

HOST = "127.0.0.1"
PORT = 8123
USERNAME = "default"
PASSWORD = ""
TABLE_NAME = "parts_test"


console = Console()


def get_client() -> clickhouse_connect.driver.Client:
    try:
        client = clickhouse_connect.get_client(
            host=HOST,
            port=PORT,
            username=USERNAME,
            password=PASSWORD,
            connect_timeout=3,
            send_receive_timeout=10,
        )
        # Verify server is alive
        client.command("SELECT 1")
        return client
    except Exception as err:
        console.print("[bold red][!] Error: Unable to connect to ClickHouse at 127.0.0.1:8123.[/]")
        print(f"    Details: {err}")
        print("\nPlease start the target profile first:")
        print("    cd clickhouse && make up EXP=parts-limit\n")
        sys.exit(1)


def get_active_parts(client: clickhouse_connect.driver.Client, table: str) -> list[tuple]:
    """Retrieve active parts count and details from system.parts."""
    query = f"""
    SELECT name, rows, bytes_on_disk
    FROM system.parts
    WHERE table = '{table}' AND active = 1
    ORDER BY modification_time DESC
    """
    return client.query(query).result_rows


def reset_table(client: clickhouse_connect.driver.Client, table: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {table}")
    # Configure lower parts_to_throw_insert for fast, deterministic limit demonstration
    create_sql = f"""
    CREATE TABLE {table} (
        id UInt64,
        created_at DateTime DEFAULT now(),
        payload String
    ) ENGINE = MergeTree()
    ORDER BY id
    SETTINGS parts_to_delay_insert = 80, parts_to_throw_insert = 120
    """
    client.command(create_sql)


def run_unbatched_inserts(client: clickhouse_connect.driver.Client) -> None:
    console.print("\n[bold blue][*] Part 1: Rapid Un-Batched Single-Row Inserts (Testing Merge Capacity)...[/]")
    print("    Target Table : parts_test (MergeTree)")
    print("    Thresholds   : parts_to_delay_insert=80, parts_to_throw_insert=120")
    print("    Firing up to 500 single-row inserts without batching...\n")

    reset_table(client, TABLE_NAME)

    inserted_count = 0
    start_time = time.perf_counter()
    too_many_parts_hit = False

    for i in range(1, 501):
        try:
            # Single-row insert creates 1 new physical part on disk
            client.insert(TABLE_NAME, [[i, f"payload-data-node-{i:05d}"]], column_names=["id", "payload"])
            inserted_count += 1

            if inserted_count % 15 == 0 or inserted_count in (1, 50, 80, 100):
                active_parts = get_active_parts(client, TABLE_NAME)
                elapsed = time.perf_counter() - start_time
                console.print(f"    Insert #{inserted_count:3d} | Active Parts on Disk: [bold yellow]{len(active_parts):3d}[/] | Elapsed: {elapsed:.2f}s")

        except DatabaseError as err:
            err_msg = str(err)
            if "Too many parts" in err_msg or "252" in err_msg:
                too_many_parts_hit = True
                active_parts = get_active_parts(client, TABLE_NAME)
                console.print(f"\n  [bold red][!] WRITE REJECTED AT INSERT #{inserted_count + 1}![/]")
                console.print(f"      Active parts in system.parts : [bold red]{len(active_parts)}[/]")
                print("      ClickHouse Error Code         : 252 (TOO_MANY_PARTS)")
                print(f"      Engine Message                : {err_msg.strip()}")
                break
            else:
                console.print(f"\n  [bold red][!] Unexpected error: {err}[/]")
                break
        except Exception as err:
            console.print(f"\n  [bold red][!] Client error: {err}[/]")
            break

    if not too_many_parts_hit:
        active_parts = get_active_parts(client, TABLE_NAME)
        print(f"\n  [*] Loop finished without error. Active parts: {len(active_parts)}")
    else:
        console.print("\n  [bold green][✓] Failure Reproduced:[/] ClickHouse backpressure mechanism successfully throttled")
        print("      writes because background compaction could not keep pace with point inserts.")


def run_batched_insert(client: clickhouse_connect.driver.Client) -> None:
    console.print("\n[bold blue][*] Part 2: Vectorized Batched Insert (Production Pattern)...[/]")
    reset_table(client, TABLE_NAME)

    batch_size = 2000
    print(f"    Generating {batch_size:,} records in client memory...")
    batch_data = [[i, f"batched-payload-{i:05d}"] for i in range(1, batch_size + 1)]

    print(f"    Sending 1 batch of {batch_size:,} rows in a single network roundtrip...")
    start_time = time.perf_counter()
    client.insert(TABLE_NAME, batch_data, column_names=["id", "payload"])
    elapsed = time.perf_counter() - start_time

    active_parts = get_active_parts(client, TABLE_NAME)
    total_rows = client.command(f"SELECT count() FROM {TABLE_NAME}")

    console.print("\n    Batch Status          : [bold green]SUCCESS[/]")
    print(f"    Total Rows in Table   : {total_rows:,}")
    console.print(f"    Active Parts Created  : [bold green]{len(active_parts)}[/] (Entire batch written as 1 contiguous part)")
    print(f"    Duration              : {elapsed * 1000:.2f} ms ({batch_size / max(elapsed, 0.001):,.0f} rows/sec)")

    if active_parts:
        part_name, part_rows, part_bytes = active_parts[0]
        print(f"    Part Disk Descriptor  : name={part_name}, rows={part_rows}, compressed_bytes={part_bytes}")


def main() -> None:
    console.print("[bold cyan]=================================================================[/]")
    console.print("[bold cyan]Experiment 01: ClickHouse Parts Explosion & Batching Limits[/]")
    print("Investigating MergeTree write mechanics and the TOO_MANY_PARTS threshold.")
    console.print("[bold cyan]=================================================================[/]")

    client = get_client()

    # Part 1: Parts explosion failure
    run_unbatched_inserts(client)

    # Part 2: Clean batched insert
    run_batched_insert(client)

    console.print("\n[bold green]=================================================================")
    print("SYSTEMS & PRODUCTION TAKEAWAYS")
    console.print("=================================================================[/]")
    print("1. ClickHouse is NOT an OLTP database. Every INSERT statement writes a distinct")
    print("   filesystem directory containing compressed column data (.bin) and index marks (.mrk).")
    print("2. Treating ClickHouse like an OLTP store (Postgres) with single-row inserts guarantees catastrophic")
    print("   TOO_MANY_PARTS (Code: 252) failures and disk metadata thrashing.")
    print("3. Best practices for ClickHouse ingestion:")
    print("   - Batch inserts on client side (>= 1,000 to 100,000 rows per batch).")
    print("   - Use an ingestion buffer: Apache Kafka, Vector, FluentBit, or Redis Streams.")
    print("   - Enable ClickHouse native Async Inserts: SET async_insert = 1, wait_for_async_insert = 1")
    print("     (ClickHouse will internally buffer point writes in RAM into batched parts).")
    print("=================================================================\n")


if __name__ == "__main__":
    main()

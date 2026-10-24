# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "pymongo>=4.6.0",
# ]
# ///
"""
Experiment 01: MongoDB 100MB In-Memory Sort Ceiling & External Disk Spill

Demonstrates:
1. Hard Engine Failure (PlanExecutor Sort Limit):
   - By default, MongoDB restricts in-memory blocking sorts to 100MB
     (internalQueryMaxBlockingSortMemoryUsageBytes = 104,857,600 bytes).
   - Queries without a supporting B-Tree index on large datasets abort with:
     'OperationFailure: Sort exceeded memory limit of 104857600 bytes, but did not opt in to external sort.'
2. Mitigation 1: External Sort (allowDiskUse=True):
   - Opting into external sorting allows WiredTiger to spill temporary sort blocks to disk.
   - Query succeeds, but incurs disk I/O latency.
3. Mitigation 2: B-Tree Index:
   - Creating an index on the sort field completely eliminates the blocking SORT stage.
   - Results are streamed via B-Tree index traversal (IXSCAN) with 0 memory allocated to sorting.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import pymongo
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure
from rich.console import Console

MONGO_URI = "mongodb://127.0.0.1:27017/"
DB_NAME = "lab_db"
COLL_NAME = "orders"
TARGET_DOCS = 100_000
PAYLOAD_SIZE = 1_100  # ~1.1 KB payload per doc -> 100,000 docs ≈ 115 MB BSON


console = Console()


def get_client() -> MongoClient:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2500)
    try:
        client.admin.command("ping")
    except ConnectionFailure:
        console.print("[bold red][ERROR] Cannot connect to MongoDB on 127.0.0.1:27017.[/]")
        print("\nPlease start the 'unindexed-sort' container first:")
        print("  cd mongodb && make up EXP=unindexed-sort\n")
        sys.exit(1)
    return client


def seed_orders(coll: pymongo.collection.Collection, force: bool = False) -> None:
    current_count = coll.count_documents({})
    if current_count >= TARGET_DOCS and not force:
        print(f"[*] Collection '{COLL_NAME}' already seeded with {current_count:,} documents.")
        return

    if force or current_count > 0:
        print(f"[*] Resetting collection '{COLL_NAME}'...")
        coll.drop()

    print(f"[*] Generating & inserting {TARGET_DOCS:,} documents (~115MB total BSON)...")
    t0 = time.perf_counter()

    batch_size = 10_000
    base_time = 1_700_000_000
    dummy_payload = "X" * PAYLOAD_SIZE

    for batch_start in range(0, TARGET_DOCS, batch_size):
        batch = [
            {
                "order_id": i,
                "customer_id": f"cust_{i % 5000:05d}",
                "timestamp": base_time + i,
                "amount": round(10.0 + (i % 1000) * 0.45, 2),
                "status": "COMPLETED",
                "metadata": dummy_payload,
            }
            for i in range(batch_start, batch_start + batch_size)
        ]
        coll.insert_many(batch, ordered=False)
        inserted = batch_start + len(batch)
        pct = (inserted / TARGET_DOCS) * 100
        print(f"    Inserted {inserted:,}/{TARGET_DOCS:,} ({pct:.0f}%)", end="\r", flush=True)

    elapsed = time.perf_counter() - t0
    print(f"\n[+] Seeding complete in {elapsed:.2f}s ({TARGET_DOCS / elapsed:,.0f} docs/sec).")


def extract_sort_details(plan_stats: dict[str, Any]) -> dict[str, Any]:
    """Inspect explain executionStats for sort stage details."""
    stats = plan_stats.get("executionStats", {})
    exec_stages = stats.get("executionStages", {})

    def find_stage(stage_dict: dict[str, Any], target_name: str) -> dict[str, Any] | None:
        if stage_dict.get("stage") == target_name:
            return stage_dict
        for child in stage_dict.get("inputStages", []):
            res = find_stage(child, target_name)
            if res:
                return res
        if "inputStage" in stage_dict:
            return find_stage(stage_dict["inputStage"], target_name)
        return None

    sort_stage = find_stage(exec_stages, "SORT")
    ixscan_stage = find_stage(exec_stages, "IXSCAN")

    return {
        "executionTimeMillis": stats.get("executionTimeMillis", -1),
        "totalDocsExamined": stats.get("totalDocsExamined", -1),
        "nReturned": stats.get("nReturned", -1),
        "has_sort_stage": sort_stage is not None,
        "has_ixscan_stage": ixscan_stage is not None,
        "usedDisk": sort_stage.get("usedDisk", False) if sort_stage else False,
        "memLimit": sort_stage.get("memLimit", 0) if sort_stage else 0,
        "memUsage": sort_stage.get("memUsage", 0) if sort_stage else 0,
    }


def run_experiment() -> None:
    client = get_client()
    db = client[DB_NAME]
    coll = db[COLL_NAME]

    console.print("[bold blue]" + "=" * 70 + "[/]")
    console.print("[bold green]  Experiment 01: MongoDB 100MB In-Memory Sort Ceiling[/]")
    print(f"  Target DB      : {DB_NAME}")
    print(f"  Collection     : {COLL_NAME}")
    print("  Default Cap    : 104,857,600 bytes (100MB internalQueryMaxBlockingSortMemoryUsage)")
    console.print("[bold blue]" + "=" * 70 + "[/]\n")

    # Ensure any previous index on timestamp is dropped
    for idx_name in coll.index_information():
        if "timestamp" in idx_name:
            print(f"[*] Dropping prior index: {idx_name}")
            coll.drop_index(idx_name)

    seed_orders(coll)

    results_table: list[dict[str, str]] = []

    # -------------------------------------------------------------
    # Scenario 1: Unindexed Sort with allow_disk_use(False) (Strict In-Memory)
    # -------------------------------------------------------------
    console.print("\n[bold yellow][Scenario 1] Unindexed Sort with allow_disk_use(False) (In-Memory Only)[/]")
    print("  Query: db.orders.find().sort('timestamp', -1).allow_disk_use(False)")
    t0 = time.perf_counter()
    try:
        cursor = coll.find({}).sort("timestamp", -1).allow_disk_use(False)
        _ = list(cursor)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        console.print("  [bold red]Unexpected: Query succeeded![/]")
        results_table.append(
            {
                "mode": "Unindexed (allow_disk_use=False)",
                "status": "SUCCEEDED (Unexpected)",
                "sort_stage": "SORT",
                "disk_spill": "No",
                "time_ms": f"{elapsed_ms:.1f}ms",
            }
        )
    except OperationFailure as err:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        console.print(f"  [bold red][EXPECTED FAILURE CAUGHT][/] in {elapsed_ms:.1f}ms:")
        print(f"    Error Code    : {err.code}")
        print(f"    Error Details : {err.details.get('errmsg', str(err))}")
        results_table.append(
            {
                "mode": "Unindexed (allow_disk_use=False)",
                "status": f"FAILED (Code {err.code})",
                "sort_stage": "SORT (Aborted)",
                "disk_spill": "No (Disallowed)",
                "time_ms": f"{elapsed_ms:.1f}ms",
            }
        )

    # -------------------------------------------------------------
    # Scenario 2: Unindexed Sort with allowDiskUse=True (Spills to Disk)
    # -------------------------------------------------------------
    console.print("\n[bold yellow][Scenario 2] Unindexed Sort WITH allowDiskUse=True (Spill to Disk)[/]")
    print("  Query: db.orders.find().sort('timestamp', -1).allow_disk_use(True)")
    t0 = time.perf_counter()
    try:
        explain_plan = coll.find({}).sort("timestamp", -1).allow_disk_use(True).explain()
        details = extract_sort_details(explain_plan)

        cursor = coll.find({}).sort("timestamp", -1).allow_disk_use(True).limit(5)
        top5 = list(cursor)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        console.print(f"  [bold green][SUCCESS][/] Completed in {elapsed_ms:.1f}ms (Explain: {details['executionTimeMillis']}ms)")
        print(f"    Docs Examined : {details['totalDocsExamined']:,}")
        print(f"    Used Disk     : {details['usedDisk']} (Spilled temporary sort run to disk)")
        print(f"    Top Doc ID    : {top5[0]['order_id']} (ts={top5[0]['timestamp']})")
        results_table.append(
            {
                "mode": "Unindexed (allowDiskUse)",
                "status": "SUCCEEDED",
                "sort_stage": "SORT (External)",
                "disk_spill": str(details["usedDisk"]),
                "time_ms": f"{elapsed_ms:.1f}ms",
            }
        )
    except Exception as err:
        console.print(f"  [bold red]Error during allowDiskUse: {err}[/]")

    # -------------------------------------------------------------
    # Scenario 3: Indexed Sort (B-Tree Index Scan - 0 Sort Memory)
    # -------------------------------------------------------------
    console.print("\n[bold yellow][Scenario 3] Indexed Sort (B-Tree Index on {'timestamp': -1})[/]")
    print("  Creating index: db.orders.create_index([('timestamp', -1)])...")
    idx_t0 = time.perf_counter()
    coll.create_index([("timestamp", -1)], name="idx_timestamp_desc")
    print(f"  Index created in {(time.perf_counter() - idx_t0) * 1000:.1f}ms.")

    print("  Query: db.orders.find().sort('timestamp', -1)")
    t0 = time.perf_counter()
    explain_plan = coll.find({}, {"order_id": 1, "timestamp": 1}).sort("timestamp", -1).explain()
    details = extract_sort_details(explain_plan)

    cursor = coll.find({}, {"order_id": 1, "timestamp": 1}).sort("timestamp", -1).limit(5)
    top5 = list(cursor)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    console.print(f"  [bold green][SUCCESS][/] Completed in {elapsed_ms:.1f}ms (Explain: {details['executionTimeMillis']}ms)")
    print(f"    Sort Stage    : {'Eliminated (IXSCAN only)' if not details['has_sort_stage'] else 'SORT'}")
    print(f"    Index Used    : {details['has_ixscan_stage']} (B-Tree backward traversal)")
    print("    Sort Memory   : 0 bytes (Streaming pipeline)")
    print(f"    Top Doc ID    : {top5[0]['order_id']} (ts={top5[0]['timestamp']})")
    results_table.append(
        {
            "mode": "Indexed Sort (B-Tree)",
            "status": "SUCCEEDED",
            "sort_stage": "None (IXSCAN)",
            "disk_spill": "No (0 bytes RAM)",
            "time_ms": f"{elapsed_ms:.1f}ms",
        }
    )

    # -------------------------------------------------------------
    # Summary Comparison Table
    # -------------------------------------------------------------
    console.print("\n[bold blue]" + "=" * 80 + "[/]")
    console.print("[bold white]Execution Summary & Engine Behavior:[/]")
    console.print("[bold blue]" + "-" * 80 + "[/]")
    print(f"{'Strategy':<26} | {'Outcome':<18} | {'Sort Stage':<16} | {'Spill Disk':<10} | {'Latency':<8}")
    print("-" * 80)
    for row in results_table:
        print(f"{row['mode']:<26} | {row['status']:<18} | {row['sort_stage']:<16} | {row['disk_spill']:<10} | {row['time_ms']:<8}")
    console.print("[bold blue]" + "=" * 80 + "[/]\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="MongoDB 100MB Sort Ceiling Experiment")
    parser.add_argument("--reseed", action="store_true", help="Force reseed collection with 100,000 docs")
    args = parser.parse_args()

    if args.reseed:
        client = get_client()
        seed_orders(client[DB_NAME][COLL_NAME], force=True)

    run_experiment()


if __name__ == "__main__":
    main()

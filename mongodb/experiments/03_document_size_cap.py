# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "pymongo>=4.6.0",
# ]
# ///
"""
Experiment 03: Document Growth, Reallocation & the 16MB BSON Hard Ceiling

Demonstrates:
1. The Unbounded Array Anti-Pattern:
   - Repeatedly appending ($push) events into a single parent document.
   - As the document grows from 10KB to 16MB, WiredTiger must repeatedly allocate
     larger memory buffers and rewrite the entire bloated document on every tiny update.
2. Hitting the Hard 16,777,216 Byte BSON Ceiling:
   - Capturing the exact OperationFailure when a $push breaches 16MB.
3. The Bucket Pattern Remediation:
   - Grouping events into fixed-count sub-documents (e.g. 500 events per bucket),
     keeping write latency flat and avoiding the 16MB cliff.
"""

from __future__ import annotations

import sys
import time

from pymongo import MongoClient
from pymongo.errors import DocumentTooLarge, OperationFailure, WriteError
from rich.console import Console

MONGO_URI = "mongodb://127.0.0.1:27017/"
BSON_MAX_BYTES = 16 * 1024 * 1024  # 16,777,216 bytes


console = Console()


def format_bytes(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} TB"


def main() -> None:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception:
        console.print("[bold red]Error: Cannot connect to MongoDB on 127.0.0.1:27017.[/]")
        console.print("Start the container first: make up EXP=document-limits", markup=False, highlight=False, soft_wrap=True)
        sys.exit(1)

    db = client["lab_db"]
    db.drop_collection("unbounded_sensors")
    db.drop_collection("bucketed_sensors")

    console.print("=" * 70, style="bold cyan")
    console.print("[bold cyan]Experiment 03: MongoDB Unbounded Array Growth & the 16MB BSON Ceiling[/]")
    console.print(f"Hard BSON Document Limit: {format_bytes(BSON_MAX_BYTES)} ({BSON_MAX_BYTES:,} bytes)", markup=False, highlight=False, soft_wrap=True)
    console.print("=" * 70 + "\n", style="bold cyan")

    # 1. Part 1: Unbounded $push into a single document until 16MB crash
    console.print("[bold blue][*] Part 1: Appending 256KB event batches via $push until 16MB crash...[/]")
    col_unbounded = db["unbounded_sensors"]
    col_unbounded.insert_one({"sensor_id": "sensor-01", "events": []})

    # Each batch adds ~256KB of array data (64 events * 4KB each)
    single_event = {"ts": 1700000000, "payload": "X" * 4000}
    batch_events = [single_event for _ in range(64)]

    batch_num = 0
    initial_latency_ms = 0.0
    peak_latency_ms = 0.0

    while True:
        batch_num += 1
        t0 = time.perf_counter()
        try:
            col_unbounded.update_one(
                {"sensor_id": "sensor-01"},
                {"$push": {"events": {"$each": batch_events}}},
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if batch_num == 1:
                initial_latency_ms = elapsed_ms
            peak_latency_ms = max(peak_latency_ms, elapsed_ms)

            if batch_num % 10 == 0 or batch_num == 1:
                # Measure current BSON object size in engine via $bsonSize
                stats = list(
                    col_unbounded.aggregate(
                        [
                            {"$match": {"sensor_id": "sensor-01"}},
                            {"$project": {"size": {"$bsonSize": "$$ROOT"}, "count": {"$size": "$events"}}},
                        ]
                    )
                )[0]
                console.print(
                    f"    Batch {batch_num:>2}: Events={stats['count']:>5,} | BSON Size={format_bytes(stats['size']):>9} | Write Latency={elapsed_ms:>6.2f}ms",
                markup=False, highlight=False, soft_wrap=True
                )
        except (DocumentTooLarge, WriteError, OperationFailure) as err:
            stats = list(
                col_unbounded.aggregate(
                    [
                        {"$match": {"sensor_id": "sensor-01"}},
                        {"$project": {"size": {"$bsonSize": "$$ROOT"}, "count": {"$size": "$events"}}},
                    ]
                )
            )[0]
            console.print(f"\n  [bold red][!] HARD 16MB CEILING BREACHED at Batch {batch_num}![/]")
            console.print(f"      Document Size Before Rejection : {format_bytes(stats['size'])} ({stats['size']:,} bytes)", markup=False, highlight=False, soft_wrap=True)
            console.print(f"      Error Code / Message           : {err}", markup=False, highlight=False, soft_wrap=True)
            break

    # 2. Part 2: The Bucket Pattern (Capping array at 500 events per document)
    console.print("\n[bold blue][*] Part 2: Remediation via the Bucket Pattern (max 64 events/bucket)...[/]")
    col_bucketed = db["bucketed_sensors"]
    col_bucketed.create_index([("sensor_id", 1), ("bucket_count", 1)])

    t_start = time.perf_counter()
    # Write 70 batches (more data than crashed Part 1!)
    for b in range(1, 71):
        col_bucketed.update_one(
            {"sensor_id": "sensor-01", "bucket_count": {"$lt": 64}},
            {
                "$push": {"events": {"$each": batch_events}},
                "$inc": {"bucket_count": len(batch_events)},
                "$setOnInsert": {"created_at": time.time()},
            },
            upsert=True,
        )
    bucket_elapsed_ms = (time.perf_counter() - t_start) * 1000
    num_buckets = col_bucketed.count_documents({"sensor_id": "sensor-01"})

    console.print(f"    Successfully wrote 70 batches ({70 * 64:,} events, ~18MB total) with ZERO errors!", markup=False, highlight=False, soft_wrap=True)
    console.print(f"    Total Bucket Documents Created : {num_buckets}", markup=False, highlight=False, soft_wrap=True)
    console.print(f"    Average Write Latency / Batch  : {bucket_elapsed_ms / 70:.2f}ms (vs {peak_latency_ms:.2f}ms peak on bloated doc)", markup=False, highlight=False, soft_wrap=True)

    console.print("\n" + "=" * 70, style="bold green")
    console.print("SYSTEMS TAKEAWAYS", markup=False, highlight=False, soft_wrap=True)
    console.print("=" * 70, style="bold green")
    console.print("1. Never use unbounded $push arrays for activity logs, comments, or IoT telemetry.", markup=False, highlight=False, soft_wrap=True)
    console.print(f"2. Write amplification: Latency grew from {initial_latency_ms:.2f}ms to {peak_latency_ms:.2f}ms as WiredTiger", markup=False, highlight=False, soft_wrap=True)
    console.print("   had to reallocate and rewrite the entire multi-megabyte document on every update.", markup=False, highlight=False, soft_wrap=True)
    console.print("3. The Bucket Pattern ($lt threshold + upsert: true) splits streams into bounded", markup=False, highlight=False, soft_wrap=True)
    console.print("   documents (~256KB each), keeping memory footprint and latency O(1).", markup=False, highlight=False, soft_wrap=True)
    console.print("======================================================================\n", markup=False, highlight=False, soft_wrap=True)

    client.close()


if __name__ == "__main__":
    main()

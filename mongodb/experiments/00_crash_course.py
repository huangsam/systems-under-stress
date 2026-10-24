# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "pymongo>=4.6.0",
# ]
# ///
"""
MongoDB Crash Course: Document Storage, Nested Queries & Aggregation Pipelines

Demonstrates:
1. Ping connectivity & server version check via buildInfo.
2. Document creation (nested schemas, arrays, BSON types).
3. Filter queries ($gt, nested field lookups) and partial updates ($set, $inc, $push).
4. Multi-stage Aggregation Pipeline ($match -> $group -> $sort).
5. Collection & Engine diagnostics (collStats, serverStatus).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from pymongo import MongoClient
from rich.console import Console

MONGO_URI = "mongodb://127.0.0.1:27017/"
DB_NAME = "lab_db"
COLL_NAME = "crash_course_inventory"


console = Console()


def main() -> None:
    console.print("\n[bold cyan]" + "=" * 70 + "[/]")
    console.print("[bold green]MongoDB Crash Course: Documents, Pipelines & Diagnostics[/]")
    console.print("[bold cyan]" + "=" * 70 + "[/]\n")

    client: MongoClient = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)

    try:
        ping_res = client.admin.command("ping")
        console.print(f"[bold blue][*] 1. Server Connectivity & Info (ping: {ping_res.get('ok')})[/]")
    except Exception as e:
        console.print(f"[bold red][!] Connection failed: {e}[/]")
        print("Start the lab container first: cd mongodb && make up EXP=unindexed-sort")
        sys.exit(1)

    build_info = client.admin.command("buildInfo")
    print(f"    MongoDB Version: {build_info.get('version')}, Git: {build_info.get('gitVersion')[:8]}")

    db = client[DB_NAME]
    coll = db[COLL_NAME]
    coll.drop()

    # 2. Document insertion (nested documents & arrays)
    console.print("\n[bold blue][*] 2. Inserting Rich Documents (Nested BSON & Arrays)[/]")
    sample_docs = [
        {
            "sku": "LAP-01",
            "name": "Dev Laptop",
            "category": "hardware",
            "pricing": {"base": 1200.0, "currency": "USD", "discount_pct": 10},
            "tags": ["portable", "apple", "arm64"],
            "stock": 15,
            "created_at": datetime.now(UTC),
        },
        {
            "sku": "MON-02",
            "name": "4K IPS Monitor",
            "category": "hardware",
            "pricing": {"base": 450.0, "currency": "USD", "discount_pct": 0},
            "tags": ["display", "4k"],
            "stock": 30,
            "created_at": datetime.now(UTC),
        },
        {
            "sku": "SUB-03",
            "name": "Cloud Storage Tier 1",
            "category": "services",
            "pricing": {"base": 20.0, "currency": "USD", "discount_pct": 5},
            "tags": ["cloud", "saas", "subscription"],
            "stock": 9999,
            "created_at": datetime.now(UTC),
        },
        {
            "sku": "SUB-04",
            "name": "Enterprise Support Plan",
            "category": "services",
            "pricing": {"base": 500.0, "currency": "USD", "discount_pct": 15},
            "tags": ["support", "sla", "enterprise"],
            "stock": 50,
            "created_at": datetime.now(UTC),
        },
    ]

    res = coll.insert_many(sample_docs)
    print(f"    Inserted {len(res.inserted_ids)} documents into '{COLL_NAME}'.")

    # 3. Filter queries and atomic updates
    console.print("\n[bold blue][*] 3. Nested Query & Atomic Field Modification[/]")
    # Query using nested dot-notation
    found = coll.find_one({"pricing.base": {"$gte": 1000.0}})
    if found:
        print(f"    High-value item found: '{found['name']}' at ${found['pricing']['base']}")

    # Atomic update ($inc and $push)
    coll.update_one(
        {"sku": "LAP-01"},
        {"$inc": {"stock": -1}, "$push": {"tags": "backorder-risk"}},
    )
    updated = coll.find_one({"sku": "LAP-01"})
    if updated:
        print(f"    Updated 'LAP-01' stock: {updated['stock']}, tags: {updated['tags']}")

    # 4. Aggregation pipeline
    console.print("\n[bold blue][*] 4. Aggregation Pipeline ($match -> $group -> $sort)[/]")
    pipeline = [
        {"$match": {"stock": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$category",
                "item_count": {"$sum": 1},
                "total_stock": {"$sum": "$stock"},
                "avg_price": {"$avg": "$pricing.base"},
            }
        },
        {"$sort": {"total_stock": -1}},
    ]
    cursor = coll.aggregate(pipeline)
    print("    Aggregated results by category:")
    for doc in cursor:
        print(f"      - Category: {doc['_id']}: {doc['item_count']} items, avg ${doc['avg_price']:.2f}, total stock {doc['total_stock']}")

    # 5. Diagnostics
    console.print("\n[bold blue][*] 5. Diagnostics & Collection Stats[/]")
    server_status = db.command("serverStatus")
    mem_info = server_status.get("mem", {})
    connections = server_status.get("connections", {})
    print(f"    Resident Memory: {mem_info.get('resident')} MB, Virtual: {mem_info.get('virtual')} MB")
    print(f"    Current Connections: {connections.get('current')} (Available: {connections.get('available')})")

    # Cleanup
    coll.drop()
    print(f"    Dropped demo collection '{COLL_NAME}'.")

    console.print("\n[bold green][✓] MongoDB Crash Course completed successfully![/]\n")


if __name__ == "__main__":
    main()

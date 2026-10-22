# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pymongo>=4.6.0",
# ]
# ///
"""
Experiment 02: MongoDB Write Concerns & Crash Durability (SIGKILL / kill -9)

Tests data durability across MongoDB Write Concerns during an ungraceful crash:
1. w=0 (Unacknowledged):
   - Client sends write over the socket without waiting for server acknowledgment.
   - Highest throughput, but high loss on crash (network buffers, uncommitted state).
2. w=1, j=False (Memory-Acknowledged / Standalone Default):
   - Mongod acknowledges once written to the WiredTiger in-memory cache.
   - Writes waiting in RAM before the journal commit interval (up to 100ms)
     or checkpoint (60s) are lost when terminated abruptly by SIGKILL.
3. w=1, j=True (Journal-Committed / Fsync on Disk):
   - Mongod blocks acknowledgment until the write is flushed and fsynced to the on-disk journal.
   - On SIGKILL, WiredTiger journal recovery replays all committed writes: 0 lost acknowledged writes.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient, WriteConcern
from pymongo.errors import AutoReconnect, ConnectionFailure, PyMongoError

COMPOSE_DIR = Path(__file__).resolve().parent.parent
MONGO_URI = "mongodb://127.0.0.1:27017/"
DB_NAME = "lab_db"
COLL_NAME = "durability_test"


def run_compose_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "--profile", "durability"] + args
    return subprocess.run(cmd, cwd=COMPOSE_DIR, capture_output=True, text=True, check=True)


def ensure_container_ready() -> MongoClient:
    try:
        run_compose_cmd(["up", "-d", "--wait"])
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        print(f"\033[1;33m[Notice] Docker command execution issue: {err}\033[0m")

    # Connect with timeout
    for attempt in range(1, 15):
        try:
            client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
            client.admin.command("ping")
            return client
        except ConnectionFailure:
            time.sleep(1)

    print("\033[1;31m[ERROR] Unable to connect to MongoDB on 127.0.0.1:27017.\033[0m")
    print("Please ensure the durability container is up:")
    print("  cd mongodb && make up EXP=durability")
    sys.exit(1)


def test_write_concern_durability(
    mode_name: str,
    write_concern: WriteConcern,
    target_writes: int = 2500,
    kill_after: int = 1200,
) -> dict[str, Any]:
    print(f"\n\033[1;34m" + "-" * 70 + "\033[0m")
    w_val = write_concern.document.get("w", 1)
    j_val = write_concern.document.get("j", False)
    print(f"\033[1;33mTesting Mode: {mode_name}\033[0m (w={w_val}, j={j_val})")
    print("-" * 70)

    client = ensure_container_ready()
    db = client[DB_NAME]
    coll = db.get_collection(COLL_NAME, write_concern=write_concern)

    # Clean collection
    coll.drop()
    print("  [*] Dropped test collection for clean baseline.")

    ack_count = 0
    sent_count = 0
    writer_done = threading.Event()
    stop_writing = threading.Event()
    write_errors = 0
    t0 = time.perf_counter()

    def writer_thread() -> None:
        nonlocal ack_count, sent_count, write_errors
        for i in range(1, target_writes + 1):
            if stop_writing.is_set():
                break
            sent_count = i
            doc = {
                "_id": i,
                "seq": i,
                "timestamp": time.time(),
                "payload": f"payload_data_{i:06d}_" + "Z" * 128,
            }
            try:
                coll.insert_one(doc)
                ack_count = i
            except (AutoReconnect, ConnectionFailure, PyMongoError):
                write_errors += 1
                break
            except Exception:
                write_errors += 1
                break
        writer_done.set()

    t = threading.Thread(target=writer_thread, daemon=True)
    t.start()

    # Monitor until kill threshold is reached
    print(f"  [*] Firing rapid writes (target kill trigger at ~{kill_after:,} writes)...")
    while (sent_count < kill_after if w_val == 0 else ack_count < kill_after) and not writer_done.is_set():
        time.sleep(0.005)

    effective_reported = sent_count if w_val == 0 else ack_count
    print(f"  [*] Trigger reached ({effective_reported:,} writes recorded).")
    print("  \033[1;31m[*] Terminating container abruptly via SIGKILL (kill -9)...\033[0m")

    # Send SIGKILL to the container
    try:
        run_compose_cmd(["kill", "-s", "SIGKILL", "durability"])
    except Exception as e:
        print(f"    Notice: kill command failed or completed: {e}")

    stop_writing.set()
    writer_done.wait(timeout=2.0)
    write_duration = time.perf_counter() - t0

    reported_total = sent_count if w_val == 0 else ack_count
    ops_sec = reported_total / write_duration if write_duration > 0 else 0
    print(f"    Writes before crash : {reported_total:,} ({ops_sec:,.0f} ops/sec)")

    print("  [*] Container down. Restarting container to test WiredTiger crash recovery...")
    restart_t0 = time.perf_counter()
    new_client = ensure_container_ready()
    restart_sec = time.perf_counter() - restart_t0
    print(f"  [+] MongoDB restarted and healthy in {restart_sec:.2f}s.")

    # Audit recovered data
    rec_coll = new_client[DB_NAME][COLL_NAME]
    recovered_count = rec_coll.count_documents({})
    max_doc = rec_coll.find_one(sort=[("seq", -1)])
    max_seq = max_doc["seq"] if max_doc else 0

    lost_writes = max(0, reported_total - recovered_count)
    loss_pct = (lost_writes / reported_total * 100.0) if reported_total > 0 else 0.0

    print(f"  \033[1;36mAudit Results for {mode_name}:\033[0m")
    print(f"    Client Acknowledged / Sent : {reported_total:,}")
    print(f"    Recovered Documents in DB  : {recovered_count:,} (highest seq={max_seq:,})")
    print(f"    Lost Writes                : \033[1;{'31' if lost_writes > 0 else '32'}m{lost_writes:,} ({loss_pct:.1f}%)\033[0m")

    return {
        "mode": mode_name,
        "write_concern": f"w={w_val}, j={j_val}",
        "sent_or_ack": reported_total,
        "recovered": recovered_count,
        "lost": lost_writes,
        "loss_pct": f"{loss_pct:.1f}%",
        "durability": "100% (Zero Loss)" if lost_writes == 0 else f"Vulnerable ({loss_pct:.1f}% lost)",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="MongoDB Write Concerns & Crash Recovery")
    parser.add_argument(
        "--mode",
        choices=["all", "w0", "w1", "jtrue"],
        default="all",
        help="Write concern mode to test (default: all)",
    )
    args = parser.parse_args()

    print("\033[1;34m" + "=" * 75 + "\033[0m")
    print("\033[1;32m  Experiment 02: MongoDB Write Concerns & Crash Recovery (SIGKILL)\033[0m")
    print("  Investigating durability: w=0 vs w=1 (RAM) vs w=1, j=True (Disk Journal)")
    print("\033[1;34m" + "=" * 75 + "\033[0m")

    modes_to_test: list[tuple[str, WriteConcern, int, int]] = []

    if args.mode in ("all", "w0"):
        modes_to_test.append(("w=0 (Unacknowledged)", WriteConcern(w=0), 3000, 1500))
    if args.mode in ("all", "w1"):
        modes_to_test.append(("w=1, j=False (Memory-Ack)", WriteConcern(w=1, j=False), 2500, 1200))
    if args.mode in ("all", "jtrue"):
        modes_to_test.append(("w=1, j=True (Journal-Synced)", WriteConcern(w=1, j=True), 1500, 700))

    results: list[dict[str, Any]] = []
    for label, wc, target, kill_at in modes_to_test:
        res = test_write_concern_durability(label, wc, target_writes=target, kill_after=kill_at)
        results.append(res)
        time.sleep(1)

    print("\n\033[1;34m" + "=" * 80 + "\033[0m")
    print("\033[1;37mDurability Comparison Matrix:\033[0m")
    print("\033[1;34m" + "-" * 80 + "\033[0m")
    print(f"{'Mode':<28} | {'Ack/Sent':<10} | {'Recovered':<10} | {'Lost':<8} | {'Loss %':<8} | {'Durability':<15}")
    print("-" * 80)
    for r in results:
        print(f"{r['mode']:<28} | {r['sent_or_ack']:<10} | {r['recovered']:<10} | {r['lost']:<8} | {r['loss_pct']:<8} | {r['durability']:<15}")
    print("\033[1;34m" + "=" * 80 + "\033[0m\n")


if __name__ == "__main__":
    main()

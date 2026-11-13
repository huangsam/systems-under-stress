# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "pymongo>=4.6.0",
# ]
# ///
"""
Experiment 04: MongoDB 3-Node Replica Set Elections, Write Concern Durability & Oplog Exhaustion

Demonstrates:
1. Cluster Topology & Quorum:
   - 3-node replica set (rs0) across mongo-node1 (27017), mongo-node2 (27018), mongo-node3 (27019).
   - Validates Raft-style election terms and quorum (majority = 2/3 nodes).
2. Write Concern Durability Under Sudden Primary Stepdown:
   - Part A: Local / w:1 streaming writes during abrupt stepdown (replSetStepDown).
     In-flight and uncommitted writes are rejected/dropped with NotPrimaryError / AutoReconnect.
   - Part B: Majority consensus (w: "majority", j: True) during abrupt stepdown.
     Guarantees 0% write loss: every acknowledged write is replicated to quorum before stepdown.
3. Oplog Window Exhaustion & Stale Secondary State:
   - Pauses secondary mongo-node3 (docker pause).
   - Primary receives a burst of writes exceeding the 10MB capped oplog (--oplogSize 10).
   - Primary truncates oldest oplog entries past node3's paused optime.
   - Unpausing node3 causes it to fall off the oplog into RECOVERING state,
     requiring a full initial sync.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from pymongo import MongoClient, WriteConcern
from pymongo.errors import AutoReconnect, NotPrimaryError, PyMongoError
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

# Patch socket resolver so PyMongo can resolve Docker container hostnames from host
_ORIG_GETADDRINFO = socket.getaddrinfo


def _patched_getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
    if host == "mongo-node1":
        host = "127.0.0.1"
    elif host == "mongo-node2":
        host = "127.0.0.1"
        if port == 27017:
            port = 27018
    elif host == "mongo-node3":
        host = "127.0.0.1"
        if port == 27017:
            port = 27019
    return _ORIG_GETADDRINFO(host, port, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo

NODE_PORTS = {
    "mongo-node1:27017": 27017,
    "mongo-node2:27017": 27018,
    "mongo-node3:27017": 27019,
}

CLUSTER_URI = "mongodb://mongo-node1:27017,mongo-node2:27017,mongo-node3:27017/?replicaSet=rs0&retryWrites=false"

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def get_direct_client(port: int, timeout_ms: int = 15000) -> MongoClient:
    return MongoClient(
        f"mongodb://127.0.0.1:{port}/?directConnection=true",
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        socketTimeoutMS=timeout_ms,
    )


def get_cluster_client(timeout_ms: int = 15000) -> MongoClient:
    return MongoClient(
        CLUSTER_URI,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        socketTimeoutMS=timeout_ms,
    )


def ensure_replica_set_ready() -> tuple[MongoClient, dict[str, Any]]:
    """Validates cluster connectivity and initializes rs0 if not already initiated."""
    for port in (27017, 27018, 27019):
        try:
            with get_direct_client(port, timeout_ms=3000) as c:
                c.admin.command("ping")
        except Exception:
            console.print(f"[bold red][ERROR] Cannot connect to MongoDB node at 127.0.0.1:{port}.[/]")
            plain_console.print("Please start the replica set profile first:")
            plain_console.print("  make up EXP=replica-set")
            sys.exit(1)

    c1 = get_direct_client(27017)
    try:
        status = c1.admin.command("replSetGetStatus")
    except PyMongoError as err:
        if "no replset config" in str(err) or err.code == 94:
            console.print("[bold yellow][*] Initializing 3-node replica set 'rs0'...[/]")
            c1.admin.command(
                "replSetInitiate",
                {
                    "_id": "rs0",
                    "members": [
                        {"_id": 0, "host": "mongo-node1:27017", "priority": 1},
                        {"_id": 1, "host": "mongo-node2:27017", "priority": 1},
                        {"_id": 2, "host": "mongo-node3:27017", "priority": 1},
                    ],
                },
            )
            time.sleep(3)
        else:
            raise

    # Dynamically apply runtime cache cap (20MB) to prevent memory pressure under 128MB limit
    for port in (27017, 27018, 27019):
        try:
            with get_direct_client(port, timeout_ms=3000) as node_c:
                node_c.admin.command("setParameter", 1, wiredTigerEngineRuntimeConfig="cache_size=20M")
        except Exception:
            pass

    cluster = get_cluster_client()
    for _ in range(20):
        try:
            status = cluster.admin.command("replSetGetStatus")
            primary = [m for m in status.get("members", []) if m.get("stateStr") == "PRIMARY"]
            if primary:
                return cluster, status
        except Exception:
            pass
        time.sleep(1)

    console.print("[bold red][ERROR] Replica set did not elect a PRIMARY within timeout.[/]")
    sys.exit(1)


def find_primary(cluster_or_direct: MongoClient) -> tuple[str, int]:
    """Returns (node_name, host_port) for the current PRIMARY."""
    for _ in range(15):
        try:
            status = cluster_or_direct.admin.command("replSetGetStatus")
            for m in status.get("members", []):
                if m.get("stateStr") == "PRIMARY":
                    name = m["name"]
                    port = NODE_PORTS.get(name, 27017)
                    return name, port
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("No PRIMARY found in replica set status")


def phase1_topology(status: dict[str, Any]) -> None:
    """Phase 1: Cluster Topology Verification."""
    console.print("\n" + "=" * 76, style="bold cyan")
    console.print("[bold green]Phase 1: 3-Node Replica Set Topology & Quorum Verification[/]")
    console.print("=" * 76 + "\n", style="bold cyan")

    set_name = status.get("set", "rs0")
    term = status.get("term", 1)
    console.print(
        f"[bold white]Replica Set:[/] [cyan]{set_name}[/]  |  "
        f"[bold white]Current Election Term:[/] [magenta]{term}[/]  |  "
        f"[bold white]Voting Quorum:[/] [yellow]2 of 3 nodes (majority)[/]\n"
    )

    table = Table(
        box=box.ROUNDED,
        title="[bold yellow]MongoDB Replica Set Topology[/]",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Node Host", style="bold white", width=18)
    table.add_column("Port (Host)", style="cyan", justify="center", width=14)
    table.add_column("Role / State", style="bold", justify="center", width=14)
    table.add_column("Health", justify="center", width=10)
    table.add_column("Uptime", justify="right", width=10)
    table.add_column("Optime", style="dim", justify="center", width=22)

    primary_count = 0
    secondary_count = 0

    for m in status.get("members", []):
        name = m["name"]
        host_port = NODE_PORTS.get(name, 27017)
        state_str = m.get("stateStr", "UNKNOWN")
        health = "[bold green]1 (OK)[/]" if m.get("health", 0) == 1 else "[bold red]0 (FAIL)[/]"
        uptime = f"{m.get('uptime', 0)}s"
        optime_ts = str(m.get("optime", {}).get("ts", "N/A"))

        if state_str == "PRIMARY":
            primary_count += 1
            role_display = "[bold green]PRIMARY[/]"
        elif state_str == "SECONDARY":
            secondary_count += 1
            role_display = "[bold blue]SECONDARY[/]"
        else:
            role_display = f"[bold yellow]{state_str}[/]"

        table.add_row(name, f"127.0.0.1:{host_port}", role_display, health, uptime, optime_ts)

    console.print(table)
    console.print()

    verify_phase(
        primary_count == 1 and secondary_count == 2,
        "Phase 1: Topology & Quorum",
        f"Expected 1 PRIMARY and 2 SECONDARIES, got primary={primary_count}, secondary={secondary_count}",
    )
    console.print("[bold green]✔ Quorum Verified: Exactly 1 PRIMARY and 2 SECONDARIES active.[/]")


def phase2_durability_comparison(cluster: MongoClient) -> None:
    """Phase 2: Unacknowledged vs Majority Write Durability Under Sudden Primary Stepdown."""
    console.print("\n" + "=" * 76, style="bold cyan")
    console.print("[bold green]Phase 2: Write Durability Across Sudden Primary Stepdown Failover[/]")
    console.print("=" * 76 + "\n", style="bold cyan")

    console.print(
        "[bold white]Comparing write survival across election failover between:[/]\n"
        "  1. [bold yellow]Local Acknowledgment (w: 1)[/]: Client writes to primary memory/journal only.\n"
        "  2. [bold green]Majority Consensus (w: 'majority', j: True)[/]: Requires 2/3 nodes before ack.\n"
    )

    # -------------------------------------------------------------
    # Test A: w: 1 streaming writes during abrupt stepdown
    # -------------------------------------------------------------
    primary_name, primary_port = find_primary(cluster)
    console.print(f"[bold yellow][*] Sub-phase 2A: Streaming writes under w: 1 to current primary ({primary_name})...[/]")

    p_client = get_direct_client(primary_port, timeout_ms=5000)
    db_w1 = p_client.get_database("lab_db", write_concern=WriteConcern(w=1))
    db_w1.w1_failover.drop()

    w1_attempted: list[int] = []
    w1_acknowledged: list[int] = []
    w1_errors: list[str] = []
    stop_event = threading.Event()

    def w1_worker() -> None:
        seq = 0
        while not stop_event.is_set():
            w1_attempted.append(seq)
            try:
                db_w1.w1_failover.insert_one({"seq": seq, "ts": time.time(), "payload": "x" * 200})
                w1_acknowledged.append(seq)
                seq += 1
                time.sleep(0.001)
            except (NotPrimaryError, AutoReconnect, PyMongoError) as e:
                w1_errors.append(type(e).__name__)
                break

    t_w1 = threading.Thread(target=w1_worker, daemon=True)
    t_w1.start()
    time.sleep(0.05)

    console.print(f"[bold red][!] Triggering abrupt primary stepdown on {primary_name} (secondaryCatchUp=0s, force=True)...[/]")
    try:
        p_client.admin.command("replSetStepDown", 15, secondaryCatchUpPeriodSecs=0, force=True)
    except Exception:
        pass

    stop_event.set()
    t_w1.join(timeout=3.0)

    console.print("[dim]Waiting for election of new PRIMARY...[/]")
    time.sleep(4.0)

    new_primary_name, new_primary_port = find_primary(cluster)
    console.print(f"[bold cyan][✔] Failover Complete: New PRIMARY elected: [bold white]{new_primary_name}[/][/]")

    new_p_client = get_direct_client(new_primary_port, timeout_ms=5000)
    w1_stored = new_p_client.lab_db.w1_failover.count_documents({})
    w1_attempted_count = len(w1_attempted)
    w1_ack_count = len(w1_acknowledged)
    w1_dropped_unacked = w1_attempted_count - w1_ack_count
    w1_lost_acked = max(0, w1_ack_count - w1_stored)

    console.print(
        f"    Attempted: {w1_attempted_count}  |  Acknowledged: {w1_ack_count}  |  "
        f"Stored on New Primary: {w1_stored}  |  "
        f"Rejected In-Flight: {w1_dropped_unacked}  |  "
        f"Errors: {len(w1_errors)} ({w1_errors[0] if w1_errors else 'None'})\n"
    )

    # -------------------------------------------------------------
    # Test B: w: "majority", j: True streaming writes during abrupt stepdown
    # -------------------------------------------------------------
    console.print(f"[bold green][*] Sub-phase 2B: Streaming writes under w: 'majority' to primary ({new_primary_name})...[/]")

    p_client_maj = get_direct_client(new_primary_port, timeout_ms=5000)
    db_maj = p_client_maj.get_database("lab_db", write_concern=WriteConcern(w="majority", j=True))
    db_maj.maj_failover.drop()

    maj_attempted: list[int] = []
    maj_acknowledged: list[int] = []
    maj_errors: list[str] = []
    stop_event_maj = threading.Event()

    def maj_worker() -> None:
        seq = 0
        while not stop_event_maj.is_set():
            maj_attempted.append(seq)
            try:
                db_maj.maj_failover.insert_one({"seq": seq, "ts": time.time(), "payload": "x" * 200})
                maj_acknowledged.append(seq)
                seq += 1
                time.sleep(0.002)
            except (NotPrimaryError, AutoReconnect, PyMongoError) as e:
                maj_errors.append(type(e).__name__)
                break

    t_maj = threading.Thread(target=maj_worker, daemon=True)
    t_maj.start()
    time.sleep(0.05)

    console.print(f"[bold red][!] Triggering abrupt primary stepdown on {new_primary_name} (secondaryCatchUp=0s, force=True)...[/]")
    try:
        p_client_maj.admin.command("replSetStepDown", 15, secondaryCatchUpPeriodSecs=0, force=True)
    except Exception:
        pass

    stop_event_maj.set()
    t_maj.join(timeout=3.0)

    console.print("[dim]Waiting for election of next PRIMARY...[/]")
    time.sleep(4.0)

    final_primary_name, final_primary_port = find_primary(cluster)
    console.print(f"[bold cyan][✔] Failover Complete: Next PRIMARY elected: [bold white]{final_primary_name}[/][/]")

    final_p_client = get_direct_client(final_primary_port, timeout_ms=5000)
    maj_stored = final_p_client.lab_db.maj_failover.count_documents({})
    maj_attempted_count = len(maj_attempted)
    maj_ack_count = len(maj_acknowledged)
    maj_dropped_unacked = maj_attempted_count - maj_ack_count
    maj_lost_acked = max(0, maj_ack_count - maj_stored)

    console.print(
        f"    Attempted: {maj_attempted_count}  |  Acknowledged: {maj_ack_count}  |  "
        f"Stored on New Primary: {maj_stored}  |  "
        f"Rejected In-Flight: {maj_dropped_unacked}  |  "
        f"Errors: {len(maj_errors)} ({maj_errors[0] if maj_errors else 'None'})\n"
    )

    # -------------------------------------------------------------
    # Comparison Table
    # -------------------------------------------------------------
    comp_table = Table(
        box=box.ROUNDED,
        title="[bold yellow]Durability Comparison Under Sudden Failover[/]",
        show_header=True,
        header_style="bold cyan",
    )
    comp_table.add_column("Write Concern", style="bold white", width=22)
    comp_table.add_column("Attempted", justify="right", width=12)
    comp_table.add_column("Acknowledged", justify="right", width=14)
    comp_table.add_column("Stored on New Primary", justify="right", width=22)
    comp_table.add_column("Lost Acknowledged", justify="right", width=18)
    comp_table.add_column("Durability Guarantee", justify="center", width=22)

    comp_table.add_row(
        "w: 1 (Local Memory)",
        str(w1_attempted_count),
        str(w1_ack_count),
        str(w1_stored),
        f"[bold yellow]{w1_lost_acked}[/]" if w1_lost_acked == 0 else f"[bold red]{w1_lost_acked}[/]",
        "[yellow]Vulnerable to Rollback[/]",
    )
    comp_table.add_row(
        "w: 'majority', j: True",
        str(maj_attempted_count),
        str(maj_ack_count),
        str(maj_stored),
        f"[bold green]{maj_lost_acked} (0.0% loss)[/]",
        "[bold green]100% Durable (Committed)[/]",
    )

    console.print(comp_table)
    console.print()
    verify_phase(
        w1_ack_count > 0 and new_primary_name != primary_name,
        "Phase 2A: w=1 Stepdown Failover",
        f"Expected w1_ack > 0 and primary change ({primary_name} -> {new_primary_name})",
    )
    verify_phase(
        maj_ack_count > 0 and final_primary_name != new_primary_name and maj_lost_acked == 0,
        "Phase 2B: Majority Write Durability",
        f"Expected maj_ack > 0, primary change, and 0 lost majority writes; got ack={maj_ack_count}, lost={maj_lost_acked}",
    )
    console.print("[bold green]✔ Finding: 'w: majority' guarantees that all acknowledged writes survive elections without data loss.[/]")


def phase3_oplog_exhaustion(cluster: MongoClient) -> None:
    """Phase 3: Oplog Window Exhaustion & Stale Secondary State."""
    console.print("\n" + "=" * 76, style="bold cyan")
    console.print("[bold green]Phase 3: Capped Oplog Exhaustion & Stale Secondary Recovery[/]")
    console.print("=" * 76 + "\n", style="bold cyan")

    console.print(
        "[bold white]Objective:[/] Demonstrate that when a secondary falls behind the primary's\n"
        "capped oplog retention window (10MB limit), it cannot resume replication and\n"
        "transitions to [bold red]RECOVERING[/] state, requiring an initial sync.\n"
    )

    # Ensure mongo-node3 is a SECONDARY
    primary_name, primary_port = find_primary(cluster)
    if "node3" in primary_name:
        console.print("[bold yellow][*] mongo-node3 is currently primary; stepping down to ensure it is secondary...[/]")
        c3 = get_direct_client(27019, timeout_ms=5000)
        try:
            c3.admin.command("replSetStepDown", 60, secondaryCatchUpPeriodSecs=0, force=True)
        except Exception:
            pass
        time.sleep(4.0)
        primary_name, primary_port = find_primary(cluster)

    # Record node 3 optime before pausing
    status = cluster.admin.command("replSetGetStatus")
    node3_m = next(m for m in status.get("members", []) if "node3" in m["name"])
    node3_ts = node3_m["optime"]["ts"]
    console.print(f"[bold cyan][*] Primary is [bold white]{primary_name}[/]; mongo-node3 optime timestamp: [magenta]{node3_ts}[/][/]")

    # Pause secondary node 3
    console.print("[bold yellow][*] Pausing container 'mongo-node3' (docker pause)...[/]")
    subprocess.run(["docker", "pause", "mongo-node3"], check=True)
    paused = True

    try:
        db = cluster.get_database("lab_db", write_concern=WriteConcern(w="majority"))
        db.oplog_churn.drop()

        payload_prefix = os.urandom(2000).hex()
        total = 0

        console.print("[bold white][*] Writing burst of updates to primary to churn oplog beyond 10MB capacity...[/]")

        for batch in range(1, 450):
            for doc_id in range(10):
                for _ in range(5):
                    try:
                        db.oplog_churn.update_one(
                            {"_id": doc_id},
                            {"$set": {"data": f"{payload_prefix}_{batch}_{doc_id}_{os.urandom(100).hex()}"}},
                            upsert=True,
                        )
                        total += 1
                        break
                    except (AutoReconnect, NotPrimaryError, PyMongoError):
                        time.sleep(0.5)

            time.sleep(0.01)

            if batch % 5 == 0:
                try:
                    cur_primary, cur_port = find_primary(cluster)
                    with get_direct_client(cur_port, timeout_ms=5000) as cur_p_client:
                        cur_oplog = cur_p_client.local["oplog.rs"]
                        first_oplog = next(cur_oplog.find().sort("$natural", 1).limit(1))
                        stats = cur_p_client.local.command("collStats", "oplog.rs")
                        size_mb = stats.get("size", 0) / (1024 * 1024)
                        oldest_ts = first_oplog["ts"]

                        plain_console.print(f"    [Batch {batch:3d}] Churned {total:4d} ops | Oplog Size: {size_mb:.2f}MB | Oldest Oplog TS: {oldest_ts}")
                        sys.stdout.flush()

                        if oldest_ts > node3_ts:
                            console.print(
                                f"\n[bold green][✔] Oplog Window Exceeded: Oldest oplog entry ({oldest_ts}) "
                                f"has advanced beyond node3's paused timestamp ({node3_ts})![/]"
                            )
                            break
                except Exception:
                    pass

        cur_primary, cur_port = find_primary(cluster)
        with get_direct_client(cur_port, timeout_ms=5000) as cur_p_client:
            first_oplog = next(cur_p_client.local["oplog.rs"].find().sort("$natural", 1).limit(1))
            oldest_ts = first_oplog["ts"]

        # Unpause secondary node 3
        console.print("\n[bold yellow][*] Unpausing container 'mongo-node3' (docker unpause)...[/]")
        subprocess.run(["docker", "unpause", "mongo-node3"], check=True)
        paused = False
    finally:
        if paused:
            subprocess.run(["docker", "unpause", "mongo-node3"], check=False)

    console.print("[bold white][*] Polling replica set status as mongo-node3 discovers oplog gap...[/]\n")

    stale_detected = False
    for attempt in range(1, 15):
        time.sleep(1.0)
        try:
            status = cluster.admin.command("replSetGetStatus")
            node3_info = next(m for m in status.get("members", []) if "node3" in m["name"])
            state_str = node3_info.get("stateStr", "UNKNOWN")
            msg = str(node3_info.get("lastHeartbeatMessage", ""))
            sync_source = node3_info.get("syncSourceHost", "none")

            try:
                with get_direct_client(27019, timeout_ms=2000) as c3:
                    s3 = c3.admin.command("replSetGetStatus")
                    s3_my_state = s3.get("myState")
                    if s3_my_state == 3:
                        state_str = "RECOVERING"
            except Exception:
                pass

            plain_console.print(f"    Poll {attempt:2d}: mongo-node3 state = {state_str:<10} | syncSource = {sync_source:<18} | heartbeatMsg = {msg}")
            sys.stdout.flush()

            if state_str == "RECOVERING" or "too stale" in msg.lower() or "cannot select sync source" in msg.lower() or "behind" in msg.lower():
                stale_detected = True
                break
        except Exception:
            pass

    # Results Table
    console.print()
    res_table = Table(
        box=box.ROUNDED,
        title="[bold yellow]Oplog Window Exhaustion Outcome[/]",
        show_header=True,
        header_style="bold cyan",
    )
    res_table.add_column("Node", style="bold white", width=14)
    res_table.add_column("Role / State", justify="center", width=14)
    res_table.add_column("Node Optime", justify="center", width=22)
    res_table.add_column("Primary Oldest Oplog", justify="center", width=22)
    res_table.add_column("Oplog Gap Status", justify="center", width=26)

    res_table.add_row(
        "mongo-node3",
        "[bold red]RECOVERING[/]" if (stale_detected or oldest_ts > node3_ts) else "[bold blue]SECONDARY[/]",
        str(node3_ts),
        str(oldest_ts),
        "[bold red]Fell Off Oplog (Stale)[/]" if oldest_ts > node3_ts else "[green]In Window[/]",
    )
    console.print(res_table)
    console.print()
    verify_phase(
        oldest_ts > node3_ts and stale_detected,
        "Phase 3: Oplog Exhaustion",
        f"Expected oldest_ts ({oldest_ts}) > node3_ts ({node3_ts}) and stale/RECOVERING state, got stale_detected={stale_detected}",
    )
    console.print(
        "[bold green]✔ Finding: When oplog retention is exceeded, disconnected secondaries cannot resume replication and enter RECOVERING, requiring full initial sync.[/]"
    )


def print_takeaways() -> None:
    """Prints systems engineering and production takeaways."""
    console.print("\n" + "=" * 76, style="bold cyan")
    console.print("[bold green]Production & Systems Engineering Takeaways[/]")
    console.print("=" * 76 + "\n", style="bold cyan")

    takeaways = [
        (
            "1. Durability Requires Majority Consensus (w: 'majority')",
            "Standalone MongoDB defaults or 'w: 1' only guarantee persistence on the single primary node. "
            "Under sudden stepdown or network partition, in-flight or un-replicated writes are rolled back. "
            "Always use 'w: majority' and enable retryable writes ('retryWrites=true') for mission-critical writes.",
        ),
        (
            "2. Election Quorum & Raft-Style Terms",
            "A 3-node cluster requires 2 healthy voting nodes (majority = floor(N/2) + 1). "
            "If 2 nodes become unreachable, the remaining 1 node cannot maintain PRIMARY and steps down to SECONDARY. "
            "Stepdowns increment the election term, invalidating stale primaries instantly.",
        ),
        (
            "3. Oplog Sizing is a High-Availability SLA",
            "The oplog acts as a rolling buffer of idempotent operations. "
            "If a secondary is disconnected (network partition, maintenance reboot, backup snapshot) "
            "longer than the oplog retention window, it 'falls off the oplog' and enters RECOVERING. "
            "Recovering from a stale state requires a costly, I/O-intensive Initial Sync.",
        ),
    ]

    for title, desc in takeaways:
        console.print(f"[bold cyan]{title}[/]")
        console.print(f"  {desc}\n")


def main() -> None:
    console.print("\n" + "=" * 76, style="bold cyan")
    console.print("[bold yellow]MongoDB Experiment 04: 3-Node Replica Set Durability & Oplog Lab[/]")
    console.print("=" * 76 + "\n", style="bold cyan")

    cluster, status = ensure_replica_set_ready()

    phase1_topology(status)
    phase2_durability_comparison(cluster)
    phase3_oplog_exhaustion(cluster)
    print_takeaways()


if __name__ == "__main__":
    main()

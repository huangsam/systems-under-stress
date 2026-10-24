# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
#     "cassandra-driver>=3.29.0",
# ]
# ///
"""
Experiment 02: Tunable Consistency & PACELC in Practice (3-Node Mini-Cluster)

Demonstrates the PACELC theorem and Cassandra's tunable consistency levels:
ConsistencyLevel.ONE vs QUORUM vs ALL under node failure scenarios.

Cluster Setup:
- 3 Cassandra Nodes: cassandra-node1 (seed), cassandra-node2, cassandra-node3.
- Keyspace: lab_consistency (Replication Factor RF = 3).
- Quorum math for RF=3: floor(RF / 2) + 1 = 2 nodes required.

Scenarios tested:
1. Baseline (3/3 nodes alive):
   - ONE (req 1): SUCCESS
   - QUORUM (req 2): SUCCESS
   - ALL (req 3): SUCCESS
2. 1 Node Down (2/3 nodes alive - cassandra-node3 stopped):
   - ONE (req 1): SUCCESS
   - QUORUM (req 2): SUCCESS
   - ALL (req 3): FAILS with UnavailableException
3. 2 Nodes Down (1/3 nodes alive - cassandra-node2 stopped):
   - ONE (req 1): SUCCESS (Availability preserved)
   - QUORUM (req 2): FAILS with UnavailableException (Consistency prioritized)
   - ALL (req 3): FAILS with UnavailableException
4. Cluster Healing:
   - Restart nodes and verify full consistency recovery.
"""

from __future__ import annotations

import datetime
import subprocess
import sys
import time
from pathlib import Path

from cassandra.cluster import EXEC_PROFILE_DEFAULT, Cluster, ExecutionProfile
from cassandra.policies import WhiteListRoundRobinPolicy
from cassandra.query import SimpleStatement
from rich.console import Console

from cassandra import ConsistencyLevel, Unavailable

COMPOSE_DIR = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 9042
KEYSPACE = "lab_consistency"
TABLE = "accounts"


console = Console()


def log(msg: str, color: str = "[/]") -> None:
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{timestamp}][/] {color}{msg}[/]", flush=True)


def docker_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["docker"] + args
    return subprocess.run(cmd, cwd=COMPOSE_DIR, capture_output=True, text=True)


def wait_for_coordinator() -> Cluster:
    log("Connecting to coordinator cassandra-node1 on 127.0.0.1:9042...", "[bold cyan]")
    profile = ExecutionProfile(
        request_timeout=15.0,
        load_balancing_policy=WhiteListRoundRobinPolicy([HOST]),
    )
    for attempt in range(1, 35):
        try:
            cluster = Cluster(
                contact_points=[HOST],
                port=PORT,
                execution_profiles={EXEC_PROFILE_DEFAULT: profile},
                connect_timeout=5.0,
            )
            session = cluster.connect()
            session.execute("SELECT release_version FROM system.local")
            log(f"Connected to Cassandra coordinator successfully (attempt {attempt}).", "[bold green]")
            return cluster
        except Exception:
            time.sleep(2)
    log("Failed to connect to Cassandra. Make sure 'make up EXP=cluster-consistency' is running.", "[bold red]")
    sys.exit(1)


def setup_cluster_schema(session) -> None:
    log(f"Setting up keyspace '{KEYSPACE}' with RF=3 and table '{TABLE}'...", "[bold cyan]")
    session.execute(f"""
        CREATE KEYSPACE IF NOT EXISTS {KEYSPACE}
        WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': 3}};
    """)

    session.execute(f"""
        CREATE TABLE IF NOT EXISTS {KEYSPACE}.{TABLE} (
            id int PRIMARY KEY,
            owner text,
            balance double
        );
    """)

    session.execute(f"TRUNCATE {KEYSPACE}.{TABLE};")
    log("Keyspace and table ready with RF=3.", "[green]")


def test_write_read(session, cl: ConsistencyLevel, account_id: int, label: str) -> tuple[bool, float, str]:
    """Tests an INSERT and SELECT at the specified ConsistencyLevel."""
    insert_stmt = SimpleStatement(
        f"INSERT INTO {KEYSPACE}.{TABLE} (id, owner, balance) VALUES (%s, %s, %s)",
        consistency_level=cl,
    )
    select_stmt = SimpleStatement(
        f"SELECT * FROM {KEYSPACE}.{TABLE} WHERE id = %s",
        consistency_level=cl,
    )

    t0 = time.perf_counter()
    try:
        session.execute(insert_stmt, (account_id, f"user_{account_id}", 1000.0 + account_id))
        rows = list(session.execute(select_stmt, (account_id,)))
        t1 = time.perf_counter()
        elapsed_ms = (t1 - t0) * 1000
        if rows:
            return True, elapsed_ms, f"OK ({elapsed_ms:.2f}ms)"
        return False, elapsed_ms, "No rows returned"
    except Unavailable as exc:
        t1 = time.perf_counter()
        elapsed_ms = (t1 - t0) * 1000
        alive = getattr(exc, "alive_replicas", "?")
        required = getattr(exc, "required_replicas", "?")
        return False, elapsed_ms, f"Unavailable (alive: {alive}, required: {required})"
    except Exception as exc:
        t1 = time.perf_counter()
        elapsed_ms = (t1 - t0) * 1000
        return False, elapsed_ms, f"Failed: {type(exc).__name__}"


def evaluate_levels(session, scenario_name: str, base_id: int) -> dict[str, str]:
    levels = [
        ("ONE", ConsistencyLevel.ONE),
        ("QUORUM", ConsistencyLevel.QUORUM),
        ("ALL", ConsistencyLevel.ALL),
    ]
    results = {}
    print(f"\n--- Testing Scenario: {scenario_name} ---")
    for name, cl in levels:
        success, elapsed, msg = test_write_read(session, cl, base_id, name)
        status = "[green][PASS][/]" if success else "[red][FAIL][/]"
        results[name] = "PASS" if success else "FAIL"
        print(f"  {name:7s} | {status} {msg}")
        base_id += 1
    return results


def run_experiment() -> None:
    print("\n" + "=" * 70)
    print("  EXPERIMENT 02: Tunable Consistency & PACELC in Practice  ")
    print("  3-Node Mini-Cluster (RF = 3: node1, node2, node3)       ")
    print("=" * 70 + "\n")

    cluster = wait_for_coordinator()
    session = cluster.connect()

    matrix = []

    try:
        setup_cluster_schema(session)

        # ---------------------------------------------------------
        # Scenario 1: All 3 Nodes UP (Healthy Baseline)
        # ---------------------------------------------------------
        log("Phase 1: Testing baseline consistency with 3/3 nodes healthy...", "[bold cyan]")
        res1 = evaluate_levels(session, "3 Nodes Alive (node1, node2, node3)", 100)
        matrix.append(("3 Nodes Alive (Healthy)", res1["ONE"], res1["QUORUM"], res1["ALL"]))

        # ---------------------------------------------------------
        # Scenario 2: Stop 1 Node (cassandra-node3)
        # ---------------------------------------------------------
        log("\nPhase 2: Stopping cassandra-node3 (2 of 3 nodes remain)...", "[bold yellow]")
        docker_cmd(["stop", "cassandra-node3"])
        time.sleep(4)  # Allow gossip failure detector to mark node3 DOWN

        res2 = evaluate_levels(session, "2 Nodes Alive (node3 DOWN)", 200)
        matrix.append(("2 Nodes Alive (node3 DOWN)", res2["ONE"], res2["QUORUM"], res2["ALL"]))

        # ---------------------------------------------------------
        # Scenario 3: Stop 2nd Node (cassandra-node2)
        # ---------------------------------------------------------
        log("\nPhase 3: Stopping cassandra-node2 (Only 1 of 3 nodes remains)...", "[bold red]")
        docker_cmd(["stop", "cassandra-node2"])
        time.sleep(4)  # Allow gossip failure detector to mark node2 DOWN

        res3 = evaluate_levels(session, "1 Node Alive (node2 & node3 DOWN)", 300)
        matrix.append(("1 Node Alive (node2/3 DOWN)", res3["ONE"], res3["QUORUM"], res3["ALL"]))

        # ---------------------------------------------------------
        # Scenario 4: Healing & Recovery
        # ---------------------------------------------------------
        log("\nPhase 4: Restarting cassandra-node2 and cassandra-node3...", "[bold blue]")
        docker_cmd(["start", "cassandra-node2"])
        docker_cmd(["start", "cassandra-node3"])

        log("Waiting for nodes to rejoin gossip ring...", "[dim]")
        for _ in range(15):
            time.sleep(3)
            # Test if ALL can pass again
            success, _, _ = test_write_read(session, ConsistencyLevel.ALL, 999, "ALL")
            if success:
                log("Cluster fully recovered! All 3 replicas responding.", "[bold green]")
                break

        res4 = evaluate_levels(session, "3 Nodes Alive (After Healing)", 400)
        matrix.append(("Cluster Healed (3 Nodes)", res4["ONE"], res4["QUORUM"], res4["ALL"]))

        # ---------------------------------------------------------
        # PACELC Trade-Off Matrix Display
        # ---------------------------------------------------------
        print("\n" + "=" * 70)
        print("  PACELC AVAILABILITY MATRIX UNDER NODE DEGRADATION")
        print("=" * 70)
        print(f"  {'Cluster State':<30} | {'ONE (req 1)':<12} | {'QUORUM (req 2)':<14} | {'ALL (req 3)':<12}")
        print("  " + "-" * 66)
        for state, one_res, q_res, all_res in matrix:
            c_one = f"[green]{one_res}[/]" if one_res == "PASS" else f"[red]{one_res}[/]"
            c_q = f"[green]{q_res}[/]" if q_res == "PASS" else f"[red]{q_res}[/]"
            c_all = f"[green]{all_res}[/]" if all_res == "PASS" else f"[red]{all_res}[/]"
            print(f"  {state:<30} | {c_one:<21} | {c_q:<23} | {c_all:<21}")
        print("=" * 70 + "\n")

        console.print("[bold]PACELC & Cassandra Distributed Systems Takeaways:[/]")
        print("1. In PACELC: if Partitioned (P): trade-off between Availability (A) and Consistency (C).")
        print("2. When 1 node dies (2 alive):")
        print("   - QUORUM still succeeds because floor(3/2) + 1 = 2 live replicas.")
        print("   - ALL fails immediately with UnavailableException (cannot satisfy 3/3).")
        print("3. When 2 nodes die (1 alive):")
        print("   - QUORUM fails (needs 2, only 1 available). Prioritizes Consistency over Availability.")
        print("   - ONE succeeds. Prioritizes Availability over Consistency (risk of stale reads).")
        print("4. Strong Consistency Formula: Read Consistency + Write Consistency > Replication Factor.")
        print("   - QUORUM Write (2) + QUORUM Read (2) = 4 > 3 (RF). Guarantees strong consistency (Pigeonhole principle).")
        print("   - ONE Write (1) + ONE Read (1) = 2 <= 3. Eventual consistency only.")
        print()

    finally:
        session.shutdown()
        cluster.shutdown()


if __name__ == "__main__":
    run_experiment()

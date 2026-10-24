# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "neo4j>=5.15",
# ]
# ///
"""
Experiment 02: Dense Supernodes & Lock Contention in Neo4j

Demonstrates:
1. Pointer-Chasing Traversal Degradation:
   - In Neo4j's storage engine, relationships are stored as doubly-linked record chains
     originating at each node record.
   - When traversing through a dense "Supernode" (50,000 edges), the engine must scan
     the long linked list of relationship pointers, resulting in massive DB hits and
     CPU cache misses.
   - Compares read traversal through a Supernode (50,000 edges) vs a Sparse Node (10 edges).
2. Write Lock Contention on Hub Nodes:
   - Creating an edge requires Neo4j to acquire an exclusive lock on both endpoints to
     update their relationship head pointers.
   - When concurrent transactions write relationships to the same supernode, all
     transactions block and serialize on the supernode's exclusive lock.
   - Contrasts concurrent writes targeting a single supernode vs partitioned target nodes.
"""

from __future__ import annotations

import concurrent.futures
import statistics
import sys
import time
from typing import Any

from rich.console import Console

from neo4j import Driver, GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "password123")


console = Console()
err_console = Console(stderr=True)


def get_driver() -> Driver:
    return GraphDatabase.driver(URI, auth=AUTH)


def wait_for_neo4j(driver: Driver, timeout_sec: int = 45) -> None:
    start = time.time()
    print("[*] Connecting to Neo4j at bolt://127.0.0.1:7687...")
    while time.time() - start < timeout_sec:
        try:
            with driver.session() as session:
                result = session.run("RETURN 1 AS online")
                if result.single()["online"] == 1:
                    console.print("[[bold green]+[/]] Neo4j is online and accepting Bolt connections.")
                    return
        except Exception:
            time.sleep(1.5)
    raise TimeoutError(f"Neo4j did not become ready within {timeout_sec} seconds.")


def extract_total_db_hits(plan: Any) -> int:
    if plan is None:
        return 0
    total = getattr(plan, "db_hits", 0) or 0
    for child in getattr(plan, "children", []):
        total += extract_total_db_hits(child)
    return total


def reset_and_seed_supernode(driver: Driver, supernode_edges: int = 50000, sparse_edges: int = 10) -> None:
    console.print(f"\n[bold blue][*] Seeding Supernode ({supernode_edges:,} edges) and Sparse Node ({sparse_edges} edges)...[/]")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")

        session.run("CREATE CONSTRAINT hub_id_uniq IF NOT EXISTS FOR (h:Hub) REQUIRE h.id IS UNIQUE")
        session.run("CREATE CONSTRAINT follower_id_uniq IF NOT EXISTS FOR (f:Follower) REQUIRE f.id IS UNIQUE")

        # Create Hub nodes
        session.run(
            """
            CREATE (:Hub {id: 'supernode', name: 'Global Celebrity', type: 'dense'}),
                   (:Hub {id: 'sparse_node', name: 'Regular User', type: 'sparse'})
            """
        )

        # Batch seed 50,000 edges to supernode in chunks of 10,000
        batch_size = 10000
        batches = supernode_edges // batch_size
        for b in range(batches):
            start_idx = b * batch_size
            end_idx = (b + 1) * batch_size - 1
            session.run(
                """
                MATCH (s:Hub {id: 'supernode'})
                UNWIND range($start_idx, $end_idx) AS i
                CREATE (:Follower {id: i, name: 'Follower_' + toString(i)})-[:FOLLOWS]->(s)
                """,
                {"start_idx": start_idx, "end_idx": end_idx},
            )
            print(f"    -> Inserted {(b + 1) * batch_size:,} / {supernode_edges:,} supernode edges...")

        # Seed sparse node edges
        session.run(
            """
            MATCH (s:Hub {id: 'sparse_node'})
            UNWIND range(1, $sparse_edges) AS i
            CREATE (:Follower {id: 50000 + i, name: 'SparseFollower_' + toString(i)})-[:FOLLOWS]->(s)
            """,
            {"sparse_edges": sparse_edges},
        )

        super_deg = session.run("MATCH (:Hub {id: 'supernode'})<-[r:FOLLOWS]-() RETURN count(r) AS cnt").single()["cnt"]
        sparse_deg = session.run("MATCH (:Hub {id: 'sparse_node'})<-[r:FOLLOWS]-() RETURN count(r) AS cnt").single()["cnt"]
        console.print(f"[[bold green]+[/]] Supernode degree: {super_deg:,} edges | Sparse node degree: {sparse_deg} edges.")


def test_traversal_degradation(driver: Driver) -> None:
    console.print("\n[bold blue][*] Part 1: Read Traversal Degradation (Pointer-Chasing in Relationship Chains)...[/]")

    with driver.session() as session:
        # 1. Query through Sparse Node (degree = 10)
        query_sparse = "PROFILE MATCH (h:Hub {id: 'sparse_node'})<-[:FOLLOWS]-(f:Follower) WHERE f.id = 50005 RETURN f.name AS name"
        t0 = time.perf_counter()
        res_sparse = session.run(query_sparse)
        _rec_sparse = res_sparse.single()
        sum_sparse = res_sparse.consume()
        wall_sparse_ms = (time.perf_counter() - t0) * 1000.0
        hits_sparse = extract_total_db_hits(sum_sparse.profile)

        # 2. Query through Supernode (degree = 50,000)
        # Filters for a leaf near the tail of the relationship linked list
        query_super = "PROFILE MATCH (h:Hub {id: 'supernode'})<-[:FOLLOWS]-(f:Follower) WHERE f.id = 49995 RETURN f.name AS name"
        t1 = time.perf_counter()
        res_super = session.run(query_super)
        _rec_super = res_super.single()
        sum_super = res_super.consume()
        wall_super_ms = (time.perf_counter() - t1) * 1000.0
        hits_super = extract_total_db_hits(sum_super.profile)

        print(f"{'Target Node Type':<22}{'Degree':<14}{'DB Hits':<14}{'Server Time (ms)':<18}{'Wall Time (ms)':<14}")
        print("-" * 82)
        server_sparse_ms = sum_sparse.result_available_after + sum_sparse.result_consumed_after
        server_super_ms = sum_super.result_available_after + sum_super.result_consumed_after

        print(f"{'Sparse Node':<22}{'10':<14}{hits_sparse:<14,}{server_sparse_ms:<18.2f}{wall_sparse_ms:<14.2f}")
        print(f"{'Dense Supernode':<22}{'50,000':<14}{hits_super:<14,}{server_super_ms:<18.2f}{wall_super_ms:<14.2f}")

        if hits_sparse > 0:
            hits_ratio = hits_super / hits_sparse
            console.print(f"\n[[bold yellow]![/]] Traversing through the Supernode required [bold red]{hits_ratio:,.0f}x[/] more DB hits")
            print("    due to sequentially traversing the 50,000-pointer relationship record chain.")


def test_lock_contention(driver: Driver, num_workers: int = 10, writes_per_worker: int = 25) -> None:
    console.print(f"\n[bold blue][*] Part 2: Concurrent Write Lock Contention ({num_workers} threads, {writes_per_worker} writes/thread)...[/]")

    # Setup partition nodes for uncontended test
    with driver.session() as session:
        session.run(
            """
            UNWIND range(0, $num_workers - 1) AS wid
            MERGE (:Hub {id: 'partition_' + toString(wid), name: 'Partition Hub ' + toString(wid), type: 'partition'})
            """,
            {"num_workers": num_workers},
        )

    # 1. Contended writes: All threads attach edges to the SAME supernode
    def worker_contended(worker_id: int) -> list[float]:
        latencies = []
        with driver.session() as s:
            for i in range(writes_per_worker):
                uid = f"c_{worker_id}_{i}_{time.time_ns()}"
                t_start = time.perf_counter()
                s.run(
                    """
                    MATCH (h:Hub {id: 'supernode'})
                    CREATE (:WriterNode {id: $uid})-[:FOLLOWS]->(h)
                    """,
                    {"uid": uid},
                )
                latencies.append((time.perf_counter() - t_start) * 1000.0)
        return latencies

    print("  -> Running Workload A: High Contention (All threads locking the Supernode)...")
    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(worker_contended, wid) for wid in range(num_workers)]
        contended_results = [f.result() for f in concurrent.futures.as_completed(futures)]
    wall_contended_sec = time.perf_counter() - wall_start

    all_contended_latencies = [lat for worker_lats in contended_results for lat in worker_lats]

    # 2. Uncontended writes: Each thread attaches edges to its OWN partition hub
    def worker_uncontended(worker_id: int) -> list[float]:
        latencies = []
        with driver.session() as s:
            target_hub = f"partition_{worker_id}"
            for i in range(writes_per_worker):
                uid = f"u_{worker_id}_{i}_{time.time_ns()}"
                t_start = time.perf_counter()
                s.run(
                    """
                    MATCH (h:Hub {id: $target_hub})
                    CREATE (:WriterNode {id: $uid})-[:FOLLOWS]->(h)
                    """,
                    {"uid": uid, "target_hub": target_hub},
                )
                latencies.append((time.perf_counter() - t_start) * 1000.0)
        return latencies

    print("  -> Running Workload B: Zero Contention (Each thread locking independent hubs)...")
    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(worker_uncontended, wid) for wid in range(num_workers)]
        uncontended_results = [f.result() for f in concurrent.futures.as_completed(futures)]
    wall_uncontended_sec = time.perf_counter() - wall_start

    all_uncontended_latencies = [lat for worker_lats in uncontended_results for lat in worker_lats]

    total_writes = num_workers * writes_per_worker
    throughput_contended = total_writes / wall_contended_sec
    throughput_uncontended = total_writes / wall_uncontended_sec

    p50_c = statistics.median(all_contended_latencies)
    p95_c = statistics.quantiles(all_contended_latencies, n=20)[18]  # 95th percentile
    max_c = max(all_contended_latencies)

    p50_u = statistics.median(all_uncontended_latencies)
    p95_u = statistics.quantiles(all_uncontended_latencies, n=20)[18]
    max_u = max(all_uncontended_latencies)

    print("\n" + "=" * 82)
    print(f"{'Metric':<30}{'Supernode Contention':<26}{'Partitioned Hubs':<26}")
    print("=" * 82)
    print(f"{'Total Duration':<30}{f'{wall_contended_sec:.2f} s':<26}{f'{wall_uncontended_sec:.2f} s':<26}")
    print(f"{'Throughput':<30}{f'{throughput_contended:.1f} writes/s':<26}{f'{throughput_uncontended:.1f} writes/s':<26}")
    print(f"{'P50 Latency':<30}{f'{p50_c:.2f} ms':<26}{f'{p50_u:.2f} ms':<26}")
    print(f"{'P95 Latency':<30}{f'{p95_c:.2f} ms':<26}{f'{p95_u:.2f} ms':<26}")
    print(f"{'Max Latency':<30}{f'{max_c:.2f} ms':<26}{f'{max_u:.2f} ms':<26}")
    print("=" * 82)

    latency_ratio = p95_c / p95_u if p95_u > 0 else 1.0
    console.print(f"\n[[bold yellow]![/]] Supernode exclusive lock serialization increased P95 write latency by [bold red]{latency_ratio:.1f}x[/].")


def main() -> None:
    console.print("[bold cyan]========================================================================[/]")
    console.print("[bold cyan] Neo4j Limit Lab: Experiment 02 - Supernode Degradation & Lock Contention[/]")
    console.print("[bold cyan]========================================================================[/]")

    driver = get_driver()
    try:
        wait_for_neo4j(driver)
        reset_and_seed_supernode(driver, supernode_edges=50000, sparse_edges=10)
        test_traversal_degradation(driver)
        test_lock_contention(driver, num_workers=10, writes_per_worker=25)
        console.print("\n[bold green][✓] Experiment 02 completed successfully.[/]")
    except Exception as e:
        err_console.print(f"\n[bold red][!] Error running experiment:[/] {e}")
        sys.exit(1)
    finally:
        driver.close()


if __name__ == "__main__":
    main()

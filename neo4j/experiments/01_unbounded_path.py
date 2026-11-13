# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "neo4j>=5.15",
# ]
# ///
"""
Experiment 01: Combinatorial Path Explosion & Unbounded Graph Traversal in Neo4j

Demonstrates:
1. Exponential Combinatorial Explosion:
   - Evaluates variable-length path traversal queries: MATCH (a)-[*1..n]->(b)
   - As traversal depth increases from 1 to 6 hops on a graph with branching factor ~4,
     path evaluations and database operations scale exponentially O(b^d).
2. Index-Free Adjacency Mechanics:
   - Neo4j traverses relationships via direct record pointers in memory/page cache.
   - While each individual pointer dereference is microsecond-fast, the total number of
     paths explodes combinatorially, leading to millions of DB hits and query timeouts.
3. Mitigation via ShortestPath:
   - Contrasts exhaustive variable-length matching against Neo4j's bidirectional BFS
     shortestPath((a)-[*]-(b)), demonstrating orders-of-magnitude reduction in DB hits.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from rich.console import Console
from rich.markup import escape

from neo4j import Driver, GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "password123")


console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)
err_console = Console(stderr=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def get_driver() -> Driver:
    return GraphDatabase.driver(URI, auth=AUTH)


def wait_for_neo4j(driver: Driver, timeout_sec: int = 45) -> None:
    """Waits until Neo4j is responsive and ready for Cypher queries."""
    start = time.time()
    plain_console.print("[*] Connecting to Neo4j at bolt://127.0.0.1:7687...")
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
    """Recursively computes total db_hits across all operators in a profiled Cypher plan."""
    if plan is None:
        return 0
    if isinstance(plan, dict):
        args = plan.get("args") or {}
        total = int(plan.get("dbHits") or plan.get("db_hits") or args.get("DbHits") or 0)
        for child in plan.get("children") or []:
            total += extract_total_db_hits(child)
        return total
    total = int(getattr(plan, "db_hits", 0) or 0)
    for child in getattr(plan, "children", []):
        total += extract_total_db_hits(child)
    return total


def reset_and_seed_graph(driver: Driver, num_nodes: int = 2000, branching: int = 4) -> None:
    """Resets the database and seeds an interconnected synthetic network."""
    console.print(f"\n[bold blue][*] Seeding graph with {num_nodes} nodes (branching factor = {branching})...[/]")
    with driver.session() as session:
        # Fast reset
        session.run("MATCH (n) DETACH DELETE n")

        # Create unique constraint on Node(id)
        session.run("CREATE CONSTRAINT node_id_uniq IF NOT EXISTS FOR (n:Node) REQUIRE n.id IS UNIQUE")
        session.run("CALL db.awaitIndexes()")

        # Batch create nodes
        session.run(
            """
            UNWIND range(0, $num_nodes - 1) AS node_id
            CREATE (:Node {id: node_id, name: 'Node_' + toString(node_id)})
            """,
            {"num_nodes": num_nodes},
        )

        # Batch create edges with deterministic branching
        # Node i connects forward to (i * branching + 1..branching) % num_nodes
        session.run(
            """
            UNWIND range(0, $num_nodes - 1) AS src_id
            MATCH (src:Node {id: src_id})
            UNWIND range(1, $branching) AS offset
            MATCH (dst:Node {id: (src_id * $branching + offset) % $num_nodes})
            CREATE (src)-[:CONNECTED_TO]->(dst)
            """,
            {"num_nodes": num_nodes, "branching": branching},
        )

        # Verify counts
        node_count = session.run("MATCH (n:Node) RETURN count(n) AS cnt").single()["cnt"]
        rel_count = session.run("MATCH ()-[r:CONNECTED_TO]->() RETURN count(r) AS cnt").single()["cnt"]
        verify_phase(
            node_count == num_nodes and rel_count > 5000,
            "Phase 0 Seed",
            f"expected {num_nodes} nodes and >5000 edges, got {node_count} nodes, {rel_count} edges",
        )
        console.print(f"[[bold green]+[/]] Seeded {node_count:,} nodes and {rel_count:,} relationships.")


def test_depth_explosion(driver: Driver, max_depth: int = 6) -> None:
    """Tests exponential scaling of path traversal from depth 1 to max_depth."""
    console.print("\n[bold blue][*] Part 1: Testing Unbounded Path Traversal MATCH (a)-[*1..d]->(b)...[/]")
    plain_console.print(f"{'Depth':<8}{'Paths Found':<16}{'DB Hits':<14}{'Server Time (ms)':<18}{'Wall Time (ms)':<16}{'Growth':<10}")
    plain_console.print("-" * 82)

    prev_paths = None
    depth1_paths = 0

    with driver.session() as session:
        for depth in range(1, max_depth + 1):
            query = f"PROFILE MATCH p = (a:Node {{id: 0}})-[*1..{depth}]->(b:Node) RETURN count(p) AS path_count"

            wall_start = time.perf_counter()
            result = session.run(query)
            record = result.single()
            summary = result.consume()
            wall_elapsed_ms = (time.perf_counter() - wall_start) * 1000.0

            path_count = record["path_count"]
            db_hits = extract_total_db_hits(summary.profile)
            server_time_ms = summary.result_available_after + summary.result_consumed_after

            if depth == 1:
                depth1_paths = path_count
                verify_phase(depth1_paths > 0, "Phase 1 Depth 1", f"expected >0 paths at depth 1, got {depth1_paths}")

            growth = f"{path_count / prev_paths:.1f}x" if prev_paths and prev_paths > 0 else "-"
            prev_paths = path_count

            plain_console.print(f"{depth:<8}{path_count:<16,}{db_hits:<14,}{server_time_ms:<18.2f}{wall_elapsed_ms:<16.2f}{growth:<10}")

    verify_phase(
        prev_paths is not None and prev_paths > depth1_paths * 100,
        "Phase 1 Depth Explosion",
        f"expected combinatorial growth >100x from depth 1 ({depth1_paths}) to depth {max_depth} ({prev_paths})",
    )


def test_shortest_path_comparison(driver: Driver, target_id: int = 500) -> None:
    """Compares exhaustive variable-length traversal vs shortestPath algorithm."""
    console.print(f"\n[bold blue][*] Part 2: Exhaustive Traversal vs Bidirectional shortestPath (Target Node {target_id})...[/]")

    with driver.session() as session:
        # 1. Exhaustive variable-length traversal (depth 1..6)
        query_exhaustive = "PROFILE MATCH p = (a:Node {id: 0})-[*1..6]->(b:Node {id: $target_id}) RETURN count(p) AS paths"
        t0 = time.perf_counter()
        res_ex = session.run(query_exhaustive, {"target_id": target_id})
        rec_ex = res_ex.single()
        sum_ex = res_ex.consume()
        wall_ex_ms = (time.perf_counter() - t0) * 1000.0
        ex_paths = rec_ex["paths"]
        ex_db_hits = extract_total_db_hits(sum_ex.profile)

        # 2. shortestPath (Fast bidirectional BFS)
        query_shortest = "PROFILE MATCH (a:Node {id: 0}), (b:Node {id: $target_id}) MATCH p = shortestPath((a)-[*..15]->(b)) RETURN length(p) AS path_len"
        t1 = time.perf_counter()
        res_sp = session.run(query_shortest, {"target_id": target_id})
        rec_sp = res_sp.single()
        sum_sp = res_sp.consume()
        wall_sp_ms = (time.perf_counter() - t1) * 1000.0
        sp_len = rec_sp["path_len"] if rec_sp else -1
        sp_db_hits = extract_total_db_hits(sum_sp.profile)

        plain_console.print(f"{'Algorithm':<28}{'Result':<18}{'DB Hits':<14}{'Latency (ms)':<14}")
        plain_console.print("-" * 74)
        plain_console.print(f"{'Exhaustive MATCH [*1..6]':<28}{f'{ex_paths} paths':<18}{ex_db_hits:<14,}{wall_ex_ms:<14.2f}")
        plain_console.print(f"{'shortestPath((a)-[*]-(b))':<28}{f'{sp_len} hops':<18}{sp_db_hits:<14,}{wall_sp_ms:<14.2f}")

        verify_phase(
            ex_paths > 0 and sp_len >= 1,
            "Phase 2 ShortestPath",
            f"expected valid path to node {target_id}, got ex_paths={ex_paths}, sp_len={sp_len}",
        )
        verify_phase(
            ex_db_hits > sp_db_hits * 5,
            "Phase 2 ShortestPath",
            f"expected shortestPath DB hits ({sp_db_hits}) << exhaustive ({ex_db_hits})",
        )

        if ex_db_hits > 0 and sp_db_hits > 0:
            reduction = ex_db_hits / sp_db_hits
            console.print(f"\n[[bold green]+[/]] shortestPath achieved a [bold yellow]{reduction:.1f}x[/] reduction in DB hits.")


def main() -> None:
    console.print("=" * 72, style="bold cyan")
    console.print("[bold cyan] Neo4j Limit Lab: Experiment 01 - Unbounded Path Combinatorial Explosion[/]")
    console.print("=" * 72, style="bold cyan")

    driver = get_driver()
    try:
        wait_for_neo4j(driver)
        reset_and_seed_graph(driver, num_nodes=2000, branching=4)
        test_depth_explosion(driver, max_depth=6)
        test_shortest_path_comparison(driver, target_id=500)
        console.print("\n[bold green][✓] Experiment 01 completed successfully.[/]")
    except Exception as e:
        err_console.print(f"\n[bold red][!] Error running experiment:[/] {escape(str(e))}")
        sys.exit(1)
    finally:
        driver.close()


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "neo4j>=5.15.0",
#     "psycopg[binary]>=3.1.0",
# ]
# ///
"""
Experiment 03: Multi-Hop Cypher (Neo4j) vs Recursive CTE SQL (PostgreSQL)

Demonstrates:
1. Loading an identical 3,000-node dependency/hierarchy graph into both
   PostgreSQL (relational table with B-Tree index) and Neo4j (native property graph).
2. Benchmarking variable-depth traversal (hops 1 to 6):
   - PostgreSQL: WITH RECURSIVE traverse AS (...) executing iterative hash/index joins.
   - Neo4j: MATCH (root:Service {id: 0})-[:DEPENDS_ON*1..d]->(dep) using index-free adjacency.
3. Observing the crossover point where pointer-chasing beats relational join materialization.
"""

from __future__ import annotations

import sys
import time

import psycopg
from rich.console import Console
from rich.markup import escape

from neo4j import GraphDatabase

NEO4J_URI = "bolt://127.0.0.1:7687"
NEO4J_AUTH = ("neo4j", "password123")
PG_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/lab_db"

NUM_NODES = 1500
BRANCHING = 3


console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def build_edges() -> list[tuple[int, int]]:
    edges: list[tuple[int, int]] = []
    for src in range(NUM_NODES):
        for b in range(1, BRANCHING + 1):
            dst = (src * BRANCHING + b) % NUM_NODES
            if dst != src:
                edges.append((src, dst))
    return edges


def seed_postgres(edges: list[tuple[int, int]]) -> None:
    plain_console.print(f"[*] Seeding PostgreSQL with {NUM_NODES:,} nodes and {len(edges):,} directed edges...")
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS service_deps;")
            cur.execute("""
            CREATE TABLE service_deps (
                src_id INT NOT NULL,
                dst_id INT NOT NULL,
                PRIMARY KEY (src_id, dst_id)
            );
            CREATE INDEX idx_service_src ON service_deps (src_id);
            """)
            with cur.copy("COPY service_deps (src_id, dst_id) FROM STDIN") as copy:
                for s, d in edges:
                    copy.write_row((s, d))
            cur.execute("ANALYZE service_deps;")
            cur.execute("SELECT COUNT(*) FROM service_deps;")
            pg_edge_cnt = cur.fetchone()[0]
            verify_phase(pg_edge_cnt == len(edges), "Phase 0 PG Seed", f"expected {len(edges)} edges in PostgreSQL, got {pg_edge_cnt}")


def seed_neo4j(driver, edges: list[tuple[int, int]]) -> None:
    plain_console.print(f"[*] Seeding Neo4j with {NUM_NODES:,} nodes and {len(edges):,} relationships...")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
        session.run("CREATE CONSTRAINT service_id_unique IF NOT EXISTS FOR (s:Service) REQUIRE s.id IS UNIQUE")
        session.run("CALL db.awaitIndexes()")
        session.run(
            "UNWIND range(0, $max_id) AS id CREATE (:Service {id: id})",
            max_id=NUM_NODES - 1,
        )
        edge_dicts = [{"src": s, "dst": d} for s, d in edges]
        session.run(
            """
            UNWIND $edges AS e
            MATCH (a:Service {id: e.src})
            MATCH (b:Service {id: e.dst})
            CREATE (a)-[:DEPENDS_ON]->(b)
            """,
            edges=edge_dicts,
        )
        neo_nodes = session.run("MATCH (n:Service) RETURN count(n) AS cnt").single()["cnt"]
        neo_edges = session.run("MATCH ()-[r:DEPENDS_ON]->() RETURN count(r) AS cnt").single()["cnt"]
        verify_phase(
            neo_nodes == NUM_NODES and neo_edges == len(edges),
            "Phase 0 Neo4j Seed",
            f"expected {NUM_NODES} nodes and {len(edges)} edges in Neo4j, got {neo_nodes} nodes, {neo_edges} edges",
        )


def query_postgres_depth(pg_conn, depth: int) -> tuple[int, float]:
    sql = """
    WITH RECURSIVE transit(dst_id, lvl) AS (
        SELECT dst_id, 1
        FROM service_deps
        WHERE src_id = 0
      UNION ALL
        SELECT d.dst_id, t.lvl + 1
        FROM transit t
        JOIN service_deps d ON t.dst_id = d.src_id
        WHERE t.lvl < %s
    )
    SELECT count(DISTINCT dst_id) FROM transit;
    """
    t0 = time.perf_counter()
    with pg_conn.cursor() as cur:
        cur.execute(sql, (depth,))
        distinct_count = cur.fetchone()[0]
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return distinct_count, elapsed_ms


def query_neo4j_depth(session, depth: int) -> tuple[int, float]:
    cypher = f"""
    MATCH (s:Service {{id: 0}})-[:DEPENDS_ON*1..{depth}]->(dep:Service)
    RETURN count(DISTINCT dep) AS cnt
    """
    t0 = time.perf_counter()
    rec = session.run(cypher).single()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return int(rec["cnt"]), elapsed_ms


def main() -> None:
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
        driver.verify_connectivity()
        pg_conn = psycopg.connect(PG_DSN, autocommit=True)
    except Exception as e:
        console.print(f"[bold red]Error connecting to Neo4j or PostgreSQL ({escape(str(e))})[/]")
        plain_console.print("Start the dual-engine profile first: make up EXP=compare-sql")
        sys.exit(1)

    console.print("=" * 70, style="bold cyan")
    console.print("[bold cyan]Experiment 03: Multi-Hop Cypher (Neo4j) vs WITH RECURSIVE (PostgreSQL)[/]")
    plain_console.print(f"Graph Topology: {NUM_NODES:,} services, branching factor = {BRANCHING}")
    console.print("=" * 70 + "\n", style="bold cyan")

    edges = build_edges()
    seed_postgres(edges)
    seed_neo4j(driver, edges)

    # Warmup both engines
    with driver.session() as session:
        query_neo4j_depth(session, 2)
        query_postgres_depth(pg_conn, 2)

        console.print("\n[bold blue][*] Benchmarking Multi-Hop Traversal Latency (Depths 1 to 7):[/]\n")
        header = f"{'Hop Depth':>9} | {'Distinct Reachable':>18} | {'Postgres CTE (ms)':>18} | {'Neo4j Cypher (ms)':>18}"
        plain_console.print(header)
        plain_console.print("-" * len(header))

        min_reach = 0
        max_reach = 0
        for depth in range(1, 8):
            pg_cnt, pg_ms = query_postgres_depth(pg_conn, depth)
            neo_cnt, neo_ms = query_neo4j_depth(session, depth)
            verify_phase(
                pg_cnt == neo_cnt and neo_cnt > 0,
                f"Depth {depth} Parity",
                f"expected matching >0 reachable nodes, got PG={pg_cnt}, Neo4j={neo_cnt}",
            )
            if depth == 1:
                min_reach = neo_cnt
            max_reach = neo_cnt
            plain_console.print(f"{depth:>9} | {neo_cnt:>18,} | {pg_ms:>16.2f}ms | {neo_ms:>16.2f}ms")

        verify_phase(max_reach > min_reach, "Multi-Hop Scaling", f"expected depth 7 reach ({max_reach}) > depth 1 reach ({min_reach})")

    console.print("\n" + "=" * 70, style="bold green")
    plain_console.print("SYSTEMS TAKEAWAYS")
    console.print("=" * 70, style="bold green")
    plain_console.print("1. Shallow Joins (1-3 hops): PostgreSQL B-Tree indexes are remarkably fast")
    plain_console.print("   and often beat or match a graph database without adding operational complexity.")
    plain_console.print("2. Deep / Variable Traversals (5+ hops): Relational WITH RECURSIVE must materialize")
    plain_console.print("   intermediate worktables and repeatedly probe B-Tree indexes O(log N) per hop.")
    plain_console.print("3. Index-Free Adjacency: Neo4j stores direct physical pointers on each node record,")
    plain_console.print("   making hop traversal an O(1) memory pointer chase per relationship.")
    plain_console.print("======================================================================\n")

    pg_conn.close()
    driver.close()


if __name__ == "__main__":
    main()

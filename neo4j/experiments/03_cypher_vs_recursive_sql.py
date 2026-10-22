# /// script
# requires-python = ">=3.12"
# dependencies = [
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
from neo4j import GraphDatabase
import psycopg


NEO4J_URI = "bolt://127.0.0.1:7687"
NEO4J_AUTH = ("neo4j", "password123")
PG_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/lab_db"

NUM_NODES = 1500
BRANCHING = 3


def build_edges() -> list[tuple[int, int]]:
    edges: list[tuple[int, int]] = []
    for src in range(NUM_NODES):
        for b in range(1, BRANCHING + 1):
            dst = (src * BRANCHING + b) % NUM_NODES
            if dst != src:
                edges.append((src, dst))
    return edges


def seed_postgres(edges: list[tuple[int, int]]) -> None:
    print(f"[*] Seeding PostgreSQL with {NUM_NODES:,} nodes and {len(edges):,} directed edges...")
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


def seed_neo4j(driver, edges: list[tuple[int, int]]) -> None:
    print(f"[*] Seeding Neo4j with {NUM_NODES:,} nodes and {len(edges):,} relationships...")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
        session.run("CREATE CONSTRAINT service_id_unique IF NOT EXISTS FOR (s:Service) REQUIRE s.id IS UNIQUE")
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
        print(f"\033[1;31mError connecting to Neo4j or PostgreSQL ({e})\033[0m")
        print("Start the dual-engine profile first: make up EXP=compare-sql")
        sys.exit(1)

    print("\033[1;36m======================================================================\033[0m")
    print("\033[1;36mExperiment 03: Multi-Hop Cypher (Neo4j) vs WITH RECURSIVE (PostgreSQL)\033[0m")
    print(f"Graph Topology: {NUM_NODES:,} services, branching factor = {BRANCHING}")
    print("\033[1;36m======================================================================\033[0m\n")

    edges = build_edges()
    seed_postgres(edges)
    seed_neo4j(driver, edges)

    # Warmup both engines
    with driver.session() as session:
        query_neo4j_depth(session, 2)
        query_postgres_depth(pg_conn, 2)

        print("\n\033[1;34m[*] Benchmarking Multi-Hop Traversal Latency (Depths 1 to 7):\033[0m\n")
        header = f"{'Hop Depth':>9} | {'Distinct Reachable':>18} | {'Postgres CTE (ms)':>18} | {'Neo4j Cypher (ms)':>18}"
        print(header)
        print("-" * len(header))

        for depth in range(1, 8):
            pg_cnt, pg_ms = query_postgres_depth(pg_conn, depth)
            neo_cnt, neo_ms = query_neo4j_depth(session, depth)
            assert pg_cnt == neo_cnt, f"Mismatch at depth {depth}: PG={pg_cnt} vs Neo4j={neo_cnt}"
            print(f"{depth:>9} | {neo_cnt:>18,} | {pg_ms:>16.2f}ms | {neo_ms:>16.2f}ms")

    print("\n\033[1;32m======================================================================")
    print("SYSTEMS TAKEAWAYS")
    print("======================================================================\033[0m")
    print("1. Shallow Joins (1-3 hops): PostgreSQL B-Tree indexes are remarkably fast")
    print("   and often beat or match a graph database without adding operational complexity.")
    print("2. Deep / Variable Traversals (5+ hops): Relational WITH RECURSIVE must materialize")
    print("   intermediate worktables and repeatedly probe B-Tree indexes O(log N) per hop.")
    print("3. Index-Free Adjacency: Neo4j stores direct physical pointers on each node record,")
    print("   making hop traversal an O(1) memory pointer chase per relationship.")
    print("======================================================================\n")

    pg_conn.close()
    driver.close()


if __name__ == "__main__":
    main()

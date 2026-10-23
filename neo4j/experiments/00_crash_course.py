# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "neo4j>=5.18.0",
# ]
# ///
"""
Neo4j Crash Course: Property Graph Model, Cypher Traversals & Graph Diagnostics

Demonstrates:
1. Bolt driver connectivity & database component version check.
2. Labeled node creation with typed property maps.
3. Directed relationship creation with edge properties.
4. Cypher pattern matching (single-hop and multi-hop coworker traversals).
5. Graph diagnostics (dbms.components, total node and relationship counts).
"""

from __future__ import annotations

import sys

from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "password123")


def main() -> None:
    print("\n\033[1;36m" + "=" * 70 + "\033[0m")
    print("\033[1;32mNeo4j Crash Course: Property Graphs, Cypher & Traversals\033[0m")
    print("\033[1;36m" + "=" * 70 + "\033[0m\n")

    try:
        driver = GraphDatabase.driver(URI, auth=AUTH)
        driver.verify_connectivity()
    except Exception as e:
        print(f"\033[1;31m[!] Connection failed: {e}\033[0m")
        print("Start the lab container first: cd neo4j && make up EXP=traversal-depth")
        sys.exit(1)

    with driver:
        # 1. Connectivity & Version
        print("\033[1;34m[*] 1. Server Connectivity & Component Introspection\033[0m")
        records, _, _ = driver.execute_query("CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS version, edition;")
        for r in records:
            print(f"    Product: {r['name']} v{r['version']} ({r['edition']})")

        # Clean any old test artifacts
        driver.execute_query("MATCH (n:CrashCourse) DETACH DELETE n;")

        # 2. Creating Nodes with Labels & Properties
        print("\n\033[1;34m[*] 2. Creating Labeled Nodes with Properties\033[0m")
        create_nodes_cypher = """
            CREATE (a:CrashCourse:Developer {id: 1, name: 'Alice', seniority: 'Principal'})
            CREATE (b:CrashCourse:Developer {id: 2, name: 'Bob', seniority: 'Senior'})
            CREATE (c:CrashCourse:Developer {id: 3, name: 'Charlie', seniority: 'Mid-level'})
            CREATE (p1:CrashCourse:Service {name: 'AuthGateway', tier: 1})
            CREATE (p2:CrashCourse:Service {name: 'PaymentProcessor', tier: 1})
            RETURN count(*) AS created;
        """
        driver.execute_query(create_nodes_cypher)
        print("    Created 3 Developer nodes and 2 Service nodes.")

        # 3. Directed Relationships with Edge Properties
        print("\n\033[1;34m[*] 3. Creating Directed Relationships with Edge Attributes\033[0m")
        create_edges_cypher = """
            MATCH (a:CrashCourse:Developer {name: 'Alice'}), (b:CrashCourse:Developer {name: 'Bob'}),
                  (c:CrashCourse:Developer {name: 'Charlie'}),
                  (s1:CrashCourse:Service {name: 'AuthGateway'}), (s2:CrashCourse:Service {name: 'PaymentProcessor'})
            CREATE (a)-[:MAINTAINS {role: 'Lead', since: 2022}]->(s1)
            CREATE (b)-[:MAINTAINS {role: 'Contributor', since: 2023}]->(s1)
            CREATE (a)-[:MAINTAINS {role: 'Architect', since: 2021}]->(s2)
            CREATE (c)-[:MAINTAINS {role: 'Contributor', since: 2024}]->(s2)
            CREATE (s1)-[:DEPENDS_ON {protocol: 'gRPC', timeout_ms: 250}]->(s2);
        """
        driver.execute_query(create_edges_cypher)
        print("    Created 4 :MAINTAINS edges and 1 :DEPENDS_ON service-to-service edge.")

        # 4. Pattern Match Traversals
        print("\n\033[1;34m[*] 4. Cypher Pattern Matching & Multi-Hop Traversal\033[0m")
        query_direct = """
            MATCH (d:CrashCourse:Developer)-[m:MAINTAINS]->(s:CrashCourse:Service)
            RETURN d.name AS dev, m.role AS role, s.name AS service
            ORDER BY service, dev;
        """
        res_direct, _, _ = driver.execute_query(query_direct)
        print("    Direct Maintainers:")
        for r in res_direct:
            print(f"      - {r['dev']} -> {r['service']} (Role: {r['role']})")

        print("\n    Traversing Coworkers (Developers sharing maintained services):")
        query_coworkers = """
            MATCH (d1:CrashCourse:Developer)-[:MAINTAINS]->(s:CrashCourse:Service)<-[:MAINTAINS]-(d2:CrashCourse:Developer)
            WHERE d1.id < d2.id
            RETURN d1.name AS dev1, d2.name AS dev2, s.name AS shared_service;
        """
        res_coworkers, _, _ = driver.execute_query(query_coworkers)
        for r in res_coworkers:
            print(f"      - {r['dev1']} & {r['dev2']} collaborate on '{r['shared_service']}'")

        # 5. Graph Diagnostics
        print("\n\033[1;34m[*] 5. Graph Diagnostics & Node Counts\033[0m")
        counts_res, _, _ = driver.execute_query("""
            MATCH (n:CrashCourse)
            OPTIONAL MATCH (n)-[r]->()
            RETURN count(DISTINCT n) AS nodes, count(r) AS rels;
        """)
        nodes_cnt = counts_res[0]["nodes"]
        rels_cnt = counts_res[0]["rels"]
        print(f"    Active CrashCourse subgraph: {nodes_cnt} nodes, {rels_cnt} relationships.")

        # Cleanup
        driver.execute_query("MATCH (n:CrashCourse) DETACH DELETE n;")
        print("    Cleaned up demo graph nodes and relationships.")

    print("\n\033[1;32m[✓] Neo4j Crash Course completed successfully!\033[0m\n")


if __name__ == "__main__":
    main()

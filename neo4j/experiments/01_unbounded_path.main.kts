@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.neo4j.driver:neo4j-java-driver:5.20.0")

import org.neo4j.driver.AuthTokens
import org.neo4j.driver.GraphDatabase
import org.neo4j.driver.summary.Plan
import org.neo4j.driver.summary.ProfiledPlan

val URI = "bolt://127.0.0.1:7687"
val AUTH = AuthTokens.basic("neo4j", "password123")
val NUM_NODES = 1500
val BRANCHING = 4

fun extractDbHits(plan: Plan?): Long {
    if (plan == null) return 0L
    var hits = 0L
    if (plan is ProfiledPlan) {
        hits += plan.dbHits()
    }
    for (child in plan.children()) {
        hits += extractDbHits(child)
    }
    return hits
}

fun main() {
    println("\u001b[1;36m=================================================================\u001b[0m")
    println("\u001b[1;36mExperiment 01 (Kotlin): Neo4j Unbounded Path Traversal\u001b[0m")
    println("Investigating combinatorial explosion O(b^d) vs bidirectional BFS.")
    println("\u001b[1;36m=================================================================\u001b[0m")

    val driver = try {
        val d = GraphDatabase.driver(URI, AUTH)
        d.verifyConnectivity()
        d
    } catch (e: Exception) {
        println("\u001b[1;31m[!] Error: Unable to connect to Neo4j at $URI: ${e.message}\u001b[0m")
        println("Start container first: cd neo4j && make up EXP=traversal-depth")
        return
    }

    driver.session().use { session ->
        val nodeCount = session.run("MATCH (n:Person) RETURN count(n) AS c").single().get("c").asInt()
        if (nodeCount < NUM_NODES) {
            println("\n[*] Seeding graph with $NUM_NODES nodes and branching factor $BRANCHING...")
            session.run("MATCH (n) DETACH DELETE n")
            session.run("""
                UNWIND range(0, $NUM_NODES - 1) AS id
                CREATE (:Person {id: id, name: 'Person_' + toString(id)})
            """.trimIndent())
            session.run("CREATE INDEX FOR (p:Person) ON (p.id)")

            val edges = ArrayList<List<Int>>()
            for (src in 0 until NUM_NODES) {
                for (b in 1..BRANCHING) {
                    val dst = (src * BRANCHING + b) % NUM_NODES
                    if (dst != src) edges.add(listOf(src, dst))
                }
            }
            session.run("""
                UNWIND ${'$'}edges AS e
                MATCH (a:Person {id: e[0]}), (b:Person {id: e[1]})
                CREATE (a)-[:KNOWS]->(b)
            """.trimIndent(), mapOf("edges" to edges))
            println("[*] Seeding complete.")
        }

        // Part 1: Combinatorial explosion
        println("\n\u001b[1;34m[*] Part 1: Benchmarking Variable-Length Path Matching (MATCH -[:KNOWS*1..d]->)...\u001b[0m")
        println("---------------------------------------------------------------------------------")
        println("%-6s %-15s %-18s %-15s".format("Depth", "Paths Evaluated", "Engine DB Hits", "Latency"))
        println("---------------------------------------------------------------------------------")

        for (depth in 1..5) {
            val cypher = "PROFILE MATCH p = (a:Person {id: 0})-[:KNOWS*1..$depth]->(b:Person) RETURN count(p) AS paths"
            val t0 = System.currentTimeMillis()
            val result = session.run(cypher)
            val record = result.single()
            val summary = result.consume()
            val elapsed = System.currentTimeMillis() - t0

            val paths = record.get("paths").asLong()
            val dbHits = extractDbHits(summary.profile())

            println("%-6d %-15s %-18s %-15s".format(
                depth,
                "%,d".format(paths),
                "%,d".format(dbHits),
                "${elapsed}ms"
            ))
        }
        println("---------------------------------------------------------------------------------")

        // Part 2: Shortest path optimization
        println("\n\u001b[1;34m[*] Part 2: Mitigation via ShortestPath (Bidirectional BFS)...\u001b[0m")
        val targetId = (NUM_NODES * 0.75).toInt()
        val cypherShortest = "PROFILE MATCH p = shortestPath((a:Person {id: 0})-[:KNOWS*]-(b:Person {id: $targetId})) RETURN length(p) AS hops"
        val t0 = System.currentTimeMillis()
        val result = session.run(cypherShortest)
        val record = result.single()
        val summary = result.consume()
        val elapsed = System.currentTimeMillis() - t0

        val hops = record.get("hops").asInt()
        val dbHits = extractDbHits(summary.profile())

        println("    Target Node ID : $targetId")
        println("    Shortest Path  : $hops hops")
        println("    DB Hits        : \u001b[1;32m${"%,d".format(dbHits)}\u001b[0m")
        println("    Duration       : ${elapsed}ms")

        println("\n\u001b[1;32m=================================================================")
        println("GRAPH TRAVERSAL TAKEAWAYS")
        println("=================================================================\u001b[0m")
        println("1. Unbounded variable-length traversals ([:KNOWS*]) explode combinatorially O(b^d).")
        println("2. While index-free adjacency enables microsecond pointer dereferences, evaluating")
        println("   millions of permutations exhausts memory and causes query timeouts.")
        println("3. Always constrain variable-length traversals with upper bounds (*1..3) or use")
        println("   shortestPath() / APOC path expanders with node filters.")
        println("=================================================================\n")
    }
    driver.close()
}

main()

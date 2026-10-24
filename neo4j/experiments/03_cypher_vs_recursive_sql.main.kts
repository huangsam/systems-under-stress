@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.github.ajalt.mordant:mordant-jvm:2.4.0")
@file:DependsOn("org.neo4j.driver:neo4j-java-driver:5.20.0")
@file:DependsOn("org.postgresql:postgresql:42.7.3")

import com.github.ajalt.mordant.rendering.TextColors.blue
import com.github.ajalt.mordant.rendering.TextColors.cyan
import com.github.ajalt.mordant.rendering.TextColors.green
import com.github.ajalt.mordant.rendering.TextColors.magenta
import com.github.ajalt.mordant.rendering.TextColors.red
import com.github.ajalt.mordant.rendering.TextColors.white
import com.github.ajalt.mordant.rendering.TextColors.yellow
import com.github.ajalt.mordant.rendering.TextStyles.bold
import com.github.ajalt.mordant.rendering.TextStyles.dim
import org.neo4j.driver.AuthTokens
import org.neo4j.driver.GraphDatabase
import java.sql.DriverManager

val NEO4J_URI = "bolt://127.0.0.1:7687"
val NEO4J_AUTH = AuthTokens.basic("neo4j", "password123")
val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

val PG_URL = "jdbc:postgresql://127.0.0.1:5432/lab_db?user=postgres&password=postgres"

val NUM_NODES = 1500
val BRANCHING = 3

fun main() {
    println(boldCyan("=".repeat(70)))
    println(boldCyan("Experiment 03 (Kotlin): Multi-Hop Cypher (Neo4j) vs Recursive SQL (Postgres)"))
    println("Benchmarking index-free adjacency vs iterative B-Tree index joins.")
    println(boldCyan("=".repeat(70)) + "\n")

    val neoDriver = try {
        val d = GraphDatabase.driver(NEO4J_URI, NEO4J_AUTH)
        d.verifyConnectivity()
        d
    } catch (e: Exception) {
        println(boldRed("Error: Cannot connect to Neo4j on $NEO4J_URI (${e.message})"))
        println("Start container first: cd neo4j && make up EXP=compare-sql")
        return
    }

    val pgConn = try {
        DriverManager.getConnection(PG_URL)
    } catch (e: Exception) {
        println(boldRed("Error: Cannot connect to PostgreSQL on $PG_URL (${e.message})"))
        return
    }

    val edges = ArrayList<Pair<Int, Int>>()
    for (src in 0 until NUM_NODES) {
        for (b in 1..BRANCHING) {
            val dst = (src * BRANCHING + b) % NUM_NODES
            if (dst != src) edges.add(Pair(src, dst))
        }
    }

    // Seed Postgres
    println("[*] Seeding PostgreSQL with ${"%,d".format(NUM_NODES)} nodes and ${"%,d".format(edges.size)} edges...")
    pgConn.createStatement().use { stmt ->
        stmt.execute("DROP TABLE IF EXISTS service_deps")
        stmt.execute("""
            CREATE TABLE service_deps (
                src_id INT NOT NULL,
                dst_id INT NOT NULL,
                PRIMARY KEY (src_id, dst_id)
            )
        """.trimIndent())
        stmt.execute("CREATE INDEX idx_service_src ON service_deps (src_id)")
    }
    pgConn.prepareStatement("INSERT INTO service_deps (src_id, dst_id) VALUES (?, ?) ON CONFLICT DO NOTHING").use { pstmt ->
        for ((s, d) in edges) {
            pstmt.setInt(1, s)
            pstmt.setInt(2, d)
            pstmt.addBatch()
        }
        pstmt.executeBatch()
    }

    // Seed Neo4j
    println("[*] Seeding Neo4j with identical topology...")
    neoDriver.session().use { session ->
        session.run("MATCH (n) DETACH DELETE n")
        session.run("""
            UNWIND range(0, $NUM_NODES - 1) AS id
            CREATE (:Service {id: id})
        """.trimIndent())
        session.run("CREATE INDEX FOR (s:Service) ON (s.id)")

        val edgeList = edges.map { listOf(it.first, it.second) }
        session.run("""
            UNWIND ${'$'}edges AS e
            MATCH (s:Service {id: e[0]}), (d:Service {id: e[1]})
            CREATE (s)-[:DEPENDS_ON]->(d)
        """.trimIndent(), mapOf("edges" to edgeList))
    }

    println("\n" + boldBlue("[*] Multi-Hop Traversal Benchmark (from Root Service ID: 0):"))
    println("---------------------------------------------------------------------------------")
    println("%-6s %-16s %-18s %-18s".format("Depth", "Distinct Reached", "Postgres SQL", "Neo4j Cypher"))
    println("---------------------------------------------------------------------------------")

    for (depth in 1..5) {
        // Postgres
        val pgSql = """
            WITH RECURSIVE traverse AS (
                SELECT dst_id, 1 AS depth
                FROM service_deps
                WHERE src_id = 0
                UNION
                SELECT d.dst_id, t.depth + 1
                FROM service_deps d
                JOIN traverse t ON d.src_id = t.dst_id
                WHERE t.depth < ?
            )
            SELECT count(DISTINCT dst_id) FROM traverse
        """.trimIndent()

        val tPg = System.nanoTime()
        var pgCount = 0
        pgConn.prepareStatement(pgSql).use { pstmt ->
            pstmt.setInt(1, depth)
            val rs = pstmt.executeQuery()
            if (rs.next()) pgCount = rs.getInt(1)
        }
        val pgDurationMs = (System.nanoTime() - tPg) / 1_000_000.0

        // Neo4j
        val neoCypher = "MATCH (root:Service {id: 0})-[:DEPENDS_ON*1..$depth]->(dep:Service) RETURN count(DISTINCT dep) AS reachable"
        val tNeo = System.nanoTime()
        var neoCount = 0L
        neoDriver.session().use { session ->
            val res = session.run(neoCypher)
            neoCount = res.single().get("reachable").asLong()
        }
        val neoDurationMs = (System.nanoTime() - tNeo) / 1_000_000.0

        println("%-6d %-16s %-18s %-18s".format(
            depth,
            "%,d nodes".format(neoCount),
            "%.2f ms".format(pgDurationMs),
            "%.2f ms".format(neoDurationMs)
        ))
    }
    println("---------------------------------------------------------------------------------")

    println("\n" + boldGreen("=".repeat(65)))
    println("REPRESENTATIONAL & ENGINE TAKEAWAYS")
    println(boldGreen("=".repeat(65)))
    println("1. Relational stores execute recursive CTEs via iterative table joins and hash sets.")
    println("   As depth increases, intermediate working tables grow quadratically.")
    println("2. Neo4j executes pointer dereferences across memory-mapped record chains.")
    println("3. For deep or variable-length graph queries (>= 3 hops), native property graphs")
    println("   substantially outpace relational join engines.")
    println(boldGreen("=".repeat(65)) + "\n")

    neoDriver.close()
    pgConn.close()
}

main()

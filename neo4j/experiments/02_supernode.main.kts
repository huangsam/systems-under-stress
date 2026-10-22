@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.neo4j.driver:neo4j-java-driver:5.20.0")

import org.neo4j.driver.AuthTokens
import org.neo4j.driver.GraphDatabase
import org.neo4j.driver.summary.Plan
import org.neo4j.driver.summary.ProfiledPlan
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

val URI = "bolt://127.0.0.1:7687"
val AUTH = AuthTokens.basic("neo4j", "password123")
val SUPERNODE_EDGES = 30_000

fun extractDbHits(plan: Plan?): Long {
    if (plan == null) return 0L
    var hits = 0L
    if (plan is ProfiledPlan) hits += plan.dbHits()
    for (child in plan.children()) hits += extractDbHits(child)
    return hits
}

fun main() {
    println("\u001b[1;36m=================================================================\u001b[0m")
    println("\u001b[1;36mExperiment 02 (Kotlin): Neo4j Supernodes & Lock Contention\u001b[0m")
    println("Analyzing pointer-chasing traversal overhead and concurrent write bottlenecks.")
    println("\u001b[1;36m=================================================================\u001b[0m")

    val driver = try {
        val d = GraphDatabase.driver(URI, AUTH)
        d.verifyConnectivity()
        d
    } catch (e: Exception) {
        println("\u001b[1;31m[!] Error: Unable to connect to Neo4j at $URI: ${e.message}\u001b[0m")
        println("Start container first: cd neo4j && make up EXP=supernode")
        return
    }

    driver.session().use { session ->
        println("\n[*] Resetting graph and creating Supernode vs Sparse Node...")
        session.run("MATCH (n) DETACH DELETE n")
        session.run("CREATE (:Account {id: 'superstar', name: 'Superstar (Hub)'})")
        session.run("CREATE (:Account {id: 'regular_user', name: 'Regular User (Sparse)'})")

        // Seed 30,000 edges pointing to superstar
        val batchSize = 5000
        val t0 = System.currentTimeMillis()
        for (offset in 0 until SUPERNODE_EDGES step batchSize) {
            session.run("""
                UNWIND range(${'$'}start, ${'$'}end) AS id
                CREATE (f:Account {id: 'fan_' + toString(id)})
                CREATE (f)-[:FOLLOWS]->(hub:Account {id: 'superstar'})
            """.trimIndent(), mapOf("start" to offset, "end" to offset + batchSize - 1))
        }
        // Seed 10 edges pointing to regular user
        session.run("""
            UNWIND range(0, 9) AS id
            CREATE (f:Account {id: 'friend_' + toString(id)})
            CREATE (f)-[:FOLLOWS]->(reg:Account {id: 'regular_user'})
        """.trimIndent())
        println("[*] Seeded ${"%,d".format(SUPERNODE_EDGES)} edges in %.2fs.".format((System.currentTimeMillis() - t0) / 1000.0))

        // Part 1: Read traversal degradation
        println("\n\u001b[1;34m[*] Part 1: Read Traversal - Sparse Node vs Supernode...\u001b[0m")
        val resSparse = session.run("PROFILE MATCH (a:Account {id: 'regular_user'})<-[:FOLLOWS]-(f) RETURN count(f) AS c")
        val sparseCount = resSparse.single().get("c").asLong()
        val sparseHits = extractDbHits(resSparse.consume().profile())

        val resHub = session.run("PROFILE MATCH (a:Account {id: 'superstar'})<-[:FOLLOWS]-(f) RETURN count(f) AS c")
        val hubCount = resHub.single().get("c").asLong()
        val hubHits = extractDbHits(resHub.consume().profile())

        println("    Sparse Node (10 edges)    : $sparseCount followers | DB Hits: \u001b[1;32m${"%,d".format(sparseHits)}\u001b[0m")
        println("    Supernode (30,000 edges)  : $hubCount followers | DB Hits: \u001b[1;31m${"%,d".format(hubHits)}\u001b[0m")
    }

    // Part 2: Concurrent write lock contention
    println("\n\u001b[1;34m[*] Part 2: Concurrent Write Lock Contention on Single Hub vs Partitioned Nodes...\u001b[0m")
    val concurrency = 16
    val writesPerThread = 100

    // Test A: Contended Writes (all threads target superstar)
    val pool = Executors.newFixedThreadPool(concurrency)
    val successA = AtomicInteger(0)
    val tA = System.currentTimeMillis()
    for (t in 0 until concurrency) {
        pool.submit {
            driver.session().use { sess ->
                for (i in 0 until writesPerThread) {
                    sess.run(
                        "MATCH (h:Account {id: 'superstar'}) CREATE (u:User {id: ${'$'}uid})-[:FOLLOWS]->(h)",
                        mapOf("uid" to "contended_${t}_$i")
                    )
                    successA.incrementAndGet()
                }
            }
        }
    }
    pool.shutdown()
    pool.awaitTermination(30, TimeUnit.SECONDS)
    val elapsedA = System.currentTimeMillis() - tA
    println("    Contended Hub Writes (Lock contention) : ${successA.get()} writes in ${elapsedA}ms (%.1f writes/sec)".format(
        successA.get() * 1000.0 / elapsedA
    ))

    println("\n\u001b[1;32m=================================================================")
    println("SUPERNODE MITIGATION ARCHITECTURES")
    println("=================================================================\u001b[0m")
    println("1. Relationship Chaining: Traversing supernodes degrades to O(N) linear scans.")
    println("2. Lock Contention: Writing to the same hub serializes on exclusive node locks.")
    println("3. Production remediations:")
    println("   - Edge Directionality: Avoid dense incoming traversals by querying in reverse.")
    println("   - Partitioned Fanout: Decompose single hub into sub-hubs (e.g. superstar_bucket_1..N).")
    println("   - Hybrid Arch: Store supernode relationship lists in Redis sets or Kafka streams.")
    println("=================================================================\n")

    driver.close()
}

main()

#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.github.ajalt.mordant:mordant-jvm:2.4.0")
@file:DependsOn("org.neo4j.driver:neo4j-java-driver:5.20.0")

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
import kotlin.system.exitProcess

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

val URI = "bolt://127.0.0.1:7687"
val AUTH = AuthTokens.basic("neo4j", "password123")

fun main() {
    println("\n" + boldCyan("=".repeat(70)))
    println(boldGreen("Neo4j Crash Course (Kotlin / Java Driver): Property Graphs & Cypher"))
    println(boldCyan("=".repeat(70)) + "\n")

    val driver =
        try {
            val d = GraphDatabase.driver(URI, AUTH)
            d.verifyConnectivity()
            d
        } catch (e: Exception) {
            println(boldRed("[!] Connection failed: ${e.message}"))
            println("Start the lab container first: cd neo4j && make up EXP=traversal-depth")
            exitProcess(1)
        }

    driver.use { d ->
        d.session().use { session ->
            // 1. Version check
            println(boldBlue("[*] 1. Server Connectivity & Component Introspection"))
            val compResult =
                session.run(
                    "CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS version, edition;",
                )
            while (compResult.hasNext()) {
                val record = compResult.next()
                println("    Product: ${record.get("name").asString()} v${record.get("version").asString()} (${record.get("edition").asString()})")
            }

            // Cleanup old test artifacts
            session.run("MATCH (n:CrashCourse) DETACH DELETE n;")

            // 2. Nodes
            println("\n" + boldBlue("[*] 2. Creating Labeled Nodes with Properties"))
            val createNodesCypher =
                """
                CREATE (a:CrashCourse:Developer {id: 1, name: 'Alice', seniority: 'Principal'})
                CREATE (b:CrashCourse:Developer {id: 2, name: 'Bob', seniority: 'Senior'})
                CREATE (c:CrashCourse:Developer {id: 3, name: 'Charlie', seniority: 'Mid-level'})
                CREATE (p1:CrashCourse:Service {name: 'AuthGateway', tier: 1})
                CREATE (p2:CrashCourse:Service {name: 'PaymentProcessor', tier: 1})
                RETURN count(*) AS created;
                """.trimIndent()
            session.run(createNodesCypher)
            println("    Created 3 Developer nodes and 2 Service nodes.")

            // 3. Relationships
            println("\n" + boldBlue("[*] 3. Creating Directed Relationships with Edge Attributes"))
            val createEdgesCypher =
                """
                MATCH (a:CrashCourse:Developer {name: 'Alice'}), (b:CrashCourse:Developer {name: 'Bob'}),
                      (c:CrashCourse:Developer {name: 'Charlie'}),
                      (s1:CrashCourse:Service {name: 'AuthGateway'}), (s2:CrashCourse:Service {name: 'PaymentProcessor'})
                CREATE (a)-[:MAINTAINS {role: 'Lead', since: 2022}]->(s1)
                CREATE (b)-[:MAINTAINS {role: 'Contributor', since: 2023}]->(s1)
                CREATE (a)-[:MAINTAINS {role: 'Architect', since: 2021}]->(s2)
                CREATE (c)-[:MAINTAINS {role: 'Contributor', since: 2024}]->(s2)
                CREATE (s1)-[:DEPENDS_ON {protocol: 'gRPC', timeout_ms: 250}]->(s2);
                """.trimIndent()
            session.run(createEdgesCypher)
            println("    Created 4 :MAINTAINS edges and 1 :DEPENDS_ON service-to-service edge.")

            // 4. Traversals
            println("\n" + boldBlue("[*] 4. Cypher Pattern Matching & Multi-Hop Traversal"))
            val queryDirect =
                """
                MATCH (d:CrashCourse:Developer)-[m:MAINTAINS]->(s:CrashCourse:Service)
                RETURN d.name AS dev, m.role AS role, s.name AS service
                ORDER BY service, dev;
                """.trimIndent()
            val resDirect = session.run(queryDirect)
            println("    Direct Maintainers:")
            while (resDirect.hasNext()) {
                val r = resDirect.next()
                println("      - ${r.get("dev").asString()} -> ${r.get("service").asString()} (Role: ${r.get("role").asString()})")
            }

            println("\n    Traversing Coworkers (Developers sharing maintained services):")
            val queryCoworkers =
                """
                MATCH (d1:CrashCourse:Developer)-[:MAINTAINS]->(s:CrashCourse:Service)<-[:MAINTAINS]-(d2:CrashCourse:Developer)
                WHERE d1.id < d2.id
                RETURN d1.name AS dev1, d2.name AS dev2, s.name AS shared_service;
                """.trimIndent()
            val resCoworkers = session.run(queryCoworkers)
            while (resCoworkers.hasNext()) {
                val r = resCoworkers.next()
                println("      - ${r.get("dev1").asString()} & ${r.get("dev2").asString()} collaborate on '${r.get("shared_service").asString()}'")
            }

            // 5. Diagnostics
            println("\n" + boldBlue("[*] 5. Graph Diagnostics & Node Counts"))
            val countsRes =
                session.run(
                    """
                    MATCH (n:CrashCourse)
                    OPTIONAL MATCH (n)-[r]->()
                    RETURN count(DISTINCT n) AS nodes, count(r) AS rels;
                    """.trimIndent(),
                )
            if (countsRes.hasNext()) {
                val record = countsRes.next()
                println("    Active CrashCourse subgraph: ${record.get("nodes").asLong()} nodes, ${record.get("rels").asLong()} relationships.")
            }

            // Cleanup
            session.run("MATCH (n:CrashCourse) DETACH DELETE n;")
            println("    Cleaned up demo graph nodes and relationships.")
        }
    }

    println("\n" + boldGreen("[✓] Neo4j Crash Course completed successfully!") + "\n")
}

main()

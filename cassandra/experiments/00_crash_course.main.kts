#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.datastax.oss:java-driver-core:4.17.0")
@file:DependsOn("org.slf4j:slf4j-nop:2.0.13")

import com.datastax.oss.driver.api.core.CqlSession
import com.datastax.oss.driver.api.core.cql.SimpleStatement
import java.net.InetSocketAddress
import java.util.UUID
import kotlin.system.exitProcess

val HOST = "127.0.0.1"
val PORT = 9042
val DC = "datacenter1"
val KEYSPACE = "lab_crash_course"
val TABLE_NAME = "users_by_country"

fun main() {
    println("\n\u001B[1;36m" + "=".repeat(70) + "\u001B[0m")
    println("\u001B[1;32mApache Cassandra Crash Course (Kotlin / DataStax Driver): CQL & Partitions\u001B[0m")
    println("\u001B[1;36m" + "=".repeat(70) + "\u001B[0m\n")

    val session =
        try {
            CqlSession
                .builder()
                .addContactPoint(InetSocketAddress(HOST, PORT))
                .withLocalDatacenter(DC)
                .build()
        } catch (e: Exception) {
            println("\u001B[1;31m[!] Connection failed: ${e.message}\u001B[0m")
            println("Start the lab container first: cd cassandra && make up EXP=tombstones")
            exitProcess(1)
        }

    session.use { sess ->
        // 1. Cluster Metadata
        println("\u001B[1;34m[*] 1. Node Metadata & System Introspection\u001B[0m")
        val nodeRow = sess.execute("SELECT cluster_name, data_center, release_version FROM system.local").one()
        if (nodeRow != null) {
            println("    Cluster Name: ${nodeRow.getString("cluster_name")}")
            println("    Datacenter: ${nodeRow.getString("data_center")}, Cassandra Version: ${nodeRow.getString("release_version")}")
        }

        // 2. Keyspace & Table Creation
        println("\n\u001B[1;34m[*] 2. Creating Keyspace & Table Schema\u001B[0m")
        sess.execute(
            """
            CREATE KEYSPACE IF NOT EXISTS $KEYSPACE
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
            """.trimIndent(),
        )

        sess.execute("DROP TABLE IF EXISTS $KEYSPACE.$TABLE_NAME;")
        sess.execute(
            """
            CREATE TABLE $KEYSPACE.$TABLE_NAME (
                country text,
                user_id uuid,
                username text,
                email text,
                score int,
                PRIMARY KEY ((country), user_id)
            );
            """.trimIndent(),
        )
        println("    Created keyspace '$KEYSPACE' and table '$TABLE_NAME'.")
        println("    Schema Notes: 'country' is Partition Key (token hash); 'user_id' is Clustering Column.")

        // 3. Prepared Statements & Inserts
        println("\n\u001B[1;34m[*] 3. Prepared Statements & Partition-Targeted Writes\u001B[0m")
        val insertStmt =
            sess.prepare(
                """
                INSERT INTO $KEYSPACE.$TABLE_NAME (country, user_id, username, email, score)
                VALUES (?, ?, ?, ?, ?);
                """.trimIndent(),
            )

        val records =
            listOf(
                Triple("US", UUID.randomUUID(), "alice_us") to ("alice@example.com" to 95),
                Triple("US", UUID.randomUUID(), "bob_us") to ("bob@example.com" to 82),
                Triple("CA", UUID.randomUUID(), "charlie_ca") to ("charlie@example.ca" to 88),
                Triple("DE", UUID.randomUUID(), "david_de") to ("david@example.de" to 91),
                Triple("US", UUID.randomUUID(), "eve_us") to ("eve@example.com" to 99),
            )

        for ((user, info) in records) {
            sess.execute(insertStmt.bind(user.first, user.second, user.third, info.first, info.second))
        }
        println("    Inserted ${records.size} users across 3 distinct country partitions (US, CA, DE).")

        // 4. Partition Query
        println("\n\u001B[1;34m[*] 4. Querying by Partition Key (O(1) Token Routing)\u001B[0m")
        val query = SimpleStatement.newInstance("SELECT country, username, email, score FROM $KEYSPACE.$TABLE_NAME WHERE country = 'US';")
        val rs = sess.execute(query)
        println("    Querying partition 'country = US':")
        for (r in rs) {
            println("      - [${r.getString("country")}] ${r.getString("username")} (${r.getString("email")}) -> Score: ${r.getInt("score")}")
        }

        // 5. Schema Introspection
        println("\n\u001B[1;34m[*] 5. Schema Introspection (system_schema.columns)\u001B[0m")
        val schemaQuery =
            """
            SELECT column_name, kind, type
            FROM system_schema.columns
            WHERE keyspace_name = '$KEYSPACE' AND table_name = '$TABLE_NAME';
            """.trimIndent()
        val cols = sess.execute(schemaQuery)
        println("    Column definitions for $KEYSPACE.$TABLE_NAME:")
        for (col in cols) {
            println("      - ${col.getString("column_name")}: ${col.getString("type")} (kind: ${col.getString("kind")})")
        }

        // Cleanup
        sess.execute("DROP KEYSPACE IF EXISTS $KEYSPACE;")
        println("    Cleaned up demo keyspace '$KEYSPACE'.")
    }

    println("\n\u001B[1;32m[✓] Apache Cassandra Crash Course completed successfully!\u001B[0m\n")
}

main()

#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.postgresql:postgresql:42.7.3")

import java.sql.Connection
import java.sql.DriverManager

val DB_URL = "jdbc:postgresql://127.0.0.1:5432/lab_db"
val DB_USER = "postgres"
val DB_PASS = "postgres"

fun getConn(): Connection = DriverManager.getConnection(DB_URL, DB_USER, DB_PASS)

fun seedData() {
    println("[*] Preparing 150,000 test rows in 'analytics_events' table...")
    getConn().use { conn ->
        conn.createStatement().use { stmt ->
            stmt.execute("""
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id SERIAL PRIMARY KEY,
                    user_id INT NOT NULL,
                    event_name VARCHAR(100) NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """.trimIndent())

            val rs = stmt.executeQuery("SELECT count(*) FROM analytics_events;")
            rs.next()
            val count = rs.getInt(1)
            if (count >= 150000) {
                println("    Table already seeded with ${"%,d".format(count)} rows.")
                return
            }

            println("    Generating 150,000 synthetic rows via generate_series...")
            val t0 = System.currentTimeMillis()
            stmt.execute("""
                TRUNCATE TABLE analytics_events;
                INSERT INTO analytics_events (user_id, event_name, payload, created_at)
                SELECT 
                    (random() * 50000)::INT,
                    'page_view_' || (random() * 10)::INT,
                    md5(g::TEXT) || repeat('DATA_PAYLOAD_PADDING_', 2),
                    NOW() - (random() * interval '30 days')
                FROM generate_series(1, 150000) AS g;
            """.trimIndent())
            val elapsedSec = (System.currentTimeMillis() - t0) / 1000.0
            println("    Seeded 150,000 rows in ${"%.2f".format(elapsedSec)}s.")
        }
    }
}

fun explainSort(workMem: String, label: String) {
    println("\n\u001B[1;34m[*] $label: Setting work_mem = '$workMem'...\u001B[0m")
    getConn().use { conn ->
        conn.createStatement().use { stmt ->
            stmt.execute("SET work_mem = '$workMem';")
            val query = "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM analytics_events ORDER BY payload;"
            val rs = stmt.executeQuery(query)
            val lines = mutableListOf<String>()
            while (rs.next()) {
                lines.add(rs.getString(1))
            }

            for (line in lines) {
                if (line.contains("Sort Method:")) {
                    val color = if (line.contains("external merge")) "\u001B[1;31m" else "\u001B[1;32m"
                    println("    $color$line\u001B[0m")
                }
                if (line.contains("Execution Time:")) {
                    println("    \u001B[1;33m$line\u001B[0m")
                }
            }
        }
    }
}

println("\n\u001B[1;36m" + "=".repeat(70) + "\u001B[0m")
println("\u001B[1;32mExperiment 03 (Kotlin / JDBC): PostgreSQL work_mem & Disk Spill\u001B[0m")
println("\u001B[1;36m" + "=".repeat(70) + "\u001B[0m\n")

seedData()
explainSort("4MB", "Part 1: Constrained Memory (Spill to Disk)")
explainSort("64MB", "Part 2: Sufficient Memory (In-Memory Quicksort)")

println("\n\u001B[1;34m" + "=".repeat(70) + "\u001B[0m")
println("SYSTEMS TAKEAWAYS:")
println("1. With work_mem = 4MB, sorting 150k rows exceeds RAM and spills to temporary disk files.")
println("2. Bumping work_mem = 64MB flips the algorithm from external merge to in-memory quicksort.")
println("3. High global work_mem multiplied by max_connections can trigger container OOM kills.")
println("\u001B[1;34m" + "=".repeat(70) + "\u001B[0m\n")

#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.clickhouse:clickhouse-jdbc:0.6.0")

import java.sql.DriverManager
import java.sql.Timestamp
import kotlin.system.exitProcess

val JDBC_URL = "jdbc:ch://127.0.0.1:8123/default"
val TABLE_NAME = "crash_course_telemetry"

fun main() {
    println("\n\u001B[1;36m" + "=".repeat(70) + "\u001B[0m")
    println("\u001B[1;32mClickHouse Crash Course (Kotlin / JDBC): Columnar Storage & Aggregations\u001B[0m")
    println("\u001B[1;36m" + "=".repeat(70) + "\u001B[0m\n")

    val conn =
        try {
            DriverManager.getConnection(JDBC_URL)
        } catch (e: Exception) {
            println("\u001B[1;31m[!] Connection failed: ${e.message}\u001B[0m")
            println("Start the lab container first: cd clickhouse && make up EXP=parts-limit")
            exitProcess(1)
        }

    conn.use { c ->
        // 1. Connectivity & Version
        println("\u001B[1;34m[*] 1. Server Version & Connectivity\u001B[0m")
        c.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT version()")
            rs.next()
            println("    ClickHouse Version: ${rs.getString(1)}")
        }

        // 2. Table Creation
        println("\n\u001B[1;34m[*] 2. Creating MergeTree Engine Table\u001B[0m")
        c.createStatement().use { stmt ->
            stmt.execute("DROP TABLE IF EXISTS $TABLE_NAME")
            stmt.execute(
                """
                CREATE TABLE $TABLE_NAME (
                    timestamp DateTime CODEC(DoubleDelta, ZSTD(1)),
                    device_id LowCardinality(String),
                    metric_type LowCardinality(String),
                    value Float64 CODEC(Gorilla, ZSTD(1))
                ) ENGINE = MergeTree()
                ORDER BY (metric_type, device_id, timestamp)
                """.trimIndent(),
            )
            println("    Created table '$TABLE_NAME' with specialized codecs (DoubleDelta, Gorilla, LowCardinality).")
        }

        // 3. Batch Insert
        println("\n\u001B[1;34m[*] 3. Columnar Batch Insert (Block Append)\u001B[0m")
        val insertSql = "INSERT INTO $TABLE_NAME (timestamp, device_id, metric_type, value) VALUES (?, ?, ?, ?)"
        val now = System.currentTimeMillis()
        val devices = listOf("sensor-alpha", "sensor-beta", "sensor-gamma")
        val metrics = listOf("temperature", "humidity", "voltage")

        c.prepareStatement(insertSql).use { ps ->
            val totalRows = 10_000
            for (i in 0 until totalRows) {
                val dev = devices[i % devices.size]
                val met = metrics[(i / 3) % metrics.size]
                val value = 20.0 + (i % 100) * 0.1
                ps.setTimestamp(1, Timestamp(now + (i * 100L)))
                ps.setString(2, dev)
                ps.setString(3, met)
                ps.setDouble(4, value)
                ps.addBatch()
            }
            ps.executeBatch()
            println("    Appended $totalRows rows in a single batch block.")
        }

        // 4. Vectorized Analytical Aggregation
        println("\n\u001B[1;34m[*] 4. Vectorized Analytical Aggregations\u001B[0m")
        c.createStatement().use { stmt ->
            val query =
                """
                SELECT
                    metric_type,
                    count() AS count,
                    round(min(value), 2) AS min_val,
                    round(avg(value), 2) AS avg_val,
                    round(max(value), 2) AS max_val,
                    round(quantile(0.95)(value), 2) AS p95_val
                FROM $TABLE_NAME
                GROUP BY metric_type
                ORDER BY metric_type
                """.trimIndent()
            val rs = stmt.executeQuery(query)
            println("    Aggregated metrics across 10,000 measurements:")
            while (rs.next()) {
                val metric = rs.getString("metric_type")
                val count = rs.getLong("count")
                val minVal = rs.getDouble("min_val")
                val avgVal = rs.getDouble("avg_val")
                val maxVal = rs.getDouble("max_val")
                val p95 = rs.getDouble("p95_val")
                println("      - $metric: count=$count, min=$minVal, avg=$avgVal, max=$maxVal, p95=$p95")
            }
        }

        // 5. Diagnostics
        println("\n\u001B[1;34m[*] 5. Storage Introspection (system.parts)\u001B[0m")
        c.createStatement().use { stmt ->
            val query =
                """
                SELECT
                    name,
                    part_type,
                    rows,
                    formatReadableSize(data_compressed_bytes) AS compressed,
                    formatReadableSize(data_uncompressed_bytes) AS uncompressed
                FROM system.parts
                WHERE table = '$TABLE_NAME' AND active = 1
                """.trimIndent()
            val rs = stmt.executeQuery(query)
            while (rs.next()) {
                val name = rs.getString("name")
                val type = rs.getString("part_type")
                val rows = rs.getLong("rows")
                val comp = rs.getString("compressed")
                val uncomp = rs.getString("uncompressed")
                println("    Active Part '$name': type=$type, rows=$rows, compressed=$comp, uncompressed=$uncomp")
            }
        }

        // Cleanup
        c.createStatement().use { stmt ->
            stmt.execute("DROP TABLE IF EXISTS $TABLE_NAME")
            println("    Cleaned up demo table '$TABLE_NAME'.")
        }
    }

    println("\n\u001B[1;32m[✓] ClickHouse Crash Course completed successfully!\u001B[0m\n")
}

main()

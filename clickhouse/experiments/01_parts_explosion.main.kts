@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.clickhouse:clickhouse-jdbc:0.6.0")

import java.sql.DriverManager
import java.sql.SQLException

val JDBC_URL = "jdbc:ch://127.0.0.1:8123/default"
val TABLE_NAME = "parts_test"

fun getActivePartsCount(conn: java.sql.Connection): Int {
    conn.createStatement().use { stmt ->
        val rs = stmt.executeQuery("SELECT count() FROM system.parts WHERE table = '$TABLE_NAME' AND active = 1")
        return if (rs.next()) rs.getInt(1) else 0
    }
}

fun resetTable(conn: java.sql.Connection) {
    conn.createStatement().use { stmt ->
        stmt.execute("DROP TABLE IF EXISTS $TABLE_NAME")
        stmt.execute("""
            CREATE TABLE $TABLE_NAME (
                id UInt64,
                created_at DateTime DEFAULT now(),
                payload String
            ) ENGINE = MergeTree()
            ORDER BY id
            SETTINGS parts_to_delay_insert = 80, parts_to_throw_insert = 120
        """.trimIndent())
    }
}

fun main() {
    println("\u001b[1;36m=================================================================\u001b[0m")
    println("\u001b[1;36mExperiment 01 (Kotlin): ClickHouse Parts Explosion & Batching Limits\u001b[0m")
    println("Investigating MergeTree write mechanics and the TOO_MANY_PARTS threshold.")
    println("\u001b[1;36m=================================================================\u001b[0m")

    val conn = try {
        DriverManager.getConnection(JDBC_URL)
    } catch (e: Exception) {
        println("\u001b[1;31m[!] Error: Unable to connect to ClickHouse at $JDBC_URL: ${e.message}\u001b[0m")
        println("Start container first: cd clickhouse && make up EXP=parts-limit")
        return
    }

    // Part 1: Rapid Un-batched single-row inserts
    println("\n\u001b[1;34m[*] Part 1: Rapid Un-Batched Single-Row Inserts (Testing Merge Capacity)...\u001b[0m")
    println("    Target Table : $TABLE_NAME (MergeTree)")
    println("    Thresholds   : parts_to_delay_insert=80, parts_to_throw_insert=120")
    println("    Firing up to 500 single-row inserts without batching...\n")

    resetTable(conn)

    var insertedCount = 0
    val startTime = System.currentTimeMillis()
    var tooManyPartsHit = false

    conn.prepareStatement("INSERT INTO $TABLE_NAME (id, payload) VALUES (?, ?)").use { pstmt ->
        for (i in 1..500) {
            try {
                pstmt.setLong(1, i.toLong())
                pstmt.setString(2, "payload-data-node-%05d".format(i))
                pstmt.executeUpdate()
                insertedCount++

                if (insertedCount % 15 == 0 || insertedCount in listOf(1, 50, 80, 100)) {
                    val parts = getActivePartsCount(conn)
                    val elapsed = (System.currentTimeMillis() - startTime) / 1000.0
                    println("    Insert #%3d | Active Parts on Disk: \u001b[1;33m%3d\u001b[0m | Elapsed: %.2fs".format(insertedCount, parts, elapsed))
                }
            } catch (e: SQLException) {
                val msg = e.message ?: ""
                if (msg.contains("Too many parts") || msg.contains("252") || e.errorCode == 252) {
                    tooManyPartsHit = true
                    val parts = getActivePartsCount(conn)
                    println("\n  \u001b[1;31m[!] WRITE REJECTED AT INSERT #${insertedCount + 1}!\u001b[0m")
                    println("      Active parts in system.parts : \u001b[1;31m$parts\u001b[0m")
                    println("      ClickHouse Error Code         : 252 (TOO_MANY_PARTS)")
                    println("      Engine Message                : ${msg.trim()}")
                    break
                } else {
                    println("\n  \u001b[1;31m[!] Unexpected error: ${e.message}\u001b[0m")
                    break
                }
            }
        }
    }

    if (tooManyPartsHit) {
        println("\n  \u001b[1;32m[✓] Failure Reproduced:\u001b[0m ClickHouse backpressure mechanism successfully throttled")
        println("      writes because background compaction could not keep pace with point inserts.")
    }

    // Part 2: Vectorized Batched Insert
    println("\n\u001b[1;34m[*] Part 2: Vectorized Batched Insert (Production Pattern)...\u001b[0m")
    resetTable(conn)

    val batchSize = 2000
    println("    Generating ${"%,d".format(batchSize)} records in client memory...")
    println("    Sending 1 batch of ${"%,d".format(batchSize)} rows in a single network roundtrip...")

    val batchStart = System.currentTimeMillis()
    conn.prepareStatement("INSERT INTO $TABLE_NAME (id, payload) VALUES (?, ?)").use { pstmt ->
        for (i in 1..batchSize) {
            pstmt.setLong(1, i.toLong())
            pstmt.setString(2, "batched-payload-%05d".format(i))
            pstmt.addBatch()
        }
        pstmt.executeBatch()
    }
    val batchElapsedMs = System.currentTimeMillis() - batchStart
    val parts = getActivePartsCount(conn)

    var totalRows = 0
    conn.createStatement().use { stmt ->
        val rs = stmt.executeQuery("SELECT count() FROM $TABLE_NAME")
        if (rs.next()) totalRows = rs.getInt(1)
    }

    println("\n    Batch Status          : \u001b[1;32mSUCCESS\u001b[0m")
    println("    Total Rows in Table   : ${"%,d".format(totalRows)}")
    println("    Active Parts Created  : \u001b[1;32m$parts\u001b[0m (Entire batch written as 1 contiguous part)")
    println("    Duration              : %.2f ms (%s rows/sec)".format(
        batchElapsedMs.toDouble(),
        "%,d".format(if (batchElapsedMs > 0) (batchSize * 1000L / batchElapsedMs) else batchSize.toLong())
    ))

    println("\n\u001b[1;32m=================================================================")
    println("SYSTEMS & PRODUCTION TAKEAWAYS")
    println("=================================================================\u001b[0m")
    println("1. ClickHouse is NOT an OLTP database. Every INSERT creates a distinct directory")
    println("   containing compressed column data (.bin) and index marks (.mrk).")
    println("2. Treating ClickHouse like an OLTP store with single-row inserts guarantees catastrophic")
    println("   TOO_MANY_PARTS (Code: 252) failures and disk metadata thrashing.")
    println("3. Best practices for ClickHouse ingestion:")
    println("   - Batch inserts on client side (>= 1,000 to 100,000 rows per batch).")
    println("   - Use an ingestion buffer: Apache Kafka, Vector, FluentBit, or Redis Streams.")
    println("   - Enable ClickHouse native Async Inserts: SET async_insert = 1")
    println("=================================================================\n")
}

main()

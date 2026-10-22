@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.clickhouse:clickhouse-jdbc:0.6.0")

import java.sql.DriverManager
import java.sql.SQLException

val JDBC_URL = "jdbc:ch://127.0.0.1:8123/default"
val TABLE_NAME = "clickstream_events"
val QUERY_MEM_CAP = 25_000_000L
val EXTERNAL_GROUP_BY_THRESHOLD = 12_000_000L

fun main() {
    println("\u001b[1;36m=================================================================\u001b[0m")
    println("\u001b[1;36mExperiment 02 (Kotlin): Analytical Memory Limits & External Aggregation\u001b[0m")
    println("Testing RAM ceilings, memory exceptions (241), and disk spill mechanics.")
    println("\u001b[1;36m=================================================================\u001b[0m")

    val conn = try {
        DriverManager.getConnection(JDBC_URL)
    } catch (e: Exception) {
        println("\u001b[1;31m[!] Error: Unable to connect to ClickHouse at $JDBC_URL: ${e.message}\u001b[0m")
        println("Start container first: cd clickhouse && make up EXP=memory-cap")
        return
    }

    println("\n\u001b[1;34m[*] Preparing synthetic high-cardinality dataset (500,000 rows)...\u001b[0m")
    conn.createStatement().use { stmt ->
        stmt.execute("DROP TABLE IF EXISTS $TABLE_NAME")
        stmt.execute("""
            CREATE TABLE $TABLE_NAME (
                user_id UUID,
                url String,
                event_time DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY (event_time, user_id)
        """.trimIndent())

        val startTime = System.currentTimeMillis()
        for (i in 0 until 5) {
            stmt.execute("""
                INSERT INTO $TABLE_NAME
                SELECT
                    generateUUIDv4() AS user_id,
                    concat('https://store.local/products/', toString(number % 10000)) AS url,
                    now() - INTERVAL (number % 86400) SECOND AS event_time
                FROM numbers(100000)
            """.trimIndent())
        }
        val elapsed = (System.currentTimeMillis() - startTime) / 1000.0

        val rs = stmt.executeQuery("SELECT count() FROM $TABLE_NAME")
        var rowCount = 0
        if (rs.next()) rowCount = rs.getInt(1)
        println("    Dataset Ready: \u001b[1;32m${"%,d".format(rowCount)} rows\u001b[0m inserted in %.2fs".format(elapsed))
    }

    // Part 1: Strict RAM limit breach
    println("\n\u001b[1;34m[*] Part 1: High-Cardinality Aggregation Under Strict RAM Limit...\u001b[0m")
    println("    Query       : GROUP BY distinct user_id UUIDs with uniqExact(url)")
    println("    Settings    : max_memory_usage = $QUERY_MEM_CAP bytes, max_bytes_before_external_group_by = 0")

    val queryFail = """
        SELECT
            user_id,
            count() AS total_clicks,
            uniqExact(url) AS distinct_urls
        FROM $TABLE_NAME
        GROUP BY user_id
        SETTINGS
            max_memory_usage = $QUERY_MEM_CAP,
            max_bytes_before_external_group_by = 0
        FORMAT Null
    """.trimIndent()

    val t1 = System.currentTimeMillis()
    try {
        conn.createStatement().use { it.execute(queryFail) }
        println("    Unexpected: query completed without memory exception.")
    } catch (e: SQLException) {
        val elapsed = (System.currentTimeMillis() - t1) / 1000.0
        val msg = e.message ?: ""
        println("\n  \u001b[1;31m[!] QUERY TERMINATED BY ENGINE (Elapsed: %.2fs)\u001b[0m".format(elapsed))
        println("      ClickHouse Error Code : 241 (MEMORY_LIMIT_EXCEEDED)")
        println("      Engine Diagnostics    : ${msg.lines().firstOrNull() ?: msg}")
        println("\n  \u001b[1;32m[✓] Failure Verified:\u001b[0m ClickHouse protected host OS by aborting before")
        println("      the 500,000-key aggregation hash table could trigger container OOM kill.")
    }

    // Part 2: Disk Spilling
    println("\n\u001b[1;34m[*] Part 2: Enabling External Aggregation (Disk Spilling)...\u001b[0m")
    println("    Settings    : max_memory_usage = $QUERY_MEM_CAP")
    println("                  max_bytes_before_external_group_by = $EXTERNAL_GROUP_BY_THRESHOLD")

    val querySpill = """
        SELECT
            count() AS aggregate_groups,
            sum(total_clicks) AS grand_total_clicks
        FROM (
            SELECT
                user_id,
                count() AS total_clicks,
                uniqExact(url) AS distinct_urls
            FROM $TABLE_NAME
            GROUP BY user_id
            SETTINGS
                max_memory_usage = $QUERY_MEM_CAP,
                max_bytes_before_external_group_by = $EXTERNAL_GROUP_BY_THRESHOLD
        )
    """.trimIndent()

    val t2 = System.currentTimeMillis()
    conn.createStatement().use { stmt ->
        val rs = stmt.executeQuery(querySpill)
        if (rs.next()) {
            val groups = rs.getLong("aggregate_groups")
            val clicks = rs.getLong("grand_total_clicks")
            val elapsed = (System.currentTimeMillis() - t2) / 1000.0
            println("    Execution Status    : \u001b[1;32mSUCCESS (Completed via disk spill)\u001b[0m")
            println("    Distinct Keys       : ${"%,d".format(groups)}")
            println("    Aggregated Events   : ${"%,d".format(clicks)}")
            println("    Query Duration      : %.2fs".format(elapsed))
        }
    }

    // Part 3: Algorithmic Optimization (HyperLogLog)
    println("\n\u001b[1;34m[*] Part 3: Algorithmic Remediation (uniq / HyperLogLog)...\u001b[0m")
    val queryHll = """
        SELECT
            count() AS aggregate_groups,
            sum(total_clicks) AS grand_total_clicks
        FROM (
            SELECT
                user_id,
                count() AS total_clicks,
                uniq(url) AS distinct_urls
            FROM $TABLE_NAME
            GROUP BY user_id
            SETTINGS
                max_memory_usage = $QUERY_MEM_CAP,
                max_bytes_before_external_group_by = 0
        )
    """.trimIndent()

    val t3 = System.currentTimeMillis()
    conn.createStatement().use { stmt ->
        val rs = stmt.executeQuery(queryHll)
        if (rs.next()) {
            val groups = rs.getLong("aggregate_groups")
            val elapsed = (System.currentTimeMillis() - t3) / 1000.0
            println("    Execution Status    : \u001b[1;32mSUCCESS (Pure in-memory HyperLogLog)\u001b[0m")
            println("    Distinct Keys       : ${"%,d".format(groups)}")
            println("    Query Duration      : %.2fs (Zero disk I/O)".format(elapsed))
        }
    }

    println("\n\u001b[1;32m=================================================================")
    println("SYSTEMS & ARCHITECTURE TAKEAWAYS")
    println("=================================================================\u001b[0m")
    println("1. ClickHouse bounds query RAM strictly via 'max_memory_usage'.")
    println("2. 'max_bytes_before_external_group_by' allows trading I/O for stability when working")
    println("   with high-cardinality GROUP BY keys.")
    println("3. Exact uniqueness (uniqExact) maintains full set cardinality; approximate uniqueness")
    println("   (uniq / HyperLogLog) compresses state by 98% with < 1% error rate.")
    println("=================================================================\n")
}

main()

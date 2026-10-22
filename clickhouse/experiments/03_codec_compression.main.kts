@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.clickhouse:clickhouse-jdbc:0.6.0")

import java.sql.DriverManager

val JDBC_URL = "jdbc:ch://127.0.0.1:8123/default"
val NUM_ROWS = 500_000

fun formatBytes(bytes: Long): String {
    var b = bytes.toDouble()
    for (unit in listOf("B", "KB", "MB", "GB")) {
        if (b < 1024.0) return "%.2f %s".format(b, unit)
        b /= 1024.0
    }
    return "%.2f TB".format(b)
}

fun main() {
    println("\u001b[1;36m======================================================================\u001b[0m")
    println("\u001b[1;36mExperiment 03 (Kotlin): Columnar Codecs & LowCardinality Compression\u001b[0m")
    println("Benchmarking ${"%,d".format(NUM_ROWS)} rows across Uncompressed, LZ4, and Delta+ZSTD.")
    println("\u001b[1;36m======================================================================\u001b[0m\n")

    val conn = try {
        DriverManager.getConnection(JDBC_URL)
    } catch (e: Exception) {
        println("\u001b[1;31m[!] Error: Unable to connect to ClickHouse at $JDBC_URL: ${e.message}\u001b[0m")
        println("Start container first: cd clickhouse && make up EXP=compression")
        return
    }

    conn.createStatement().use { stmt ->
        stmt.execute("DROP TABLE IF EXISTS telemetry_none")
        stmt.execute("DROP TABLE IF EXISTS telemetry_lz4")
        stmt.execute("DROP TABLE IF EXISTS telemetry_optimized")

        // 1. None
        stmt.execute("""
            CREATE TABLE telemetry_none (
                ts DateTime CODEC(NONE),
                region String CODEC(NONE),
                status_code String CODEC(NONE),
                cpu_usage Float64 CODEC(NONE),
                request_bytes UInt64 CODEC(NONE)
            ) ENGINE = MergeTree()
            ORDER BY (region, ts)
        """.trimIndent())

        // 2. LZ4
        stmt.execute("""
            CREATE TABLE telemetry_lz4 (
                ts DateTime CODEC(LZ4),
                region String CODEC(LZ4),
                status_code String CODEC(LZ4),
                cpu_usage Float64 CODEC(LZ4),
                request_bytes UInt64 CODEC(LZ4)
            ) ENGINE = MergeTree()
            ORDER BY (region, ts)
        """.trimIndent())

        // 3. Optimized: Delta + ZSTD + LowCardinality
        stmt.execute("""
            CREATE TABLE telemetry_optimized (
                ts DateTime CODEC(DoubleDelta, ZSTD(3)),
                region LowCardinality(String) CODEC(ZSTD(3)),
                status_code LowCardinality(String) CODEC(ZSTD(3)),
                cpu_usage Float64 CODEC(Gorilla, ZSTD(3)),
                request_bytes UInt64 CODEC(T64, ZSTD(3))
            ) ENGINE = MergeTree()
            ORDER BY (region, ts)
        """.trimIndent())

        println("[*] Inserting ${"%,d".format(NUM_ROWS)} synthetic events into each table...")
        val tables = listOf("telemetry_none", "telemetry_lz4", "telemetry_optimized")
        for (table in tables) {
            val t0 = System.currentTimeMillis()
            stmt.execute("""
                INSERT INTO $table
                SELECT
                    toDateTime('2026-01-01 00:00:00') + INTERVAL number SECOND AS ts,
                    ['us-east-1', 'us-west-2', 'eu-west-1', 'ap-southeast-1'][1 + (number % 4)] AS region,
                    ['200', '404', '500', '302'][1 + (number % 4)] AS status_code,
                    50.0 + (number % 100) * 0.5 AS cpu_usage,
                    1024 + (number % 8192) AS request_bytes
                FROM numbers($NUM_ROWS)
            """.trimIndent())
            stmt.execute("OPTIMIZE TABLE $table FINAL")
            val elapsed = (System.currentTimeMillis() - t0) / 1000.0
            println("    Seeded and optimized $table in %.2fs".format(elapsed))
        }

        println("\n\u001b[1;34m[*] Physical Disk Footprint Comparison (from system.columns):\u001b[0m")
        println("-------------------------------------------------------------------------------------")
        println("%-22s %-16s %-16s %-12s".format("Encoding Strategy", "Uncompressed", "Compressed", "Ratio"))
        println("-------------------------------------------------------------------------------------")

        var uncompressedBase = 1L
        for (table in tables) {
            val rs = stmt.executeQuery("""
                SELECT
                    sum(data_compressed_bytes) as compressed,
                    sum(data_uncompressed_bytes) as uncompressed
                FROM system.columns
                WHERE database = 'default' AND table = '$table'
            """.trimIndent())
            if (rs.next()) {
                val comp = rs.getLong("compressed")
                val uncomp = rs.getLong("uncompressed")
                if (table == "telemetry_none") uncompressedBase = comp

                val ratio = if (comp > 0) uncompressedBase.toDouble() / comp.toDouble() else 1.0
                val label = when (table) {
                    "telemetry_none" -> "Raw Uncompressed"
                    "telemetry_lz4" -> "Standard LZ4"
                    "telemetry_optimized" -> "Specialized Codecs"
                    else -> table
                }
                println("%-22s %-16s %-16s %-12s".format(
                    label,
                    formatBytes(uncomp),
                    formatBytes(comp),
                    "%.2fx".format(ratio)
                ))
            }
        }
        println("-------------------------------------------------------------------------------------")
    }

    println("\n\u001b[1;32m=================================================================")
    println("COLUMNAR ARCHITECTURE TAKEAWAYS")
    println("=================================================================\u001b[0m")
    println("1. Columnar layout clusters values of the same type and distribution contiguously.")
    println("2. DoubleDelta + ZSTD compresses monotonically increasing timestamps to < 1 bit/value.")
    println("3. LowCardinality replaces repetitive strings with dictionary indices.")
    println("4. 10x+ storage reduction drastically cuts NVMe SSD spend and disk I/O scan bottlenecks.")
    println("=================================================================\n")
}

main()

#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.github.ajalt.mordant:mordant-jvm:2.4.0")
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
import java.math.BigDecimal
import java.sql.Connection
import java.sql.DriverManager
import kotlin.system.exitProcess

val DB_URL = "jdbc:postgresql://127.0.0.1:5432/lab_db"
val DB_USER = "postgres"
val DB_PASS = "postgres"
val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

val TABLE_NAME = "crash_course_items"

fun getConn(): Connection = DriverManager.getConnection(DB_URL, DB_USER, DB_PASS)

fun main() {
    println("\n" + boldCyan("=".repeat(70)))
    println(boldGreen("PostgreSQL Crash Course (Kotlin / JDBC): Relational Fundamentals"))
    println(boldCyan("=".repeat(70)) + "\n")

    val conn =
        try {
            getConn()
        } catch (e: Exception) {
            println(boldRed("[!] Connection failed: ${e.message}"))
            println("Start the lab container first: cd postgres && make up EXP=isolation")
            exitProcess(1)
        }

    conn.use { c ->
        // 1. Connectivity & Version
        println(boldBlue("[*] 1. Server Version & Connectivity"))
        c.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT version();")
            rs.next()
            println("    Version: ${rs.getString(1).split(",")[0]}")
        }

        // 2. Schema DDL
        println("\n" + boldBlue("[*] 2. Creating Demo Table"))
        c.createStatement().use { stmt ->
            stmt.execute("DROP TABLE IF EXISTS $TABLE_NAME;")
            stmt.execute(
                """
                CREATE TABLE $TABLE_NAME (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    sku TEXT UNIQUE NOT NULL,
                    price NUMERIC(10, 2) NOT NULL,
                    stock INT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """.trimIndent(),
            )
            println("    Created table '$TABLE_NAME'.")
        }

        // 3. Parameterized Insert & Batch Insert
        println("\n" + boldBlue("[*] 3. Parameterized Inserts & Reads"))
        c.prepareStatement(
            "INSERT INTO $TABLE_NAME (name, sku, price, stock) VALUES (?, ?, ?, ?) RETURNING id;",
        ).use { ps ->
            ps.setString(1, "Mechanical Keyboard")
            ps.setString(2, "KEY-001")
            ps.setBigDecimal(3, BigDecimal("129.99"))
            ps.setInt(4, 15)
            val rs = ps.executeQuery()
            rs.next()
            println("    Inserted single row with ID: ${rs.getInt(1)}")
        }

        c.prepareStatement(
            "INSERT INTO $TABLE_NAME (name, sku, price, stock) VALUES (?, ?, ?, ?);",
        ).use { ps ->
            val batch =
                listOf(
                    Triple("Gaming Mouse", "MOU-002", BigDecimal("59.50")) to 40,
                    Triple("UltraWide Monitor", "MON-003", BigDecimal("399.00")) to 8,
                    Triple("Desk Mat", "MAT-004", BigDecimal("25.00")) to 100,
                )
            for ((item, stock) in batch) {
                ps.setString(1, item.first)
                ps.setString(2, item.second)
                ps.setBigDecimal(3, item.third)
                ps.setInt(4, stock)
                ps.addBatch()
            }
            ps.executeBatch()
            println("    Batch-inserted ${batch.size} additional rows.")
        }

        // Read back
        c.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT id, name, sku, price, stock FROM $TABLE_NAME ORDER BY id;")
            println("    Reading items from $TABLE_NAME:")
            while (rs.next()) {
                val id = rs.getInt("id")
                val name = rs.getString("name")
                val sku = rs.getString("sku")
                val price = rs.getBigDecimal("price")
                val stock = rs.getInt("stock")
                println("      - ID $id: $name ($sku) -> \$$price [Stock: $stock]")
            }
        }
    }

    // 4. Transactions: Commit vs Rollback
    println("\n" + boldBlue("[*] 4. Transaction Semantics (Atomic Rollback)"))
    var initialCount = 0
    getConn().use { c ->
        c.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT count(*) FROM $TABLE_NAME;")
            rs.next()
            initialCount = rs.getInt(1)
        }

        c.autoCommit = false
        try {
            c.prepareStatement(
                "INSERT INTO $TABLE_NAME (name, sku, price, stock) VALUES (?, ?, ?, ?);",
            ).use { ps ->
                ps.setString(1, "Aborted Product")
                ps.setString(2, "ABT-999")
                ps.setBigDecimal(3, BigDecimal("9.99"))
                ps.setInt(4, 1)
                ps.executeUpdate()
            }
            println("    Explicitly rolling back transaction...")
            c.rollback()
        } finally {
            c.autoCommit = true
        }

        c.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT count(*) FROM $TABLE_NAME;")
            rs.next()
            val postCount = rs.getInt(1)
            check(postCount == initialCount) { "Rollback invariant violated!" }
            println("    Verified row count unchanged ($postCount rows). Rollback succeeded.")
        }

        // 5. Diagnostics: pg_stat_activity & Table Footprint
        println("\n" + boldBlue("[*] 5. Diagnostic Queries & Catalog Metadata"))
        c.createStatement().use { stmt ->
            val rs =
                stmt.executeQuery(
                    """
                    SELECT
                        pg_size_pretty(pg_total_relation_size('$TABLE_NAME')) AS total_size,
                        pg_size_pretty(pg_relation_size('$TABLE_NAME')) AS table_size,
                        pg_size_pretty(pg_indexes_size('$TABLE_NAME')) AS index_size;
                    """.trimIndent(),
                )
            rs.next()
            val total = rs.getString("total_size")
            val heap = rs.getString("table_size")
            val idx = rs.getString("index_size")
            println("    Table '$TABLE_NAME' Footprint: Total: $total, Heap: $heap, Indexes: $idx")
        }

        // Cleanup
        c.createStatement().use { stmt ->
            stmt.execute("DROP TABLE IF EXISTS $TABLE_NAME;")
            println("    Cleaned up demo table '$TABLE_NAME'.")
        }
    }

    println("\n" + boldGreen("[✓] PostgreSQL Crash Course completed successfully!") + "\n")
}

main()

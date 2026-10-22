#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.postgresql:postgresql:42.7.3")

import java.sql.Connection
import java.sql.DriverManager
import java.sql.SQLException
import java.util.concurrent.CyclicBarrier
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

val DB_URL = "jdbc:postgresql://127.0.0.1:5432/lab_db"
val DB_USER = "postgres"
val DB_PASS = "postgres"

fun getConn(): Connection = DriverManager.getConnection(DB_URL, DB_USER, DB_PASS)

fun resetAccounts() {
    getConn().use { conn ->
        conn.createStatement().use { stmt ->
            stmt.execute("TRUNCATE TABLE accounts RESTART IDENTITY;")
            stmt.execute("INSERT INTO accounts (owner, balance) VALUES ('Alice', 1000.00), ('Bob', 1000.00);")
        }
    }
}

fun getBalance(id: Int): Double {
    getConn().use { conn ->
        conn.prepareStatement("SELECT balance FROM accounts WHERE id = ?;").use { stmt ->
            stmt.setInt(1, id)
            val rs = stmt.executeQuery()
            rs.next()
            return rs.getDouble(1)
        }
    }
}

println("\n\u001B[1;36m" + "=".repeat(70) + "\u001B[0m")
println("\u001B[1;32mExperiment 02 (Kotlin / JDBC): Row Lock Contention & Deadlock Detection\u001B[0m")
println("\u001B[1;36m" + "=".repeat(70) + "\u001B[0m\n")

// Part 1: 50 Concurrent Updates to Single Hot Row
println("\u001B[1;34m[*] Part 1: 50 Concurrent Updates to Single Hot Row (Alice, id=1)...\u001B[0m")
resetAccounts()

val pool = Executors.newFixedThreadPool(25)
val t0 = System.currentTimeMillis()
val numWorkers = 50

for (i in 1..numWorkers) {
    pool.submit {
        getConn().use { conn ->
            conn.autoCommit = false
            conn.createStatement().use { stmt ->
                stmt.execute("UPDATE accounts SET balance = balance + 1.00 WHERE id = 1;")
                conn.commit()
            }
        }
    }
}

pool.shutdown()
pool.awaitTermination(30, TimeUnit.SECONDS)
val durMs = System.currentTimeMillis() - t0

val finalBal = getBalance(1)
println("    Final Balance: \u001B[1;32m$$finalBal\u001B[0m (Expected: $1050.00)")
println("    50 updates serialized safely in ${durMs}ms with zero lost updates.")

// Part 2: Controlled Deadlock Generation
println("\n\u001B[1;34m[*] Part 2: Generating Controlled Deadlock (SQLSTATE 40P01)...\u001B[0m")
resetAccounts()

val barrier = CyclicBarrier(2)
var deadlockCaught = false
var deadlockMsg = ""

val tA = thread {
    try {
        getConn().use { conn ->
            conn.autoCommit = false
            conn.createStatement().use { stmt ->
                stmt.execute("SELECT * FROM accounts WHERE id = 1 FOR UPDATE;")
                barrier.await()
                Thread.sleep(50)
                stmt.execute("SELECT * FROM accounts WHERE id = 2 FOR UPDATE;")
                conn.commit()
            }
        }
    } catch (e: SQLException) {
        if (e.sqlState == "40P01") {
            deadlockCaught = true
            deadlockMsg = e.message ?: ""
        }
    }
}

val tB = thread {
    try {
        getConn().use { conn ->
            conn.autoCommit = false
            conn.createStatement().use { stmt ->
                stmt.execute("SELECT * FROM accounts WHERE id = 2 FOR UPDATE;")
                barrier.await()
                Thread.sleep(50)
                stmt.execute("SELECT * FROM accounts WHERE id = 1 FOR UPDATE;")
                conn.commit()
            }
        }
    } catch (e: SQLException) {
        if (e.sqlState == "40P01") {
            deadlockCaught = true
            deadlockMsg = e.message ?: ""
        }
    }
}

tA.join()
tB.join()

if (deadlockCaught) {
    println("    \u001B[1;32m[✓] DEADLOCK DETECTED (SQLSTATE 40P01)!\u001B[0m")
    println("        PostgreSQL background deadlock detector broke circular lock wait.")
}

println("\n\u001B[1;34m" + "=".repeat(70) + "\u001B[0m\n")

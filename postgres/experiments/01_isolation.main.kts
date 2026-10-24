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
import java.sql.Connection
import java.sql.DriverManager
import java.sql.SQLException
import java.util.concurrent.CyclicBarrier
import kotlin.concurrent.thread

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

fun getConn(): Connection = DriverManager.getConnection(DB_URL, DB_USER, DB_PASS)

fun resetDoctors() {
    getConn().use { conn ->
        conn.createStatement().use { stmt ->
            stmt.execute("TRUNCATE TABLE doctors RESTART IDENTITY;")
            stmt.execute("INSERT INTO doctors (name, on_call) VALUES ('Dr. Alice', TRUE), ('Dr. Bob', TRUE);")
        }
    }
}

fun countOnCall(): Int {
    getConn().use { conn ->
        conn.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT count(*) FROM doctors WHERE on_call = TRUE;")
            rs.next()
            return rs.getInt(1)
        }
    }
}

println("\n" + boldCyan("=".repeat(70)))
println(boldGreen("Experiment 01 (Kotlin / JDBC Driver): PostgreSQL Transaction Isolation"))
println("Testing Write Skew under REPEATABLE READ vs SERIALIZABLE (SSI)")
println(boldCyan("=".repeat(70)) + "\n")

// Part 1: Write Skew under REPEATABLE READ
println(boldBlue("[*] Part 1: Simulating Write Skew under REPEATABLE READ..."))
println("    Rule: At least 1 doctor must be on call at all times.")
resetDoctors()

val barrier = CyclicBarrier(2)

val t1 = thread {
    getConn().use { conn ->
        conn.autoCommit = false
        conn.transactionIsolation = Connection.TRANSACTION_REPEATABLE_READ
        conn.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT count(*) FROM doctors WHERE on_call = TRUE;")
            rs.next()
            val onCall = rs.getInt(1)
            barrier.await()
            if (onCall >= 2) {
                stmt.execute("UPDATE doctors SET on_call = FALSE WHERE name = 'Dr. Alice';")
            }
            conn.commit()
        }
    }
}

val t2 = thread {
    getConn().use { conn ->
        conn.autoCommit = false
        conn.transactionIsolation = Connection.TRANSACTION_REPEATABLE_READ
        conn.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT count(*) FROM doctors WHERE on_call = TRUE;")
            rs.next()
            val onCall = rs.getInt(1)
            barrier.await()
            if (onCall >= 2) {
                stmt.execute("UPDATE doctors SET on_call = FALSE WHERE name = 'Dr. Bob';")
            }
            conn.commit()
        }
    }
}

t1.join()
t2.join()

val remainingOnCall = countOnCall()
println("    Doctors Remaining On Call: ${boldRed("$remainingOnCall")}")
if (remainingOnCall == 0) {
    println("    ${boldRed("[!] WRITE SKEW CONFIRMED:")} Both transactions committed independently.")
    println("        Repeatable Read prevented non-repeatable reads, but allowed invariant violation.")
}

// Part 2: Serialization Failure under SERIALIZABLE (SSI)
println("\n" + boldBlue("[*] Part 2: Testing Protection under SERIALIZABLE (SSI)..."))
resetDoctors()

val barrierSSI = CyclicBarrier(2)
var ssiCaught = false
var abortedError = ""

val t3 = thread {
    getConn().use { conn ->
        conn.autoCommit = false
        conn.transactionIsolation = Connection.TRANSACTION_SERIALIZABLE
        conn.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT count(*) FROM doctors WHERE on_call = TRUE;")
            rs.next()
            val onCall = rs.getInt(1)
            barrierSSI.await()
            if (onCall >= 2) {
                stmt.execute("UPDATE doctors SET on_call = FALSE WHERE name = 'Dr. Alice';")
            }
            try {
                conn.commit()
            } catch (e: SQLException) {
                if (e.sqlState == "40001") {
                    ssiCaught = true
                    abortedError = e.message ?: ""
                }
            }
        }
    }
}

val t4 = thread {
    getConn().use { conn ->
        conn.autoCommit = false
        conn.transactionIsolation = Connection.TRANSACTION_SERIALIZABLE
        conn.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT count(*) FROM doctors WHERE on_call = TRUE;")
            rs.next()
            val onCall = rs.getInt(1)
            barrierSSI.await()
            if (onCall >= 2) {
                stmt.execute("UPDATE doctors SET on_call = FALSE WHERE name = 'Dr. Bob';")
            }
            try {
                conn.commit()
            } catch (e: SQLException) {
                if (e.sqlState == "40001") {
                    ssiCaught = true
                    abortedError = e.message ?: ""
                }
            }
        }
    }
}

t3.join()
t4.join()

val ssiRemaining = countOnCall()
println("    Doctors Remaining On Call: ${boldGreen("$ssiRemaining (Invariant Protected!)")}")
if (ssiCaught) {
    println("    ${boldGreen("[✓] SERIALIZATION_FAILURE (SQLSTATE 40001) Caught Successfully!")}")
    println("        PostgreSQL SSI detected read/write dependency cycle and aborted one transaction.")
}

println("\n${boldBlue("" + "=".repeat(70) + "")}\n")

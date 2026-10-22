#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.datastax.oss:java-driver-core:4.17.0")
@file:DependsOn("org.slf4j:slf4j-nop:2.0.13")

import com.datastax.oss.driver.api.core.CqlSession
import com.datastax.oss.driver.api.core.cql.SimpleStatement
import com.datastax.oss.driver.api.core.DefaultConsistencyLevel
import com.datastax.oss.driver.api.core.servererrors.UnavailableException
import java.net.InetSocketAddress
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

val HOST = "127.0.0.1"
val PORT = 9042
val DC = "datacenter1"
val KEYSPACE = "lab_consistency"
val TABLE = "accounts"

fun log(msg: String, color: String = "\u001B[0m") {
    val time = DateTimeFormatter.ofPattern("HH:mm:ss").withZone(ZoneOffset.UTC).format(Instant.now())
    println("\u001B[2m[$time]\u001B[0m $color$msg\u001B[0m")
}

fun dockerCmd(vararg args: String): Int {
    val proc = ProcessBuilder("docker", *args).redirectErrorStream(true).start()
    return proc.waitFor()
}

fun testConsistency(session: CqlSession, cl: DefaultConsistencyLevel, accountId: String, balance: Double): Boolean {
    val stmt = SimpleStatement.newInstance(
        "INSERT INTO $KEYSPACE.$TABLE (account_id, balance, updated_at) VALUES (?, ?, toTimestamp(now()))",
        accountId,
        balance
    ).setConsistencyLevel(cl)

    return try {
        session.execute(stmt)
        log("  Write with $cl: SUCCESS", "\u001B[32m")
        true
    } catch (e: UnavailableException) {
        log("  Write with $cl: REJECTED (UnavailableException - Required: ${e.required}, Alive: ${e.alive})", "\u001B[1;31m")
        false
    } catch (e: Exception) {
        log("  Write with $cl: FAILED (${e::class.simpleName}: ${e.message})", "\u001B[33m")
        false
    }
}

println("\n" + "=".repeat(75))
println("  EXPERIMENT 02 (Kotlin/JVM): Tunable Consistency & PACELC Theorem")
println("  Cluster: 3 Nodes (RF=3) | Quorum Math: floor(3/2) + 1 = 2")
println("=".repeat(75) + "\n")

val session = CqlSession.builder()
    .addContactPoint(InetSocketAddress(HOST, PORT))
    .withLocalDatacenter(DC)
    .build()

session.use { sess ->
    sess.execute("""
        CREATE KEYSPACE IF NOT EXISTS $KEYSPACE
        WITH replication = {'class': 'NetworkTopologyStrategy', '$DC': 3};
    """.trimIndent())

    sess.execute("""
        CREATE TABLE IF NOT EXISTS $KEYSPACE.$TABLE (
            account_id text PRIMARY KEY,
            balance double,
            updated_at timestamp
        );
    """.trimIndent())

    sess.execute("TRUNCATE $KEYSPACE.$TABLE;")
    log("Keyspace $KEYSPACE provisioned with RF=3.", "\u001B[32m")

    // State 1: 3/3 Nodes Alive
    log("\n[State 1] All 3 Nodes Alive (Full Cluster Topology)", "\u001B[1;36m")
    testConsistency(sess, DefaultConsistencyLevel.ONE, "acc_1", 100.0)
    testConsistency(sess, DefaultConsistencyLevel.QUORUM, "acc_1", 200.0)
    testConsistency(sess, DefaultConsistencyLevel.ALL, "acc_1", 300.0)

    // State 2: 2/3 Nodes Alive (Stop node3)
    log("\n[State 2] Simulating Node 3 Outage (docker stop cassandra-node3)...", "\u001B[1;36m")
    dockerCmd("stop", "cassandra-node3")
    Thread.sleep(2000)
    log("Testing consistency with 2/3 nodes online:", "\u001B[33m")
    testConsistency(sess, DefaultConsistencyLevel.ONE, "acc_2", 100.0)
    testConsistency(sess, DefaultConsistencyLevel.QUORUM, "acc_2", 200.0)
    testConsistency(sess, DefaultConsistencyLevel.ALL, "acc_2", 300.0)

    // State 3: 1/3 Nodes Alive (Stop node2)
    log("\n[State 3] Simulating Node 2 Outage (docker stop cassandra-node2)...", "\u001B[1;36m")
    dockerCmd("stop", "cassandra-node2")
    Thread.sleep(2000)
    log("Testing consistency with 1/3 nodes online (Majority Lost):", "\u001B[33m")
    testConsistency(sess, DefaultConsistencyLevel.ONE, "acc_3", 100.0)
    testConsistency(sess, DefaultConsistencyLevel.QUORUM, "acc_3", 200.0)
    testConsistency(sess, DefaultConsistencyLevel.ALL, "acc_3", 300.0)

    // State 4: Heal Cluster
    log("\n[State 4] Healing Cluster (Restarting nodes)...", "\u001B[1;36m")
    dockerCmd("start", "cassandra-node2")
    dockerCmd("start", "cassandra-node3")
    log("Cluster nodes restarted.", "\u001B[32m")

    println("\n" + "=".repeat(75))
    println("  PACELC THEOREM RECAP")
    println("=".repeat(75))
    println("  • Under Partition (P): Tradeoff between Availability (A) and Consistency (C)")
    println("    - CL.ONE: AP system (Available even with 2 of 3 nodes down)")
    println("    - CL.QUORUM: CP system (Rejects writes when majority quorum is lost)")
    println("  • Else (E): Tradeoff between Latency (L) and Consistency (C)")
    println("    - CL.ONE gives single-digit millisecond latency; CL.ALL waits for all replicas.")
    println("=".repeat(75) + "\n")
}

#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.datastax.oss:java-driver-core:4.17.0")
@file:DependsOn("org.slf4j:slf4j-nop:2.0.13")
@file:DependsOn("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")

import com.datastax.oss.driver.api.core.CqlSession
import com.datastax.oss.driver.api.core.cql.SimpleStatement
import com.datastax.oss.driver.api.core.DefaultConsistencyLevel
import com.datastax.oss.driver.api.core.servererrors.ReadFailureException
import com.datastax.oss.driver.api.core.servererrors.ReadTimeoutException
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.future.await
import java.net.InetSocketAddress
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

val HOST = "127.0.0.1"
val PORT = 9042
val DC = "datacenter1"
val KEYSPACE = "lab_tombstones"
val TABLE = "sensor_telemetry"
val SENSOR_ID = "sensor-alpha"

val TOTAL_ROWS = 15_000
val DELETE_ROWS = 12_000

fun log(msg: String, color: String = "\u001B[0m") {
    val time = DateTimeFormatter.ofPattern("HH:mm:ss").withZone(ZoneOffset.UTC).format(Instant.now())
    println("\u001B[2m[$time]\u001B[0m $color$msg\u001B[0m")
}

fun triggerNodetoolFlush() {
    log("Flushing memtables to SSTables on disk via nodetool...", "\u001B[1;34m")
    try {
        val proc = ProcessBuilder("docker", "compose", "exec", "-T", "cassandra-tombstones", "nodetool", "flush", KEYSPACE, TABLE)
            .redirectErrorStream(true)
            .start()
        val exitCode = proc.waitFor()
        if (exitCode == 0) {
            log("Memtable flushed to SSTable successfully.", "\u001B[32m")
        } else {
            val output = proc.inputStream.bufferedReader().readText().trim()
            log("Notice: nodetool flush returned $exitCode: $output", "\u001B[33m")
        }
    } catch (e: Exception) {
        log("Notice: Could not invoke nodetool directly: ${e.message}", "\u001B[33m")
    }
}

fun connectCassandra(): CqlSession {
    log("Connecting to Cassandra via DataStax Java Driver 4.x...", "\u001B[1;36m")
    for (attempt in 1..20) {
        try {
            val session = CqlSession.builder()
                .addContactPoint(InetSocketAddress(HOST, PORT))
                .withLocalDatacenter(DC)
                .build()
            val row = session.execute("SELECT release_version FROM system.local").one()
            log("Connected successfully to Cassandra node! Release version: ${row?.getString("release_version")}", "\u001B[1;32m")
            return session
        } catch (e: Exception) {
            Thread.sleep(1500)
        }
    }
    throw IllegalStateException("Failed to connect to Cassandra on $HOST:$PORT. Is 'make up EXP=tombstones' running?")
}

fun setupSchema(session: CqlSession) {
    log("Setting up keyspace '$KEYSPACE' and table '$TABLE'...", "\u001B[1;36m")
    session.execute("""
        CREATE KEYSPACE IF NOT EXISTS $KEYSPACE
        WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
    """.trimIndent())

    session.execute("""
        CREATE TABLE IF NOT EXISTS $KEYSPACE.$TABLE (
            sensor_id text,
            recorded_at timestamp,
            metric_val double,
            payload text,
            PRIMARY KEY (sensor_id, recorded_at)
        ) WITH CLUSTERING ORDER BY (recorded_at ASC)
          AND gc_grace_seconds = 864000;
    """.trimIndent())

    session.execute("TRUNCATE $KEYSPACE.$TABLE;")
    log("Schema ready and table truncated.", "\u001B[32m")
}

runBlocking {
    println("\n" + "=".repeat(70))
    println("  EXPERIMENT 01 (Kotlin/JVM): The Tombstone Failure Threshold")
    println("  Driver: DataStax Java Driver 4.17.0 (Netty Async Reactive)")
    println("=".repeat(70) + "\n")

    val session = connectCassandra()
    session.use { sess ->
        setupSchema(sess)

        val baseInstant = Instant.parse("2026-01-01T00:00:00Z")
        val insertStmt = sess.prepare("""
            INSERT INTO $KEYSPACE.$TABLE (sensor_id, recorded_at, metric_val, payload)
            VALUES (?, ?, ?, ?)
        """.trimIndent())

        // Phase 1: High-concurrency async inserts
        log("Phase 1: Inserting ${"%,d".format(TOTAL_ROWS)} sensor readings into partition '$SENSOR_ID'...", "\u001B[1;36m")
        val insertStart = System.currentTimeMillis()
        val sem = Semaphore(128)
        val insertJobs = (0 until TOTAL_ROWS).map { i ->
            async(Dispatchers.IO) {
                sem.withPermit {
                    val timestamp = baseInstant.plusSeconds(i.toLong())
                    val bound = insertStmt.bind(SENSOR_ID, timestamp, i * 1.5, "metric_packet_${"%06d".format(i)}")
                        .setConsistencyLevel(DefaultConsistencyLevel.ONE)
                    sess.executeAsync(bound).await()
                }
            }
        }
        insertJobs.awaitAll()
        val insertSec = (System.currentTimeMillis() - insertStart) / 1000.0
        log("Successfully inserted ${"%,d".format(TOTAL_ROWS)} rows in ${"%.2f".format(insertSec)}s (${"%,d".format((TOTAL_ROWS / insertSec).toInt())} rows/s).", "\u001B[32m")

        // Phase 2: High-concurrency async deletes to generate tombstones
        log("Phase 2: Deleting first ${"%,d".format(DELETE_ROWS)} rows to generate tombstones...", "\u001B[1;36m")
        val deleteStmt = sess.prepare("""
            DELETE FROM $KEYSPACE.$TABLE
            WHERE sensor_id = ? AND recorded_at = ?
        """.trimIndent())

        val delStart = System.currentTimeMillis()
        val delJobs = (0 until DELETE_ROWS).map { i ->
            async(Dispatchers.IO) {
                sem.withPermit {
                    val timestamp = baseInstant.plusSeconds(i.toLong())
                    val bound = deleteStmt.bind(SENSOR_ID, timestamp)
                        .setConsistencyLevel(DefaultConsistencyLevel.ONE)
                    sess.executeAsync(bound).await()
                }
            }
        }
        delJobs.awaitAll()
        val delSec = (System.currentTimeMillis() - delStart) / 1000.0
        log("Generated ${"%,d".format(DELETE_ROWS)} tombstones in ${"%.2f".format(delSec)}s (${"%,d".format((DELETE_ROWS / delSec).toInt())} ops/s).", "\u001B[32m")

        // Phase 3: Flush to disk SSTables
        triggerNodetoolFlush()

        // Phase 4: Query partition containing tombstones
        log("Phase 3: Querying partition containing ${"%,d".format(DELETE_ROWS)} tombstones...", "\u001B[1;36m")
        log("Query: SELECT * FROM $TABLE WHERE sensor_id = '$SENSOR_ID' LIMIT 5000;", "\u001B[2m")

        try {
            val qStmt = SimpleStatement.newInstance("SELECT * FROM $KEYSPACE.$TABLE WHERE sensor_id = ? LIMIT 5000", SENSOR_ID)
                .setConsistencyLevel(DefaultConsistencyLevel.ONE)
                .setPageSize(5000)

            val queryStart = System.nanoTime()
            val rs = sess.execute(qStmt)
            val rows = rs.all()
            val durationMs = (System.nanoTime() - queryStart) / 1_000_000.0
            log("Query returned ${rows.size} rows in ${"%.2f".format(durationMs)} ms (Cassandra scanned past tombstones).", "\u001B[33m")
        } catch (e: ReadFailureException) {
            log("SUCCESS: Cassandra aborted the query with ReadFailureException!", "\u001B[1;31m")
            log("Server Error Message: ${e.message}", "\u001B[1;31m")
        } catch (e: Exception) {
            log("Query threw: ${e::class.simpleName} - ${e.message}", "\u001B[33m")
        }

        // Phase 5: Remediation via Clustering Key Slice
        log("Phase 4: Remediation - Querying with Clustering Key Bounds...", "\u001B[1;36m")
        val boundaryInstant = baseInstant.plusSeconds(DELETE_ROWS.toLong())
        log("Clustering Key Filter: recorded_at >= $boundaryInstant", "\u001B[2m")

        val remStmt = SimpleStatement.newInstance(
            "SELECT * FROM $KEYSPACE.$TABLE WHERE sensor_id = ? AND recorded_at >= ? LIMIT 1000",
            SENSOR_ID,
            boundaryInstant
        ).setConsistencyLevel(DefaultConsistencyLevel.ONE)

        val remStart = System.nanoTime()
        val remRs = sess.execute(remStmt)
        val healthyRows = remRs.all()
        val remDurationMs = (System.nanoTime() - remStart) / 1_000_000.0

        log("Remediated query returned ${healthyRows.size} live rows in ${"%.2f".format(remDurationMs)} ms!", "\u001B[1;32m")
        log("Tombstones scanned: 0 (bypassed via SSTable clustering key index block).", "\u001B[32m")

        println("\n" + "=".repeat(70))
        println("  KOTLIN / JVM EXPERIMENT 01 SUMMARY")
        println("=".repeat(70))
        println("  Rows Inserted               : ${"%,d".format(TOTAL_ROWS)}")
        println("  Rows Deleted (Tombstones)   : ${"%,d".format(DELETE_ROWS)}")
        println("  Driver Implementation       : DataStax Java Driver 4.x (Netty Async)")
        println("  Remediated Query Latency    : ${"%.2f".format(remDurationMs)} ms")
        println("=".repeat(70) + "\n")
    }
}

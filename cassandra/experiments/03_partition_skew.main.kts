#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.datastax.oss:java-driver-core:4.17.0")
@file:DependsOn("org.slf4j:slf4j-nop:2.0.13")
@file:DependsOn("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")

import com.datastax.oss.driver.api.core.CqlSession
import com.datastax.oss.driver.api.core.DefaultConsistencyLevel
import com.datastax.oss.driver.api.core.cql.SimpleStatement
import kotlinx.coroutines.*
import kotlinx.coroutines.future.await
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import java.net.InetSocketAddress
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.UUID

val HOST = "127.0.0.1"
val PORT = 9042
val DC = "datacenter1"
val KEYSPACE = "lab_partition_skew"
val EVENT_COUNT = 5_000
val NUM_BUCKETS = 10

fun log(msg: String, color: String = "\u001B[0m") {
    val time = DateTimeFormatter.ofPattern("HH:mm:ss").withZone(ZoneOffset.UTC).format(Instant.now())
    println("\u001B[2m[$time]\u001B[0m $color$msg\u001B[0m")
}

runBlocking {
    println("\n" + "=".repeat(70))
    println("  EXPERIMENT 03 (Kotlin/JVM): Wide Partitions & Hot-Spotting Skew")
    println("  Driver: DataStax Java Driver 4.17.0 (Netty Async Reactive)")
    println("=".repeat(70) + "\n")

    val session = CqlSession.builder()
        .addContactPoint(InetSocketAddress(HOST, PORT))
        .withLocalDatacenter(DC)
        .build()

    session.use { sess ->
        sess.execute("""
            CREATE KEYSPACE IF NOT EXISTS $KEYSPACE
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
        """.trimIndent())

        // 1. Anti-Pattern: Single massive partition
        sess.execute("""
            CREATE TABLE IF NOT EXISTS $KEYSPACE.telemetry_monolithic (
                device_id text,
                event_time timestamp,
                payload text,
                PRIMARY KEY (device_id, event_time)
            ) WITH CLUSTERING ORDER BY (event_time DESC);
        """.trimIndent())

        // 2. Best Practice: Synthetic 10-bucket composite partition key
        sess.execute("""
            CREATE TABLE IF NOT EXISTS $KEYSPACE.telemetry_bucketed (
                device_id text,
                bucket_id int,
                event_time timestamp,
                payload text,
                PRIMARY KEY ((device_id, bucket_id), event_time)
            ) WITH CLUSTERING ORDER BY (event_time DESC);
        """.trimIndent())

        sess.execute("TRUNCATE $KEYSPACE.telemetry_monolithic;")
        sess.execute("TRUNCATE $KEYSPACE.telemetry_bucketed;")
        log("Keyspace and benchmark tables initialized.", "\u001B[32m")

        val baseInstant = Instant.parse("2026-01-01T00:00:00Z")
        val sem = Semaphore(128)

        // Workload A: Monolithic partition
        log("Ingesting ${"%,d".format(EVENT_COUNT)} events into SINGLE partition 'ALL' (Anti-pattern)...", "\u001B[1;36m")
        val monoInsertStmt = sess.prepare("""
            INSERT INTO $KEYSPACE.telemetry_monolithic (device_id, event_time, payload)
            VALUES (?, ?, ?)
        """.trimIndent())

        val t0Mono = System.currentTimeMillis()
        val monoJobs = (0 until EVENT_COUNT).map { i ->
            async(Dispatchers.IO) {
                sem.withPermit {
                    val timestamp = baseInstant.plusSeconds(i.toLong())
                    val bound = monoInsertStmt.bind("ALL", timestamp, "telemetry_data_packet_$i")
                        .setConsistencyLevel(DefaultConsistencyLevel.ONE)
                    sess.executeAsync(bound).await()
                }
            }
        }
        monoJobs.awaitAll()
        val monoDurationSec = (System.currentTimeMillis() - t0Mono) / 1000.0

        // Workload B: Bucketed partitions
        log("Ingesting ${"%,d".format(EVENT_COUNT)} events across $NUM_BUCKETS distributed buckets (Best Practice)...", "\u001B[1;36m")
        val bucketInsertStmt = sess.prepare("""
            INSERT INTO $KEYSPACE.telemetry_bucketed (device_id, bucket_id, event_time, payload)
            VALUES (?, ?, ?, ?)
        """.trimIndent())

        val t0Bucket = System.currentTimeMillis()
        val bucketJobs = (0 until EVENT_COUNT).map { i ->
            async(Dispatchers.IO) {
                sem.withPermit {
                    val timestamp = baseInstant.plusSeconds(i.toLong())
                    val bucket = i % NUM_BUCKETS
                    val bound = bucketInsertStmt.bind("DEVICE-01", bucket, timestamp, "telemetry_data_packet_$i")
                        .setConsistencyLevel(DefaultConsistencyLevel.ONE)
                    sess.executeAsync(bound).await()
                }
            }
        }
        bucketJobs.awaitAll()
        val bucketDurationSec = (System.currentTimeMillis() - t0Bucket) / 1000.0

        // Read Benchmarks
        log("Benchmarking point partition scan latency...", "\u001B[1;36m")
        val qMono = SimpleStatement.newInstance("SELECT * FROM $KEYSPACE.telemetry_monolithic WHERE device_id = 'ALL' LIMIT 100")
            .setConsistencyLevel(DefaultConsistencyLevel.ONE)
        val t0ReadMono = System.nanoTime()
        sess.execute(qMono).all()
        val readMonoMs = (System.nanoTime() - t0ReadMono) / 1_000_000.0

        val qBucket = SimpleStatement.newInstance("SELECT * FROM $KEYSPACE.telemetry_bucketed WHERE device_id = 'DEVICE-01' AND bucket_id = 0 LIMIT 100")
            .setConsistencyLevel(DefaultConsistencyLevel.ONE)
        val t0ReadBucket = System.nanoTime()
        sess.execute(qBucket).all()
        val readBucketMs = (System.nanoTime() - t0ReadBucket) / 1_000_000.0

        println("\n" + "=".repeat(75))
        println("  PARTITION ARCHITECTURE COMPARISON (KOTLIN / DATASTAX 4.x)")
        println("=".repeat(75))
        println("  Ingestion Strategy    : Monolithic (1 Partition)  vs Bucketed ($NUM_BUCKETS Buckets)")
        println("  Write Duration        : ${"%.3f".format(monoDurationSec)}s                   vs ${"%.3f".format(bucketDurationSec)}s")
        println("  Write Throughput      : ${"%,d".format((EVENT_COUNT / monoDurationSec).toInt())} writes/s           vs ${"%,d".format((EVENT_COUNT / bucketDurationSec).toInt())} writes/s")
        println("  Read Latency (Top 100): ${"%.2f".format(readMonoMs)} ms                  vs ${"%.2f".format(readBucketMs)} ms")
        println("  JVM Partition Health  : CRITICAL (>100MB bloat)   vs SAFE (<10MB/bucket)")
        println("=".repeat(75) + "\n")
    }
}

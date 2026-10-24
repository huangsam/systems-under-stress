@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.github.ajalt.mordant:mordant-jvm:2.4.0")
@file:DependsOn("org.mongodb:mongodb-driver-sync:5.1.0")

import com.github.ajalt.mordant.rendering.TextColors.blue
import com.github.ajalt.mordant.rendering.TextColors.cyan
import com.github.ajalt.mordant.rendering.TextColors.green
import com.github.ajalt.mordant.rendering.TextColors.magenta
import com.github.ajalt.mordant.rendering.TextColors.red
import com.github.ajalt.mordant.rendering.TextColors.white
import com.github.ajalt.mordant.rendering.TextColors.yellow
import com.github.ajalt.mordant.rendering.TextStyles.bold
import com.github.ajalt.mordant.rendering.TextStyles.dim
import com.mongodb.MongoWriteException
import com.mongodb.client.MongoClients
import com.mongodb.client.model.Filters
import com.mongodb.client.model.Updates
import org.bson.Document

val MONGO_URI = "mongodb://127.0.0.1:27017/"
val DB_NAME = "lab_db"
val BSON_MAX_BYTES = 16 * 1024 * 1024

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

fun formatBytes(bytes: Long): String {
    var b = bytes.toDouble()
    for (unit in listOf("B", "KB", "MB", "GB")) {
        if (b < 1024.0) return "%.2f %s".format(b, unit)
        b /= 1024.0
    }
    return "%.2f TB".format(b)
}

fun main() {
    println(boldCyan("=".repeat(70)))
    println(boldCyan("Experiment 03 (Kotlin): Unbounded Array Growth & 16MB BSON Limit"))
    println("Demonstrating reallocation latency degradation and the hard 16MB ceiling.")
    println(boldCyan("=".repeat(70)) + "\n")

    val client = try {
        val c = MongoClients.create(MONGO_URI)
        c.getDatabase("admin").runCommand(Document("ping", 1))
        c
    } catch (e: Exception) {
        println(boldRed("Error: Cannot connect to MongoDB on 127.0.0.1:27017: ${e.message}"))
        println("Start the container first: cd mongodb && make up EXP=document-limits")
        return
    }

    val db = client.getDatabase(DB_NAME)
    val unbounded = db.getCollection("unbounded_sensors")
    val bucketed = db.getCollection("bucketed_sensors")
    unbounded.drop()
    bucketed.drop()

    // Part 1: Unbounded array push until 16MB failure
    println(boldBlue("[*] Part 1: Appending 256KB event batches via \$push until 16MB crash..."))
    unbounded.insertOne(Document("_id", "device_001").append("events", emptyList<Document>()))

    val chunkSize = 256 * 1024
    val payload256kb = "x".repeat(chunkSize)
    var totalAppended = 0L

    for (step in 1..80) {
        val t0 = System.currentTimeMillis()
        try {
            unbounded.updateOne(
                Filters.eq("_id", "device_001"),
                Updates.push("events", Document("step", step).append("payload", payload256kb))
            )
            totalAppended += chunkSize
            val elapsed = System.currentTimeMillis() - t0
            if (step % 10 == 0 || step in listOf(1, 5, 20, 40, 60)) {
                println("    Append #%2d | Document Size: ~%-10s | Push Latency: %4d ms".format(
                    step, formatBytes(totalAppended), elapsed
                ))
            }
        } catch (e: Exception) {
            val elapsed = System.currentTimeMillis() - t0
            val msg = e.message ?: ""
            println("\n  ${boldRed("[!] WRITE FAILED AT APPEND #$step (Document Size: ~${formatBytes(totalAppended)})!")}")
            println("      Latency on Crash Attempt : ${elapsed}ms")
            println("      Engine Error Diagnostics : ${msg.lines().firstOrNull() ?: msg}")
            println("\n  ${boldGreen("[✓] Failure Reproduced:")} Hard BSON limit (16,777,216 bytes) enforced.")
            break
        }
    }

    // Part 2: Bucket Pattern Remediation
    println("\n" + boldBlue("[*] Part 2: Bucket Pattern (Fixed capacity per document container)..."))
    val eventsToWrite = 1000
    val bucketSize = 100
    val smallPayload = "x".repeat(500)
    val tStart = System.currentTimeMillis()

    for (i in 0 until eventsToWrite) {
        val bucketId = "device_001_bucket_${i / bucketSize}"
        bucketed.updateOne(
            Filters.eq("_id", bucketId),
            Updates.combine(
                Updates.setOnInsert("device_id", "device_001"),
                Updates.push("events", Document("seq", i).append("data", smallPayload))
            ),
            com.mongodb.client.model.UpdateOptions().upsert(true)
        )
    }
    val bucketElapsed = System.currentTimeMillis() - tStart
    val countBuckets = bucketed.countDocuments()

    println("    Status          : ${boldGreen("SUCCESS")}")
    println("    Events Written  : ${"%,d".format(eventsToWrite)}")
    println("    Buckets Created : $countBuckets (Each bucket capped at $bucketSize events)")
    println("    Total Duration  : ${bucketElapsed}ms (%.2f ms/write)".format(bucketElapsed.toDouble() / eventsToWrite))

    println("\n" + boldGreen("=".repeat(65)))
    println("DATA MODELING TAKEAWAYS")
    println(boldGreen("=".repeat(65)))
    println("1. Growing arrays via \$push forces WiredTiger to repeatedly reallocate and rewrite the entire doc.")
    println("2. The 16MB BSON hard ceiling cannot be increased via configuration.")
    println("3. The Bucket Pattern bounds document sizes, guarantees O(1) write latency, and avoids")
    println("   catastrophic production document overflows.")
    println(boldGreen("=".repeat(65)) + "\n")
}

main()

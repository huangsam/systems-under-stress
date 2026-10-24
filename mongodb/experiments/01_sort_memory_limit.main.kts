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
import com.mongodb.MongoCommandException
import com.mongodb.client.MongoClients
import com.mongodb.client.model.Indexes
import com.mongodb.client.model.Sorts
import org.bson.Document
import java.util.Date

val MONGO_URI = "mongodb://127.0.0.1:27017/"
val DB_NAME = "lab_db"
val COLL_NAME = "orders"
val TARGET_DOCS = 100_000
val PAYLOAD_SIZE = 1_100

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

fun main() {
    println(boldCyan("=".repeat(65)))
    println(boldCyan("Experiment 01 (Kotlin): MongoDB In-Memory Sort Limit (100MB)"))
    println("Investigating PlanExecutor blocking sort ceiling and index mitigation.")
    println(boldCyan("=".repeat(65)))

    val client = try {
        val c = MongoClients.create(MONGO_URI)
        c.getDatabase("admin").runCommand(Document("ping", 1))
        c
    } catch (e: Exception) {
        println(boldRed("[ERROR] Cannot connect to MongoDB on 127.0.0.1:27017: ${e.message}"))
        println("Start the container first: cd mongodb && make up EXP=unindexed-sort")
        return
    }

    val db = client.getDatabase(DB_NAME)
    val coll = db.getCollection(COLL_NAME)

    // Seed dataset
    val currentCount = coll.countDocuments()
    if (currentCount < TARGET_DOCS) {
        println("\n[*] Seeding '${COLL_NAME}' collection with ${"%,d".format(TARGET_DOCS)} documents (~115MB BSON)...")
        coll.drop()
        val dummyPayload = "x".repeat(PAYLOAD_SIZE)
        val batchSize = 5_000
        val t0 = System.currentTimeMillis()

        for (offset in 0 until TARGET_DOCS step batchSize) {
            val docs = ArrayList<Document>(batchSize)
            for (i in 0 until batchSize) {
                val idx = offset + i
                val doc = Document()
                    .append("order_id", idx)
                    .append("order_date", Date(System.currentTimeMillis() - (idx * 60_000L)))
                    .append("amount", (idx % 500) + 9.99)
                    .append("payload", dummyPayload)
                docs.add(doc)
            }
            coll.insertMany(docs)
            print("\r    Inserted: ${"%,d".format(offset + batchSize)} / ${"%,d".format(TARGET_DOCS)} docs")
        }
        val elapsed = (System.currentTimeMillis() - t0) / 1000.0
        println("\n[*] Dataset seeded in %.2fs.".format(elapsed))
    } else {
        println("[*] Collection '${COLL_NAME}' already contains ${"%,d".format(currentCount)} documents.")
    }

    // Part 1: Unindexed blocking sort (100MB limit failure)
    println("\n" + boldBlue("[*] Part 1: Executing unindexed sort over large collection (blocking in RAM)..."))
    println("    Query: find().sort({ order_date: -1 }).limit(10)")

    val t1 = System.currentTimeMillis()
    try {
        val cursor = coll.find()
            .sort(Sorts.descending("order_date"))
            .limit(10)
            .iterator()
        if (cursor.hasNext()) cursor.next()
        println("    Query unexpectedly succeeded without hitting sort memory limit.")
    } catch (e: MongoCommandException) {
        val elapsed = (System.currentTimeMillis() - t1) / 1000.0
        println("\n  ${boldRed("[!] QUERY FAILED WITH ERROR CODE ${e.errorCode} (${e.errorCodeName})!")}")
        println("      Elapsed  : %.2fs".format(elapsed))
        println("      Message  : ${e.errorMessage.trim()}")
        println("\n  ${boldGreen("[✓] Failure Reproduced:")} MongoDB blocked memory exhaustion by enforcing")
        println("      internalQueryMaxBlockingSortMemoryUsageBytes (100MB).")
    }

    // Part 2: External sort with allowDiskUse = true
    println("\n" + boldBlue("[*] Part 2: Opting into external disk sort (allowDiskUse=true)..."))
    val t2 = System.currentTimeMillis()
    try {
        val res = coll.find()
            .sort(Sorts.descending("order_date"))
            .allowDiskUse(true)
            .limit(10)
            .first()
        val elapsed = (System.currentTimeMillis() - t2) / 1000.0
        println("    Status   : ${boldGreen("SUCCESS (Spilled to disk)")}")
        println("    Elapsed  : %.2fs".format(elapsed))
        println("    Sample Doc ID: ${res?.get("order_id")}")
    } catch (e: Exception) {
        println("    Failed: ${e.message}")
    }

    // Part 3: Index creation (IXSCAN)
    println("\n" + boldBlue("[*] Part 3: Creating B-Tree Index on { order_date: -1 }..."))
    val t3 = System.currentTimeMillis()
    coll.createIndex(Indexes.descending("order_date"))
    val idxTime = (System.currentTimeMillis() - t3) / 1000.0
    println("    Index built in %.2fs.".format(idxTime))

    val t4 = System.currentTimeMillis()
    val indexedRes = coll.find()
        .sort(Sorts.descending("order_date"))
        .limit(10)
        .first()
    val execTime = (System.currentTimeMillis() - t4)
    println("    Status   : ${boldGreen("SUCCESS (Streaming via B-Tree IXSCAN)")}")
    println("    Elapsed  : ${execTime}ms (0 RAM allocated to sort)")
    println("    Sample Doc ID: ${indexedRes?.get("order_id")}")

    println("\n" + boldGreen("=".repeat(65)))
    println("SYSTEMS & ARCHITECTURE TAKEAWAYS")
    println(boldGreen("=".repeat(65)))
    println("1. MongoDB restricts in-memory blocking sort stages to 100MB.")
    println("2. Sorting large unindexed datasets without allowDiskUse terminates with code 292.")
    println("3. allowDiskUse trades I/O throughput to avoid failures, but proper B-Tree indexing")
    println("   completely eliminates sorting memory and executes in sub-millisecond time.")
    println(boldGreen("=".repeat(65)) + "\n")
}

main()

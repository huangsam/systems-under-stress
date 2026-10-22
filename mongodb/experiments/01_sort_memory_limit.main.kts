@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.mongodb:mongodb-driver-sync:5.1.0")

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

fun main() {
    println("\u001b[1;36m=================================================================\u001b[0m")
    println("\u001b[1;36mExperiment 01 (Kotlin): MongoDB In-Memory Sort Limit (100MB)\u001b[0m")
    println("Investigating PlanExecutor blocking sort ceiling and index mitigation.")
    println("\u001b[1;36m=================================================================\u001b[0m")

    val client = try {
        val c = MongoClients.create(MONGO_URI)
        c.getDatabase("admin").runCommand(Document("ping", 1))
        c
    } catch (e: Exception) {
        println("\u001b[1;31m[ERROR] Cannot connect to MongoDB on 127.0.0.1:27017: ${e.message}\u001b[0m")
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
    println("\n\u001b[1;34m[*] Part 1: Executing unindexed sort over large collection (blocking in RAM)...\u001b[0m")
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
        println("\n  \u001b[1;31m[!] QUERY FAILED WITH ERROR CODE ${e.errorCode} (${e.errorCodeName})!\u001b[0m")
        println("      Elapsed  : %.2fs".format(elapsed))
        println("      Message  : ${e.errorMessage.trim()}")
        println("\n  \u001b[1;32m[✓] Failure Reproduced:\u001b[0m MongoDB blocked memory exhaustion by enforcing")
        println("      internalQueryMaxBlockingSortMemoryUsageBytes (100MB).")
    }

    // Part 2: External sort with allowDiskUse = true
    println("\n\u001b[1;34m[*] Part 2: Opting into external disk sort (allowDiskUse=true)...\u001b[0m")
    val t2 = System.currentTimeMillis()
    try {
        val res = coll.find()
            .sort(Sorts.descending("order_date"))
            .allowDiskUse(true)
            .limit(10)
            .first()
        val elapsed = (System.currentTimeMillis() - t2) / 1000.0
        println("    Status   : \u001b[1;32mSUCCESS (Spilled to disk)\u001b[0m")
        println("    Elapsed  : %.2fs".format(elapsed))
        println("    Sample Doc ID: ${res?.get("order_id")}")
    } catch (e: Exception) {
        println("    Failed: ${e.message}")
    }

    // Part 3: Index creation (IXSCAN)
    println("\n\u001b[1;34m[*] Part 3: Creating B-Tree Index on { order_date: -1 }...\u001b[0m")
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
    println("    Status   : \u001b[1;32mSUCCESS (Streaming via B-Tree IXSCAN)\u001b[0m")
    println("    Elapsed  : ${execTime}ms (0 RAM allocated to sort)")
    println("    Sample Doc ID: ${indexedRes?.get("order_id")}")

    println("\n\u001b[1;32m=================================================================")
    println("SYSTEMS & ARCHITECTURE TAKEAWAYS")
    println("=================================================================\u001b[0m")
    println("1. MongoDB restricts in-memory blocking sort stages to 100MB.")
    println("2. Sorting large unindexed datasets without allowDiskUse terminates with code 292.")
    println("3. allowDiskUse trades I/O throughput to avoid failures, but proper B-Tree indexing")
    println("   completely eliminates sorting memory and executes in sub-millisecond time.")
    println("=================================================================\n")
}

main()

#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.mongodb:mongodb-driver-sync:5.1.0")

import com.mongodb.client.MongoClients
import com.mongodb.client.model.Accumulators
import com.mongodb.client.model.Aggregates
import com.mongodb.client.model.Filters
import com.mongodb.client.model.Sorts
import com.mongodb.client.model.Updates
import org.bson.Document
import java.util.Date
import kotlin.system.exitProcess

val MONGO_URI = "mongodb://127.0.0.1:27017/"
val DB_NAME = "lab_db"
val COLL_NAME = "crash_course_inventory"

fun main() {
    println("\n\u001B[1;36m" + "=".repeat(70) + "\u001B[0m")
    println("\u001B[1;32mMongoDB Crash Course (Kotlin / Official Driver): Documents & Pipelines\u001B[0m")
    println("\u001B[1;36m" + "=".repeat(70) + "\u001B[0m\n")

    val client =
        try {
            val c = MongoClients.create(MONGO_URI)
            val pingRes = c.getDatabase("admin").runCommand(Document("ping", 1))
            println("\u001B[1;34m[*] 1. Server Connectivity & Info (ping: ${pingRes["ok"]})\u001B[0m")
            c
        } catch (e: Exception) {
            println("\u001B[1;31m[!] Connection failed: ${e.message}\u001B[0m")
            println("Start the lab container first: cd mongodb && make up EXP=unindexed-sort")
            exitProcess(1)
        }

    client.use { mongoClient ->
        val adminDb = mongoClient.getDatabase("admin")
        val buildInfo = adminDb.runCommand(Document("buildInfo", 1))
        println("    MongoDB Version: ${buildInfo["version"]}")

        val db = mongoClient.getDatabase(DB_NAME)
        val coll = db.getCollection(COLL_NAME)
        coll.drop()

        // 2. Document insertion
        println("\n\u001B[1;34m[*] 2. Inserting Rich Documents (Nested BSON & Arrays)\u001B[0m")
        val sampleDocs =
            listOf(
                Document("sku", "LAP-01")
                    .append("name", "Dev Laptop")
                    .append("category", "hardware")
                    .append("pricing", Document("base", 1200.0).append("currency", "USD").append("discount_pct", 10))
                    .append("tags", listOf("portable", "apple", "arm64"))
                    .append("stock", 15)
                    .append("created_at", Date()),
                Document("sku", "MON-02")
                    .append("name", "4K IPS Monitor")
                    .append("category", "hardware")
                    .append("pricing", Document("base", 450.0).append("currency", "USD").append("discount_pct", 0))
                    .append("tags", listOf("display", "4k"))
                    .append("stock", 30)
                    .append("created_at", Date()),
                Document("sku", "SUB-03")
                    .append("name", "Cloud Storage Tier 1")
                    .append("category", "services")
                    .append("pricing", Document("base", 20.0).append("currency", "USD").append("discount_pct", 5))
                    .append("tags", listOf("cloud", "saas", "subscription"))
                    .append("stock", 9999)
                    .append("created_at", Date()),
                Document("sku", "SUB-04")
                    .append("name", "Enterprise Support Plan")
                    .append("category", "services")
                    .append("pricing", Document("base", 500.0).append("currency", "USD").append("discount_pct", 15))
                    .append("tags", listOf("support", "sla", "enterprise"))
                    .append("stock", 50)
                    .append("created_at", Date()),
            )

        coll.insertMany(sampleDocs)
        println("    Inserted ${sampleDocs.size} documents into '$COLL_NAME'.")

        // 3. Query & Update
        println("\n\u001B[1;34m[*] 3. Nested Query & Atomic Field Modification\u001B[0m")
        val found = coll.find(Filters.gte("pricing.base", 1000.0)).first()
        if (found != null) {
            val pricing = found.get("pricing", Document::class.java)
            println("    High-value item found: '${found["name"]}' at $${pricing["base"]}")
        }

        coll.updateOne(
            Filters.eq("sku", "LAP-01"),
            Updates.combine(
                Updates.inc("stock", -1),
                Updates.push("tags", "backorder-risk"),
            ),
        )
        val updated = coll.find(Filters.eq("sku", "LAP-01")).first()
        if (updated != null) {
            println("    Updated 'LAP-01' stock: ${updated["stock"]}, tags: ${updated["tags"]}")
        }

        // 4. Aggregation pipeline
        println("\n\u001B[1;34m[*] 4. Aggregation Pipeline (\$match -> \$group -> \$sort)\u001B[0m")
        val pipeline =
            listOf(
                Aggregates.match(Filters.gt("stock", 0)),
                Aggregates.group(
                    "\$category",
                    Accumulators.sum("item_count", 1),
                    Accumulators.sum("total_stock", "\$stock"),
                    Accumulators.avg("avg_price", "\$pricing.base"),
                ),
                Aggregates.sort(Sorts.descending("total_stock")),
            )

        println("    Aggregated results by category:")
        for (doc in coll.aggregate(pipeline)) {
            val avg = doc.getDouble("avg_price")
            println("      - Category: ${doc["_id"]}: ${doc["item_count"]} items, avg ${"%.2f".format(avg)}, total stock ${doc["total_stock"]}")
        }

        // 5. Diagnostics
        println("\n\u001B[1;34m[*] 5. Diagnostics & Server Status\u001B[0m")
        val serverStatus = db.runCommand(Document("serverStatus", 1))
        val mem = serverStatus.get("mem", Document::class.java)
        val conns = serverStatus.get("connections", Document::class.java)
        println("    Resident Memory: ${mem?.get("resident")} MB, Virtual: ${mem?.get("virtual")} MB")
        println("    Current Connections: ${conns?.get("current")} (Available: ${conns?.get("available")})")

        // Cleanup
        coll.drop()
        println("    Dropped demo collection '$COLL_NAME'.")
    }

    println("\n\u001B[1;32m[✓] MongoDB Crash Course completed successfully!\u001B[0m\n")
}

main()

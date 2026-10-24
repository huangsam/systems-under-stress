#!/usr/bin/env kotlin
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

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

fun main() {
    println("\n" + boldCyan("=".repeat(70)))
    println(boldGreen("MongoDB Crash Course (Kotlin / Official Driver): Documents & Pipelines"))
    println(boldCyan("=".repeat(70)) + "\n")

    val client =
        try {
            val c = MongoClients.create(MONGO_URI)
            val pingRes = c.getDatabase("admin").runCommand(Document("ping", 1))
            println(boldBlue("[*] 1. Server Connectivity & Info (ping: ${pingRes["ok"]})"))
            c
        } catch (e: Exception) {
            println(boldRed("[!] Connection failed: ${e.message}"))
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
        println("\n" + boldBlue("[*] 2. Inserting Rich Documents (Nested BSON & Arrays)"))
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
        println("\n" + boldBlue("[*] 3. Nested Query & Atomic Field Modification"))
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
        println("\n" + boldBlue("[*] 4. Aggregation Pipeline (\$match -> \$group -> \$sort)"))
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
        println("\n" + boldBlue("[*] 5. Diagnostics & Server Status"))
        val serverStatus = db.runCommand(Document("serverStatus", 1))
        val mem = serverStatus.get("mem", Document::class.java)
        val conns = serverStatus.get("connections", Document::class.java)
        println("    Resident Memory: ${mem?.get("resident")} MB, Virtual: ${mem?.get("virtual")} MB")
        println("    Current Connections: ${conns?.get("current")} (Available: ${conns?.get("available")})")

        // Cleanup
        coll.drop()
        println("    Dropped demo collection '$COLL_NAME'.")
    }

    println("\n" + boldGreen("[✓] MongoDB Crash Course completed successfully!") + "\n")
}

main()

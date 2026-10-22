@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.mongodb:mongodb-driver-sync:5.1.0")

import com.mongodb.client.MongoClients
import com.mongodb.client.MongoCollection
import com.mongodb.WriteConcern
import org.bson.Document
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

val MONGO_URI = "mongodb://127.0.0.1:27017/"
val DB_NAME = "lab_db"
val COLL_NAME = "durability_test"

fun runDockerCmd(vararg args: String): Int {
    val proc = ProcessBuilder("docker", "compose", "--profile", "durability", *args)
        .directory(File(".").canonicalFile.parentFile)
        .redirectErrorStream(true)
        .start()
    proc.waitFor()
    return proc.exitValue()
}

fun waitForMongo(): Boolean {
    for (i in 1..20) {
        try {
            val client = MongoClients.create(MONGO_URI)
            client.getDatabase("admin").runCommand(Document("ping", 1))
            client.close()
            return true
        } catch (e: Exception) {
            Thread.sleep(1000)
        }
    }
    return false
}

fun testDurability(modeName: String, writeConcern: WriteConcern) {
    println("\n\u001b[1;34m[*] Testing Durability Mode: $modeName\u001b[0m")
    println("    Write Concern: $writeConcern")

    // Ensure container running
    runDockerCmd("up", "-d", "--wait")
    if (!waitForMongo()) {
        println("    Could not reach MongoDB.")
        return
    }

    val client = MongoClients.create(MONGO_URI)
    val coll = client.getDatabase(DB_NAME).getCollection(COLL_NAME).withWriteConcern(writeConcern)
    coll.drop()

    val running = AtomicBoolean(true)
    val acknowledged = AtomicInteger(0)

    val writerThread = Thread {
        var id = 0
        while (running.get()) {
            try {
                coll.insertOne(Document("_id", id).append("val", "data-$id").append("time", System.currentTimeMillis()))
                acknowledged.incrementAndGet()
                id++
            } catch (e: Exception) {
                // Socket disconnected during crash
                break
            }
        }
    }
    writerThread.start()

    // Let writes accumulate for 150ms then kill container
    Thread.sleep(150)
    println("    Killing mongod with SIGKILL (ungraceful power loss simulation)...")
    runDockerCmd("kill", "-s", "SIGKILL")
    running.set(false)
    writerThread.join()

    val ackCount = acknowledged.get()
    println("    Acknowledged writes before crash: \u001b[1;33m${ackCount}\u001b[0m")

    println("    Restarting container and running WiredTiger journal recovery...")
    runDockerCmd("up", "-d", "--wait")
    waitForMongo()

    val recoveryClient = MongoClients.create(MONGO_URI)
    val recoveredColl = recoveryClient.getDatabase(DB_NAME).getCollection(COLL_NAME)
    val recoveredCount = recoveredColl.countDocuments().toInt()
    recoveryClient.close()

    val lost = ackCount - recoveredCount
    println("    Surviving documents in storage  : \u001b[1;32m${recoveredCount}\u001b[0m")
    if (lost > 0) {
        println("    Acknowledged Writes Lost        : \u001b[1;31m$lost writes LOST!\u001b[0m")
    } else {
        println("    Acknowledged Writes Lost        : \u001b[1;32m0 (Zero data loss, 100% durable)\u001b[0m")
    }
}

fun main() {
    println("\u001b[1;36m=================================================================\u001b[0m")
    println("\u001b[1;36mExperiment 02 (Kotlin): MongoDB Write Concern & Crash Durability\u001b[0m")
    println("Evaluating data loss across ungraceful SIGKILL crashes.")
    println("\u001b[1;36m=================================================================\u001b[0m")

    // Test 1: w=1, j=false (Memory acknowledged)
    testDurability("w=1, j=false (Memory Acknowledged)", WriteConcern.W1.withJournal(false))

    // Test 2: w=1, j=true (Journal committed)
    testDurability("w=1, j=true (Journal Committed / Fsync)", WriteConcern.W1.withJournal(true))

    println("\n\u001b[1;32m=================================================================")
    println("DURABILITY & REPLICATION TAKEAWAYS")
    println("=================================================================\u001b[0m")
    println("1. w=1 with j=false returns before WiredTiger journal buffers are fsynced to disk.")
    println("2. Any uncheckpointed writes waiting in RAM during a sudden OS crash / power loss vanish.")
    println("3. j=true guarantees ACID durability on the primary by blocking until journal fsync.")
    println("4. In replica sets, 'w: \"majority\"' protects against primary failover rollback.")
    println("=================================================================\n")
}

main()

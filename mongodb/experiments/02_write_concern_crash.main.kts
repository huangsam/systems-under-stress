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
import com.mongodb.WriteConcern
import com.mongodb.client.MongoClients
import com.mongodb.client.MongoCollection
import org.bson.Document
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

val MONGO_URI = "mongodb://127.0.0.1:27017/"
val DB_NAME = "lab_db"
val COLL_NAME = "durability_test"

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

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
    println("\n" + boldBlue("[*] Testing Durability Mode: $modeName"))
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
    println("    Acknowledged writes before crash: ${boldYellow("$ackCount")}")

    println("    Restarting container and running WiredTiger journal recovery...")
    runDockerCmd("up", "-d", "--wait")
    waitForMongo()

    val recoveryClient = MongoClients.create(MONGO_URI)
    val recoveredColl = recoveryClient.getDatabase(DB_NAME).getCollection(COLL_NAME)
    val recoveredCount = recoveredColl.countDocuments().toInt()
    recoveryClient.close()

    val lost = ackCount - recoveredCount
    println("    Surviving documents in storage  : ${boldGreen("$recoveredCount")}")
    if (lost > 0) {
        println("    Acknowledged Writes Lost        : ${boldRed("$lost writes LOST!")}")
    } else {
        println("    Acknowledged Writes Lost        : ${boldGreen("0 (Zero data loss, 100% durable)")}")
    }
}

fun main() {
    println(boldCyan("=".repeat(65)))
    println(boldCyan("Experiment 02 (Kotlin): MongoDB Write Concern & Crash Durability"))
    println("Evaluating data loss across ungraceful SIGKILL crashes.")
    println(boldCyan("=".repeat(65)))

    // Test 1: w=1, j=false (Memory acknowledged)
    testDurability("w=1, j=false (Memory Acknowledged)", WriteConcern.W1.withJournal(false))

    // Test 2: w=1, j=true (Journal committed)
    testDurability("w=1, j=true (Journal Committed / Fsync)", WriteConcern.W1.withJournal(true))

    println("\n" + boldGreen("=".repeat(65)))
    println("DURABILITY & REPLICATION TAKEAWAYS")
    println(boldGreen("=".repeat(65)))
    println("1. w=1 with j=false returns before WiredTiger journal buffers are fsynced to disk.")
    println("2. Any uncheckpointed writes waiting in RAM during a sudden OS crash / power loss vanish.")
    println("3. j=true guarantees ACID durability on the primary by blocking until journal fsync.")
    println("4. In replica sets, 'w: \"majority\"' protects against primary failover rollback.")
    println(boldGreen("=".repeat(65)) + "\n")
}

main()

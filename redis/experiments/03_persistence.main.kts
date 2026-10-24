#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.github.ajalt.mordant:mordant-jvm:2.4.0")
@file:DependsOn("redis.clients:jedis:5.1.2")
@file:DependsOn("org.slf4j:slf4j-nop:2.0.13")

import com.github.ajalt.mordant.rendering.TextColors.blue
import com.github.ajalt.mordant.rendering.TextColors.cyan
import com.github.ajalt.mordant.rendering.TextColors.green
import com.github.ajalt.mordant.rendering.TextColors.magenta
import com.github.ajalt.mordant.rendering.TextColors.red
import com.github.ajalt.mordant.rendering.TextColors.white
import com.github.ajalt.mordant.rendering.TextColors.yellow
import com.github.ajalt.mordant.rendering.TextStyles.bold
import com.github.ajalt.mordant.rendering.TextStyles.dim
import redis.clients.jedis.Jedis

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

val HOST = "127.0.0.1"
val PORT = 6379

fun parseInfo(rawInfo: String): Map<String, String> {
    return rawInfo.lines()
        .filter { it.contains(":") }
        .associate {
            val parts = it.split(":", limit = 2)
            parts[0].trim() to parts[1].trim()
        }
}

fun dockerCmd(vararg args: String): Int {
    val proc = ProcessBuilder("docker", *args).redirectErrorStream(true).start()
    return proc.waitFor()
}

var jedis = try {
    val j = Jedis(HOST, PORT)
    j.ping()
    j
} catch (e: Exception) {
    println(boldRed("Error: Cannot connect to Redis on $HOST:$PORT."))
    println("Please start a persistence profile first:")
    println("  make up EXP=persistence-rdb")
    println("  make up EXP=persistence-aof")
    println("  make up EXP=persistence-always")
    System.exit(1)
    throw e
}

jedis.use { client ->
    val persistInfo = parseInfo(client.info("persistence"))
    val aofEnabled = persistInfo["aof_enabled"] == "1"
    val profileName = if (!aofEnabled) "RDB Snapshots Only" else "AOF Enabled"

    println("\n" + boldBlue("=".repeat(70)))
    println(boldGreen("Experiment 03 (Kotlin / Jedis Driver): Redis Crash Durability (SIGKILL)"))
    println("  Active Engine Mode: ${boldYellow("$profileName")}")
    println(boldBlue("=".repeat(70)) + "\n")

    println("Flushing keyspace for a clean test run...")
    client.flushAll()

    val targetWrites = 5000
    println("Firing $targetWrites rapid writes...")
    var ackWrites = 0

    for (i in 1..targetWrites) {
        client.set("seq:$i", "payload_val_$i")
        ackWrites++
        if (i == 2500) {
            println("  [*] Halfway point reached ($i writes acknowledged).")
            println("  ${boldRed("[*] Triggering abrupt container crash via SIGKILL (kill -9)...")}")
            dockerCmd("compose", "kill", "-s", "SIGKILL")
            break
        }
    }
}

println("  [*] Container terminated. Waiting 1s before starting container again...")
Thread.sleep(1000)
dockerCmd("compose", "up", "-d", "--wait")

println("  [*] Reconnecting to Redis...")
var reconnectedClient: Jedis? = null
for (attempt in 1..15) {
    try {
        val j = Jedis(HOST, PORT)
        j.ping()
        reconnectedClient = j
        break
    } catch (e: Exception) {
        Thread.sleep(500)
    }
}

if (reconnectedClient == null) {
    println(boldRed("Failed to reconnect to Redis after restart."))
} else {
    reconnectedClient.use { client ->
        val recoveredKeys = client.dbSize()
        val ackWrites = 2500L
        val lostKeys = maxOf(0L, ackWrites - recoveredKeys)
        val lossPct = if (ackWrites > 0) (lostKeys.toDouble() / ackWrites * 100.0) else 0.0

        println("\n" + boldCyan("Audit Results Post-SIGKILL Recovery:"))
        println("  Client Acknowledged Writes : ${"%,d".format(ackWrites)}")
        println("  Recovered Keys on Restart   : ${"%,d".format(recoveredKeys)}")
        val lostColor = if (lostKeys > 0) boldRed else boldGreen
        println("  Lost Writes                 : ${lostColor("${"%,d".format(lostKeys)} (${"%.1f".format(lossPct)}%)")}")

        println("\n" + boldBlue("=".repeat(70)))
        println("SYSTEMS TAKEAWAYS:")
        println("1. RDB takes periodic snapshots; all writes between snapshots are completely lost on SIGKILL.")
        println("2. AOF (appendfsync everysec) caps data loss to the last 1 second buffer.")
        println("3. AOF (appendfsync always) gives 100% durability at the cost of disk IOPS on every write.")
        println(boldBlue("=".repeat(70)) + "\n")
    }
}

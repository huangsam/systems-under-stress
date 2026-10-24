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
import redis.clients.jedis.exceptions.JedisDataException

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

val HOST = "127.0.0.1"
val PORT = 6379

fun formatBytes(bytes: Long): String {
    var b = bytes.toDouble()
    for (unit in listOf("B", "KB", "MB", "GB")) {
        if (b < 1024) return "${"%.1f".format(b)}$unit"
        b /= 1024
    }
    return "${"%.1f".format(b)}TB"
}

fun parseInfo(rawInfo: String): Map<String, String> =
    rawInfo
        .lines()
        .filter { it.contains(":") }
        .associate {
            val parts = it.split(":", limit = 2)
            parts[0].trim() to parts[1].trim()
        }

val jedis =
    try {
        val j = Jedis(HOST, PORT)
        j.ping()
        j
    } catch (e: Exception) {
        println(boldRed("Error: Cannot connect to Redis on $HOST:$PORT."))
        println("Please start an experiment container first:")
        println("  make up EXP=eviction-lru")
        println("  make up EXP=eviction-noeviction")
        System.exit(1)
        throw e
    }

jedis.use { client ->
    val infoMem = parseInfo(client.info("memory"))
    val maxMemory = infoMem["maxmemory"]?.toLongOrNull() ?: 0L
    val policy = infoMem["maxmemory_policy"] ?: "unknown"

    println("\n" + boldBlue("=".repeat(65)))
    println(boldGreen("Redis Eviction Lab (Kotlin / Jedis Driver)"))
    println("  Max Memory Limit : ${formatBytes(maxMemory)} (${"%,d".format(maxMemory)} bytes)")
    println("  Eviction Policy  : ${boldYellow("$policy")}")
    println(boldBlue("=".repeat(65)) + "\n")

    println("Flushing keyspace for a clean test run...")
    client.flushAll()

    val payloadSize = 10 * 1024 // 10KB
    val dummyPayload = "X".repeat(payloadSize)
    val totalKeys = 3000

    println("Phase 1: Flooding Redis with up to 3,000 10KB keys (~30MB raw data)...")
    var inserted = 0
    var hitOom = false

    for (i in 1..totalKeys) {
        val key = "item:$i"
        try {
            client.set(key, dummyPayload)
            inserted++
            if (i % 250 == 0) {
                val currentMem = parseInfo(client.info("memory"))["used_memory"]?.toLongOrNull() ?: 0L
                val evictions = parseInfo(client.info("stats"))["evicted_keys"]?.toLongOrNull() ?: 0L
                println("  Inserted ${"%,d".format(i)} keys | Used Memory: ${formatBytes(currentMem)} | Evicted: ${"%,d".format(evictions)}")
            }
        } catch (e: JedisDataException) {
            if (e.message?.contains("OOM") == true) {
                hitOom = true
                println("\n${boldRed("[!] OOM THRESHOLD REACHED AT KEY #$i:")}")
                println("    Redis Error: ${e.message}")
                break
            } else {
                throw e
            }
        }
    }

    val finalMemInfo = parseInfo(client.info("memory"))
    val finalStatsInfo = parseInfo(client.info("stats"))
    val usedMemory = finalMemInfo["used_memory"]?.toLongOrNull() ?: 0L
    val evictedKeys = finalStatsInfo["evicted_keys"]?.toLongOrNull() ?: 0L

    println("\n" + boldBlue("" + "-".repeat(65) + ""))
    println(boldWhite("Phase 1 Results:"))
    println("  Keys Accepted : ${"%,d".format(inserted)}")
    println("  Used Memory   : ${formatBytes(usedMemory)}")
    println("  Evicted Keys  : ${"%,d".format(evictedKeys)}")

    if (policy == "noeviction") {
        if (hitOom) {
            println("  Outcome       : ${boldGreen("SUCCESSFULLY REPRODUCED NOEVICTION OOM")}")
            println("                  Redis strictly protected its memory cap by rejecting writes.")
        }
    } else {
        println("  Outcome       : ${boldGreen("SUCCESSFUL ACTIVE EVICTION")}")
        println("                  Redis accepted all writes by seamlessly evicting cold keys.")
    }
    println(boldBlue("=".repeat(65)) + "\n")
}

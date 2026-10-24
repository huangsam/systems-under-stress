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
import redis.clients.jedis.params.SetParams
import kotlin.system.exitProcess

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

val HOST = "127.0.0.1"
val PORT = 6379

fun parseInfo(raw: String): Map<String, String> =
    raw
        .lines()
        .filter { it.contains(":") }
        .associate {
            val parts = it.split(":", limit = 2)
            parts[0].trim() to parts[1].trim()
        }

fun main() {
    println("\n" + boldCyan("=".repeat(70)))
    println(boldGreen("Redis Crash Course (Kotlin / Jedis): Data Structures & Pipelining"))
    println(boldCyan("=".repeat(70)) + "\n")

    val jedis =
        try {
            val j = Jedis(HOST, PORT)
            val pong = j.ping()
            println(boldBlue("[*] 1. Server Connectivity & Info (PING: $pong)"))
            j
        } catch (e: Exception) {
            println(boldRed("[!] Connection failed: ${e.message}"))
            println("Start the lab container first: cd redis && make up EXP=eviction-lru")
            exitProcess(1)
        }

    jedis.use { r ->
        val serverInfo = parseInfo(r.info("server"))
        println("    Redis Version: ${serverInfo["redis_version"]}, Mode: ${serverInfo["redis_mode"]}")

        // 2. Strings & TTL
        println("\n" + boldBlue("[*] 2. Strings & Key Expiration (TTL)"))
        r.set("cc:user:101:name", "Alice")
        r.set("cc:user:101:session", "token-xyz789", SetParams().ex(10))
        println("    SET cc:user:101:name -> '${r.get("cc:user:101:name")}'")
        val ttl = r.ttl("cc:user:101:session")
        println("    SETEX cc:user:101:session -> '${r.get("cc:user:101:session")}' (TTL remaining: ${ttl}s)")

        // 3. Hashes
        println("\n" + boldBlue("[*] 3. Hash Structures (HSET, HGETALL, HINCRBY)"))
        val cartKey = "cc:cart:101"
        r.hset(cartKey, mapOf("item:laptop" to "1", "item:mouse" to "2"))
        r.hincrBy(cartKey, "item:mouse", 1)
        val cartContents = r.hgetAll(cartKey)
        println("    Cart contents for $cartKey:")
        for ((k, v) in cartContents) {
            println("      - $k: $v")
        }

        // 4. Lists & Sets
        println("\n" + boldBlue("[*] 4. Lists & Sets"))
        val queueKey = "cc:jobs:queue"
        r.del(queueKey)
        r.rpush(queueKey, "job_alpha", "job_beta", "job_gamma")
        val jobCount = r.llen(queueKey)
        val jobs = r.lrange(queueKey, 0, -1)
        println("    List $queueKey ($jobCount items): $jobs")

        val tagsKey = "cc:article:42:tags"
        r.del(tagsKey)
        r.sadd(tagsKey, "database", "redis", "in-memory", "redis")
        val members = r.smembers(tagsKey)
        println("    Set $tagsKey (cardinality ${r.scard(tagsKey)}): $members")

        // 5. Pipelining
        println("\n" + boldBlue("[*] 5. Atomic Counters & Pipelining"))
        val counterKey = "cc:metrics:pageviews"
        r.del(counterKey)
        val pipe = r.pipelined()
        repeat(5) {
            pipe.incr(counterKey)
        }
        val syncResults = pipe.syncAndReturnAll()
        println("    Pipelined 5 INCR operations in 1 round-trip: final value = ${syncResults.last()}")

        // 6. Diagnostics
        println("\n" + boldBlue("[*] 6. Memory & Engine Diagnostics"))
        val mem = parseInfo(r.info("memory"))
        val stats = parseInfo(r.info("stats"))
        println("    Used Memory: ${mem["used_memory_human"]} (Peak: ${mem["used_memory_peak_human"]})")
        println("    Total Connections Received: ${stats["total_connections_received"]}")
        println("    Total Commands Processed: ${stats["total_commands_processed"]}")

        // Cleanup
        val keys = r.keys("cc:*")
        if (keys.isNotEmpty()) {
            r.del(*keys.toTypedArray())
        }
        println("    Cleaned up ${keys.size} demo keys matching 'cc:*'.")
    }

    println("\n" + boldGreen("[✓] Redis Crash Course completed successfully!") + "\n")
}

main()

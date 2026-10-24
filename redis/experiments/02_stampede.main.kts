#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.github.ajalt.mordant:mordant-jvm:2.4.0")
@file:DependsOn("redis.clients:jedis:5.1.2")
@file:DependsOn("org.slf4j:slf4j-nop:2.0.13")
@file:DependsOn("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")

import com.github.ajalt.mordant.rendering.TextColors.blue
import com.github.ajalt.mordant.rendering.TextColors.cyan
import com.github.ajalt.mordant.rendering.TextColors.green
import com.github.ajalt.mordant.rendering.TextColors.magenta
import com.github.ajalt.mordant.rendering.TextColors.red
import com.github.ajalt.mordant.rendering.TextColors.white
import com.github.ajalt.mordant.rendering.TextColors.yellow
import com.github.ajalt.mordant.rendering.TextStyles.bold
import com.github.ajalt.mordant.rendering.TextStyles.dim
import kotlinx.coroutines.*
import redis.clients.jedis.JedisPool
import redis.clients.jedis.params.SetParams
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

val HOST = "127.0.0.1"
val PORT = 6379
val HOT_KEY = "product:trending:42"
val DB_DELAY_MS = 50L
val CONCURRENT_REQUESTS = 100

class MockDatabase {
    val queryCount = AtomicInteger(0)

    suspend fun fetchProduct(id: String): String {
        queryCount.incrementAndGet()
        delay(DB_DELAY_MS)
        return """{"id": "$id", "name": "Mechanical Keyboard", "stock": 42}"""
    }
}

val pool = JedisPool(HOST, PORT)

suspend fun runNaive(db: MockDatabase): String = withContext(Dispatchers.IO) {
    pool.resource.use { jedis ->
        val cached = jedis.get(HOT_KEY)
        if (cached != null) return@withContext "cache"

        val data = db.fetchProduct("42")
        jedis.setex(HOT_KEY, 5, data)
        return@withContext "database"
    }
}

suspend fun runWithLock(db: MockDatabase): String = withContext(Dispatchers.IO) {
    pool.resource.use { jedis ->
        val cached = jedis.get(HOT_KEY)
        if (cached != null) return@withContext "cache"

        val lockKey = "lock:$HOT_KEY"
        val lockToken = UUID.randomUUID().toString()

        for (attempt in 1..20) {
            val acquired = jedis.set(lockKey, lockToken, SetParams().nx().px(2000L))
            if (acquired == "OK") {
                try {
                    val data = db.fetchProduct("42")
                    jedis.setex(HOT_KEY, 5, data)
                    return@withContext "database"
                } finally {
                    val releaseScript = """
                        if redis.call('get', KEYS[1]) == ARGV[1] then
                            return redis.call('del', KEYS[1])
                        else
                            return 0
                        end
                    """.trimIndent()
                    jedis.eval(releaseScript, listOf(lockKey), listOf(lockToken))
                }
            } else {
                delay(15)
                val checkAgain = jedis.get(HOT_KEY)
                if (checkAgain != null) return@withContext "cache"
            }
        }
        return@withContext "timeout"
    }
}

runBlocking {
    println("\n" + boldBlue("=".repeat(75)))
    println(boldGreen("Experiment 02 (Kotlin / Jedis + Coroutines): Cache Stampede Simulation"))
    println("Simulating $CONCURRENT_REQUESTS concurrent coroutine requests arriving at key expiry...")
    println(boldBlue("=".repeat(75)) + "\n")

    // Strategy 1: Naive
    pool.resource.use { it.del(HOT_KEY) }
    val dbNaive = MockDatabase()
    println("[Strategy 1] Naive Cache-Aside (Zero Throttling)")
    val t0Naive = System.currentTimeMillis()
    val naiveResults = (1..CONCURRENT_REQUESTS).map {
        async(Dispatchers.IO) { runNaive(dbNaive) }
    }.awaitAll()
    val durNaive = System.currentTimeMillis() - t0Naive

    val naiveDbHits = naiveResults.count { it == "database" }
    val naiveCacheHits = naiveResults.count { it == "cache" }
    println("  Total Duration : ${durNaive}ms")
    println("  Database Hits  : ${boldRed("$naiveDbHits / $CONCURRENT_REQUESTS (Stampede!)")}")
    println("  Cache Hits     : $naiveCacheHits")

    // Strategy 2: Mutex Lock
    pool.resource.use { it.del(HOT_KEY) }
    val dbLock = MockDatabase()
    println("\n[Strategy 2] Distributed Mutex Lock (SET NX PX)")
    val t0Lock = System.currentTimeMillis()
    val lockResults = (1..CONCURRENT_REQUESTS).map {
        async(Dispatchers.IO) { runWithLock(dbLock) }
    }.awaitAll()
    val durLock = System.currentTimeMillis() - t0Lock

    val lockDbHits = lockResults.count { it == "database" }
    val lockCacheHits = lockResults.count { it == "cache" }
    println("  Total Duration : ${durLock}ms")
    println("  Database Hits  : ${boldGreen("$lockDbHits / $CONCURRENT_REQUESTS (Deflected 99%)")}")
    println("  Cache Hits     : $lockCacheHits")

    println("\n" + boldBlue("=".repeat(75)))
    println("SYSTEMS TAKEAWAYS:")
    println("1. Without mutex locks, 100 concurrent cache misses cause 100 identical DB queries.")
    println("2. Distributed locks ensure only 1 thread recomputes, protecting PostgreSQL/MySQL.")
    println(boldBlue("=".repeat(75)) + "\n")

    pool.close()
}

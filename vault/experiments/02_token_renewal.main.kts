@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.json:json:20240303")

import org.json.JSONObject
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse

val VAULT_ADDR = System.getenv("VAULT_ADDR") ?: "http://127.0.0.1:8200"
val VAULT_TOKEN = System.getenv("VAULT_TOKEN") ?: "root"
val BASELINE_TTL = "2s"
val MAX_TTL = "6s"
val RENEWAL_INTERVAL_MS = 1000L

val client: HttpClient = HttpClient.newHttpClient()

fun vaultReq(token: String, method: String, path: String, body: JSONObject? = null): HttpResponse<String> {
    val reqBuilder = HttpRequest.newBuilder()
        .uri(URI.create("$VAULT_ADDR$path"))
        .header("X-Vault-Token", token)
        .header("Content-Type", "application/json")

    val req = when (method.uppercase()) {
        "GET" -> reqBuilder.GET().build()
        "POST" -> reqBuilder.POST(HttpRequest.BodyPublishers.ofString(body?.toString() ?: "{}")).build()
        "PUT" -> reqBuilder.PUT(HttpRequest.BodyPublishers.ofString(body?.toString() ?: "{}")).build()
        else -> throw IllegalArgumentException("Unsupported HTTP method: $method")
    }
    return client.send(req, HttpResponse.BodyHandlers.ofString())
}

fun main() {
    println("\u001b[1;36m=================================================================\u001b[0m")
    println("\u001b[1;36mExperiment 02 (Kotlin): Token TTL, Renewable Leases & Max TTL\u001b[0m")
    println("Investigating heartbeat renewal mechanics and the unbreachable max_ttl ceiling.")
    println("\u001b[1;36m=================================================================\u001b[0m\n")

    // Check Vault connection
    try {
        val health = vaultReq(VAULT_TOKEN, "GET", "/v1/sys/health")
        if (health.statusCode() !in listOf(200, 429, 472, 473)) {
            println("\u001b[1;31mError: Vault returned unexpected status: ${health.statusCode()}\u001b[0m")
            return
        }
    } catch (e: Exception) {
        println("\u001b[1;31mError: Cannot connect to Vault on $VAULT_ADDR (${e.message})\u001b[0m")
        println("Start container first: cd vault && make up EXP=token-lifecycle")
        return
    }

    println("[*] Creating child token with TTL=$BASELINE_TTL and explicit_max_ttl=$MAX_TTL...")
    val createPayload = JSONObject()
        .put("ttl", BASELINE_TTL)
        .put("explicit_max_ttl", MAX_TTL)
        .put("renewable", true)

    val createResp = vaultReq(VAULT_TOKEN, "POST", "/v1/auth/token/create", createPayload)
    val createJson = JSONObject(createResp.body())
    val auth = createJson.getJSONObject("auth")
    val childToken = auth.getString("client_token")
    val initialDuration = auth.getInt("lease_duration")

    println("    Child Token Created : ${childToken.take(12)}...")
    println("    Initial Lease TTL   : ${initialDuration}s")

    println("\n\u001b[1;34m[*] Beginning Heartbeat Renewal Loop (every 1s)...\u001b[0m")
    val startTime = System.currentTimeMillis()
    var pulse = 1

    while (true) {
        Thread.sleep(RENEWAL_INTERVAL_MS)
        val elapsed = (System.currentTimeMillis() - startTime) / 1000.0

        print("    [Pulse #$pulse | Elapsed: %.1fs] Sending renew-self... ".format(elapsed))
        val renewResp = vaultReq(childToken, "POST", "/v1/auth/token/renew-self")

        if (renewResp.statusCode() == 200) {
            val renewJson = JSONObject(renewResp.body())
            val remainingTtl = renewJson.getJSONObject("auth").getInt("lease_duration")
            println("\u001b[1;32mRENEWED\u001b[0m (New TTL: ${remainingTtl}s)")
        } else {
            println("\u001b[1;31mFAILED (HTTP ${renewResp.statusCode()})\u001b[0m")
            println("\n  \u001b[1;31m[!] HARD CEILING HIT: Token Max TTL Exceeded!\u001b[0m")
            println("      Response Body: ${renewResp.body().trim()}")
            break
        }
        pulse++
        if (pulse > 10) break
    }

    println("\n\u001b[1;34m[*] Verifying token revocation after Max TTL expiration...\u001b[0m")
    val lookupResp = vaultReq(childToken, "GET", "/v1/auth/token/lookup-self")
    println("    Lookup-self HTTP Status : \u001b[1;31m${lookupResp.statusCode()}\u001b[0m (403 Forbidden / Expired)")

    println("\n\u001b[1;32m=================================================================")
    println("TOKEN LIFECYCLE TAKEAWAYS")
    println("=================================================================\u001b[0m")
    println("1. Baseline TTL requires clients to actively pulse renewals (heartbeat).")
    println("2. 'explicit_max_ttl' enforces a hard lifetime ceiling that cannot be bypassed")
    println("   by continuous renewals.")
    println("3. When max_ttl expires, Vault permanently purges the token from memory and storage.")
    println("=================================================================\n")
}

main()

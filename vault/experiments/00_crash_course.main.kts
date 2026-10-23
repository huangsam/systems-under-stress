#!/usr/bin/env kotlin
@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.json:json:20240303")

import org.json.JSONObject
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import kotlin.system.exitProcess

val VAULT_ADDR = System.getenv("VAULT_ADDR") ?: "http://127.0.0.1:8200"
val VAULT_TOKEN = System.getenv("VAULT_TOKEN") ?: "root"
val SECRET_PATH = "crash-course-app"

val client: HttpClient = HttpClient.newHttpClient()

fun vaultRequest(
    method: String,
    path: String,
    body: JSONObject? = null,
    token: String = VAULT_TOKEN,
): HttpResponse<String> {
    val reqBuilder =
        HttpRequest
            .newBuilder()
            .uri(URI.create("$VAULT_ADDR$path"))
            .header("X-Vault-Token", token)
            .header("Content-Type", "application/json")

    val req =
        when (method.uppercase()) {
            "GET" -> reqBuilder.GET().build()
            "POST" -> reqBuilder.POST(HttpRequest.BodyPublishers.ofString(body?.toString() ?: "{}")).build()
            "PUT" -> reqBuilder.PUT(HttpRequest.BodyPublishers.ofString(body?.toString() ?: "{}")).build()
            "DELETE" -> reqBuilder.DELETE().build()
            else -> throw IllegalArgumentException("Unsupported HTTP method: $method")
        }
    return client.send(req, HttpResponse.BodyHandlers.ofString())
}

fun main() {
    println("\n\u001B[1;36m" + "=".repeat(70) + "\u001B[0m")
    println("\u001B[1;32mHashiCorp Vault Crash Course (Kotlin / HTTP API): Secrets & Leases\u001B[0m")
    println("\u001B[1;36m" + "=".repeat(70) + "\u001B[0m\n")

    val healthResp =
        try {
            vaultRequest("GET", "/v1/sys/health")
        } catch (e: Exception) {
            println("\u001B[1;31m[!] Connection failed: ${e.message}\u001B[0m")
            println("Start the lab container first: cd vault && make up EXP=dynamic-db")
            exitProcess(1)
        }

    val health = JSONObject(healthResp.body())
    println("\u001B[1;34m[*] 1. Server Health & Seal Status\u001B[0m")
    println("    Vault Version: ${health.optString("version")}, Cluster: ${health.optString("cluster_name")}")
    println("    Initialized: ${health.optBoolean("initialized")}, Sealed: ${health.optBoolean("sealed")}")

    // 2. KV v2 Write Secret
    println("\n\u001B[1;34m[*] 2. Versioned Secrets Engine (KV v2 Write & Read)\u001B[0m")
    val payloadV1 =
        JSONObject().apply {
            put(
                "data",
                JSONObject().apply {
                    put("api_key", "prod_key_alpha_99")
                    put("env", "production")
                    put("max_conns", 50)
                },
            )
        }
    vaultRequest("POST", "/v1/secret/data/$SECRET_PATH", payloadV1)

    val readResp1 = vaultRequest("GET", "/v1/secret/data/$SECRET_PATH")
    val readJson1 = JSONObject(readResp1.body())
    val dataV1 = readJson1.getJSONObject("data").getJSONObject("data")
    val metaV1 = readJson1.getJSONObject("data").getJSONObject("metadata")
    println("    Written Secret v${metaV1.getInt("version")}: $dataV1")

    // 3. Rotate Secret to V2
    println("\n\u001B[1;34m[*] 3. Secret Rotation (Creating Version 2)\u001B[0m")
    val payloadV2 =
        JSONObject().apply {
            put(
                "data",
                JSONObject().apply {
                    put("api_key", "rotated_key_beta_100")
                    put("env", "production")
                    put("max_conns", 100)
                },
            )
        }
    vaultRequest("POST", "/v1/secret/data/$SECRET_PATH", payloadV2)

    val readResp2 = vaultRequest("GET", "/v1/secret/data/$SECRET_PATH")
    val readJson2 = JSONObject(readResp2.body())
    val dataV2 = readJson2.getJSONObject("data").getJSONObject("data")
    val metaV2 = readJson2.getJSONObject("data").getJSONObject("metadata")
    println("    Rotated to Secret v${metaV2.getInt("version")}: $dataV2")

    // Historical v1 read
    val histResp = vaultRequest("GET", "/v1/secret/data/$SECRET_PATH?version=1")
    val histJson = JSONObject(histResp.body())
    val histKey = histJson.getJSONObject("data").getJSONObject("data").getString("api_key")
    println("    Historical query (v1 api_key): '$histKey'")

    // 4. Ephemeral Child Token
    println("\n\u001B[1;34m[*] 4. Ephemeral Child Tokens & Lease Metadata\u001B[0m")
    val tokenPayload =
        JSONObject().apply {
            put("ttl", "10m")
            put("renewable", true)
            put("display_name", "crash-course-worker")
        }
    val createTokenResp = vaultRequest("POST", "/v1/auth/token/create", tokenPayload)
    val tokenAuth = JSONObject(createTokenResp.body()).getJSONObject("auth")
    val childToken = tokenAuth.getString("client_token")
    val leaseDuration = tokenAuth.getInt("lease_duration")
    println("    Created child token: ${childToken.take(15)}... (TTL: ${leaseDuration}s, Renewable: ${tokenAuth.getBoolean("renewable")})")

    // Lookup token self
    val lookupResp = vaultRequest("GET", "/v1/auth/token/lookup-self", token = childToken)
    val lookupData = JSONObject(lookupResp.body()).getJSONObject("data")
    println("    Token accessor: ${lookupData.getString("accessor")}, Policies: ${lookupData.getJSONArray("policies")}")

    // Revoke child token
    val revokePayload = JSONObject().apply { put("token", childToken) }
    vaultRequest("POST", "/v1/auth/token/revoke", revokePayload)
    println("    Revoked child token.")

    // 5. Diagnostics: Mounts
    println("\n\u001B[1;34m[*] 5. Mounted Secrets Engines\u001B[0m")
    val mountsResp = vaultRequest("GET", "/v1/sys/mounts")
    val mounts = JSONObject(mountsResp.body()).getJSONObject("data")
    for (key in mounts.keySet()) {
        val cfg = mounts.getJSONObject(key)
        println("    - Mount: ${key.padEnd(15)} Type: ${cfg.getString("type").padEnd(10)} Desc: ${cfg.optString("description")}")
    }

    // Cleanup
    vaultRequest("DELETE", "/v1/secret/metadata/$SECRET_PATH")
    println("    Cleaned up demo secret '$SECRET_PATH' and all metadata.")

    println("\n\u001B[1;32m[✓] HashiCorp Vault Crash Course completed successfully!\u001B[0m\n")
}

main()

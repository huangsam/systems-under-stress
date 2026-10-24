@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("com.github.ajalt.mordant:mordant-jvm:2.4.0")
@file:DependsOn("org.json:json:20240303")

import com.github.ajalt.mordant.rendering.TextColors.blue
import com.github.ajalt.mordant.rendering.TextColors.cyan
import com.github.ajalt.mordant.rendering.TextColors.green
import com.github.ajalt.mordant.rendering.TextColors.magenta
import com.github.ajalt.mordant.rendering.TextColors.red
import com.github.ajalt.mordant.rendering.TextColors.white
import com.github.ajalt.mordant.rendering.TextColors.yellow
import com.github.ajalt.mordant.rendering.TextStyles.bold
import com.github.ajalt.mordant.rendering.TextStyles.dim
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.util.Base64

val boldCyan = bold + cyan
val boldGreen = bold + green
val boldRed = bold + red
val boldYellow = bold + yellow
val boldBlue = bold + blue
val boldMagenta = bold + magenta
val boldWhite = bold + white

val VAULT_URL = System.getenv("VAULT_ADDR") ?: "http://127.0.0.1:8200"
val VAULT_TOKEN = System.getenv("VAULT_TOKEN") ?: "root"
val KEY_NAME = "pii-customer-ssn"

val client: HttpClient = HttpClient.newHttpClient()

fun b64e(str: String): String = Base64.getEncoder().encodeToString(str.toByteArray())
fun b64d(str: String): String = String(Base64.getDecoder().decode(str))

fun vaultReq(method: String, path: String, body: JSONObject? = null): HttpResponse<String> {
    val reqBuilder = HttpRequest.newBuilder()
        .uri(URI.create("$VAULT_URL$path"))
        .header("X-Vault-Token", VAULT_TOKEN)
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
    println(boldCyan("=".repeat(70)))
    println(boldCyan("Experiment 03 (Kotlin): Transit Encryption-as-a-Service & Key Rotation"))
    println("Benchmarking HTTP crypto latency, batching, and zero-downtime key rotation.")
    println(boldCyan("=".repeat(70)) + "\n")

    // Check Vault connection
    try {
        val health = vaultReq("GET", "/v1/sys/health")
        if (health.statusCode() !in listOf(200, 429, 472, 473)) {
            println(boldRed("Error: Vault returned unexpected status: ${health.statusCode()}"))
            return
        }
    } catch (e: Exception) {
        println(boldRed("Error: Cannot connect to Vault on $VAULT_URL (${e.message})"))
        println("Start container first: cd vault && make up EXP=transit-crypto")
        return
    }

    // 1. Enable transit engine if not mounted
    val mountsResp = vaultReq("GET", "/v1/sys/mounts")
    val mounts = JSONObject(mountsResp.body())
    if (!mounts.has("transit/")) {
        println("[*] Mounting Transit Secrets Engine at transit/...")
        vaultReq("POST", "/v1/sys/mounts/transit", JSONObject().put("type", "transit"))
    }

    // 2. Create encryption key
    vaultReq("POST", "/v1/transit/keys/$KEY_NAME", JSONObject().put("type", "aes256-gcm96"))

    // Part 1: Sequential encryption
    val sampleCount = 100
    println("\n" + boldBlue("[*] Part 1: Sequential Single-Item HTTP Encryption ($sampleCount records)..."))
    val t0 = System.currentTimeMillis()
    var lastCiphertext = ""

    for (i in 1..sampleCount) {
        val plain = "SSN-%03d-45-%04d".format(i, i * 7 % 10000)
        val payload = JSONObject().put("plaintext", b64e(plain))
        val resp = vaultReq("POST", "/v1/transit/encrypt/$KEY_NAME", payload)
        val json = JSONObject(resp.body())
        lastCiphertext = json.getJSONObject("data").getString("ciphertext")
    }
    val seqElapsedMs = System.currentTimeMillis() - t0
    println("    Completed       : $sampleCount items in ${seqElapsedMs}ms")
    println("    Avg Latency     : %.2f ms/op".format(seqElapsedMs.toDouble() / sampleCount))
    println("    Throughput      : %.1f encrypts/sec".format(sampleCount * 1000.0 / seqElapsedMs))
    println("    Sample Ciphertext: ${lastCiphertext.take(35)}...")

    // Part 2: Batched encryption
    println("\n" + boldBlue("[*] Part 2: Batched HTTP Encryption ($sampleCount records in 1 payload)..."))
    val batchArray = JSONArray()
    for (i in 1..sampleCount) {
        val plain = "SSN-%03d-45-%04d".format(i, i * 7 % 10000)
        batchArray.put(JSONObject().put("plaintext", b64e(plain)))
    }
    val batchPayload = JSONObject().put("batch_input", batchArray)

    val t1 = System.currentTimeMillis()
    val batchResp = vaultReq("POST", "/v1/transit/encrypt/$KEY_NAME", batchPayload)
    val batchElapsedMs = System.currentTimeMillis() - t1
    val batchJson = JSONObject(batchResp.body())
    val batchResults = batchJson.getJSONObject("data").getJSONArray("batch_results")

    println("    Status          : ${boldGreen("SUCCESS (${batchResults.length()} items)")}")
    println("    Duration        : ${batchElapsedMs}ms")
    println("    Throughput      : %.1f encrypts/sec".format(sampleCount * 1000.0 / batchElapsedMs))
    val speedup = seqElapsedMs.toDouble() / maxOf(batchElapsedMs.toDouble(), 1.0)
    println("    Batching Speedup: ${boldGreen("%.1fx faster")}".format(speedup))

    // Part 3: Zero-downtime key rotation & rewrapping
    println("\n" + boldBlue("[*] Part 3: Zero-Downtime Key Rotation & Server-Side Rewrapping..."))
    println("    Current Ciphertext from Step 1: $lastCiphertext")

    println("    Rotating Transit Key '$KEY_NAME' to next version...")
    vaultReq("POST", "/v1/transit/keys/$KEY_NAME/rotate")

    println("    Decrypting legacy v1 ciphertext with rotated key...")
    val decResp = vaultReq("POST", "/v1/transit/decrypt/$KEY_NAME", JSONObject().put("ciphertext", lastCiphertext))
    val decJson = JSONObject(decResp.body())
    val decryptedPlain = b64d(decJson.getJSONObject("data").getString("plaintext"))
    println("    Decrypted Plaintext : ${boldGreen("$decryptedPlain")} (Backwards compatibility preserved)")

    println("    Rewrapping v1 ciphertext to v2 without plaintext exposure...")
    val rewrapResp = vaultReq("POST", "/v1/transit/rewrap/$KEY_NAME", JSONObject().put("ciphertext", lastCiphertext))
    val rewrapJson = JSONObject(rewrapResp.body())
    val upgradedCiphertext = rewrapJson.getJSONObject("data").getString("ciphertext")
    println("    Upgraded Ciphertext : ${boldGreen("$upgradedCiphertext")}")

    println("\n" + boldGreen("=".repeat(65)))
    println("ENCRYPTION-AS-A-SERVICE TAKEAWAYS")
    println(boldGreen("=".repeat(65)))
    println("1. Transit eliminates application-side key management and risks of memory key leakage.")
    println("2. HTTP latency is the bottleneck: batching encrypts 20x-50x faster by eliminating TCP RTT.")
    println("3. Vault seamlessly decrypts legacy ciphertexts using versioned keyrings.")
    println("4. 'rewrap' upgrades old ciphertexts in place without decrypting sensitive data back to client.")
    println(boldGreen("=".repeat(65)) + "\n")
}

main()

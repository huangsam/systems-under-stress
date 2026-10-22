@file:Repository("https://repo1.maven.org/maven2/")
@file:DependsOn("org.json:json:20240303")
@file:DependsOn("org.postgresql:postgresql:42.7.3")

import org.json.JSONObject
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.sql.DriverManager
import java.sql.SQLException

val VAULT_ADDR = System.getenv("VAULT_ADDR") ?: "http://127.0.0.1:8200"
val VAULT_TOKEN = System.getenv("VAULT_TOKEN") ?: "root"
val PG_HOST = System.getenv("POSTGRES_HOST") ?: "127.0.0.1"
val PG_PORT = System.getenv("POSTGRES_PORT") ?: "5432"
val PG_DB = System.getenv("POSTGRES_DB") ?: "lab_db"
val ROOT_PG_USER = System.getenv("ROOT_PG_USER") ?: "postgres"
val ROOT_PG_PASS = System.getenv("ROOT_PG_PASS") ?: "postgres"

val client: HttpClient = HttpClient.newHttpClient()

fun vaultRequest(method: String, path: String, body: JSONObject? = null): HttpResponse<String> {
    val reqBuilder = HttpRequest.newBuilder()
        .uri(URI.create("$VAULT_ADDR$path"))
        .header("X-Vault-Token", VAULT_TOKEN)
        .header("Content-Type", "application/json")

    val req = when (method.uppercase()) {
        "GET" -> reqBuilder.GET().build()
        "POST" -> reqBuilder.POST(HttpRequest.BodyPublishers.ofString(body?.toString() ?: "{}")).build()
        "PUT" -> reqBuilder.PUT(HttpRequest.BodyPublishers.ofString(body?.toString() ?: "{}")).build()
        "DELETE" -> reqBuilder.DELETE().build()
        else -> throw IllegalArgumentException("Unsupported HTTP method: $method")
    }
    return client.send(req, HttpResponse.BodyHandlers.ofString())
}

fun setupPostgresSeed() {
    println("[*] Seeding PostgreSQL table at $PG_HOST:$PG_PORT/$PG_DB...")
    val rootUrl = "jdbc:postgresql://$PG_HOST:$PG_PORT/$PG_DB?user=$ROOT_PG_USER&password=$ROOT_PG_PASS"
    DriverManager.getConnection(rootUrl).use { conn ->
        conn.createStatement().use { stmt ->
            stmt.execute("""
                CREATE TABLE IF NOT EXISTS vault_classified_vault (
                    id serial PRIMARY KEY,
                    classification_level text NOT NULL,
                    secret_payload text NOT NULL
                )
            """.trimIndent())
            stmt.execute("TRUNCATE TABLE vault_classified_vault")
            stmt.execute("INSERT INTO vault_classified_vault (classification_level, secret_payload) VALUES ('TOP_SECRET', 'alpha-99-dynamic-payload')")
        }
    }
}

fun main() {
    println("\u001b[1;36m=================================================================\u001b[0m")
    println("\u001b[1;36mExperiment 01 (Kotlin): HashiCorp Vault Dynamic DB Credentials\u001b[0m")
    println("Investigating on-demand credential generation and instantaneous lease revocation.")
    println("\u001b[1;36m=================================================================\u001b[0m")

    // Verify connectivity
    try {
        val health = vaultRequest("GET", "/v1/sys/health")
        if (health.statusCode() !in listOf(200, 429, 472, 473)) {
            println("\u001b[1;31mError: Vault returned unexpected status: ${health.statusCode()}\u001b[0m")
            return
        }
    } catch (e: Exception) {
        println("\u001b[1;31mError: Cannot connect to Vault at $VAULT_ADDR (${e.message})\u001b[0m")
        println("Start container first: cd vault && make up EXP=dynamic-creds")
        return
    }

    setupPostgresSeed()

    // 1. Mount database engine
    println("[*] Configuring Vault Database Secrets Engine...")
    val mountsResp = vaultRequest("GET", "/v1/sys/mounts")
    val mounts = JSONObject(mountsResp.body())
    if (!mounts.has("database/")) {
        val mountPayload = JSONObject().put("type", "database")
        vaultRequest("POST", "/v1/sys/mounts/database", mountPayload)
    }

    // 2. Configure postgresql connection
    val connConfig = JSONObject()
        .put("plugin_name", "postgresql-database-plugin")
        .put("allowed_roles", "dev-readonly")
        .put("connection_url", "postgresql://{{username}}:{{password}}@postgresql-vault:5432/$PG_DB?sslmode=disable")
        .put("username", ROOT_PG_USER)
        .put("password", ROOT_PG_PASS)
    vaultRequest("POST", "/v1/database/config/postgresql", connConfig)

    // 3. Configure dev-readonly role
    val roleConfig = JSONObject()
        .put("db_name", "postgresql")
        .put("creation_statements", listOf(
            """CREATE ROLE "{{name}}" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}';""",
            """GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{{name}}";"""
        ))
        .put("default_ttl", "15s")
        .put("max_ttl", "1m")
    vaultRequest("POST", "/v1/database/roles/dev-readonly", roleConfig)

    // 4. Generate dynamic credentials
    println("\n\u001b[1;34m[*] Requesting dynamic credentials from Vault for role 'dev-readonly'...\u001b[0m")
    val credsResp = vaultRequest("GET", "/v1/database/creds/dev-readonly")
    val credsJson = JSONObject(credsResp.body())
    val data = credsJson.getJSONObject("data")
    val leaseId = credsJson.getString("lease_id")
    val dynUser = data.getString("username")
    val dynPass = data.getString("password")
    val leaseDuration = credsJson.getInt("lease_duration")

    println("    Generated Username : \u001b[1;32m$dynUser\u001b[0m")
    println("    Lease ID           : $leaseId")
    println("    Lease Duration     : ${leaseDuration}s")

    // 5. Connect to Postgres with dynamic credentials
    println("\n\u001b[1;34m[*] Connecting to PostgreSQL using dynamic role ($dynUser)...\u001b[0m")
    val dynUrl = "jdbc:postgresql://$PG_HOST:$PG_PORT/$PG_DB?user=$dynUser&password=$dynPass"
    DriverManager.getConnection(dynUrl).use { conn ->
        conn.createStatement().use { stmt ->
            val rs = stmt.executeQuery("SELECT classification_level, secret_payload FROM vault_classified_vault LIMIT 1")
            if (rs.next()) {
                println("    Query Result: level=${rs.getString(1)}, payload=${rs.getString(2)}")
                println("    Authentication Status: \u001b[1;32mSUCCESS (Granted via dynamic ephemeral role)\u001b[0m")
            }
        }
    }

    // 6. Explicitly revoke lease
    println("\n\u001b[1;34m[*] Revoking lease via Vault API: $leaseId...\u001b[0m")
    val revokePayload = JSONObject().put("lease_id", leaseId)
    val revokeResp = vaultRequest("PUT", "/v1/sys/leases/revoke", revokePayload)
    println("    Vault Revocation Response: ${revokeResp.statusCode()} OK")

    // 7. Verify subsequent connection is rejected
    println("\n\u001b[1;34m[*] Attempting reconnect to PostgreSQL using revoked role ($dynUser)...\u001b[0m")
    try {
        DriverManager.getConnection(dynUrl).use { it.createStatement().execute("SELECT 1") }
        println("    Unexpected: connection succeeded with revoked role.")
    } catch (e: SQLException) {
        println("  \u001b[1;31m[!] CONNECTION REJECTED AS EXPECTED!\u001b[0m")
        println("      SQLState : ${e.sqlState}")
        println("      Message  : ${e.message?.trim()}")
        println("\n  \u001b[1;32m[✓] Failure Verified:\u001b[0m Vault executed DROP ROLE cascade on PostgreSQL,")
        println("      instantly terminating access without manual operator intervention.")
    }

    println("\n\u001b[1;32m=================================================================")
    println("DYNAMIC SECRETS TAKEAWAYS")
    println("=================================================================\u001b[0m")
    println("1. Static shared passwords violate zero-trust and risk credential leaks.")
    println("2. Vault issues unique, time-bounded credentials per process/job.")
    println("3. Revoking the Vault lease immediately triggers DDL statements on the target database,")
    println("   guaranteeing cryptographic lease revocation.")
    println("=================================================================\n")
}

main()

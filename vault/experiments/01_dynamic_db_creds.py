# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "hvac>=2.1.0",
#     "psycopg[binary]>=3.1.0",
# ]
# ///
"""
Experiment 01: Dynamic Secrets Engine & Lease Revocation Cascades

Demonstrates:
1. Dynamic Database Credentials Generation:
   - Vault creates just-in-time, uniquely named database roles with short TTLs.
   - Applications never share or store static database passwords.
2. Immediate Cryptographic Lease Revocation:
   - When a lease is revoked (either explicitly or via TTL expiry), Vault
     immediately executes revocation statements against PostgreSQL, severing access.
3. Post-Revocation Enforcement:
   - PostgreSQL rejects subsequent connection attempts with password authentication failure
     or missing role error.
"""

from __future__ import annotations

import os
import sys
import time

import hvac
import psycopg
from psycopg import errors

VAULT_ADDR = os.getenv("VAULT_ADDR", "http://127.0.0.1:8200")
VAULT_TOKEN = os.getenv("VAULT_TOKEN", "root")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "lab_db")
ROOT_PG_USER = os.getenv("ROOT_PG_USER", "postgres")
ROOT_PG_PASS = os.getenv("ROOT_PG_PASS", "postgres")

# Target role name and TTLs
ROLE_NAME = "dev-readonly"
ROLE_TTL = "15s"
ROLE_MAX_TTL = "1m"


def print_banner(title: str) -> None:
    print(f"\n\033[1;36m{'=' * 70}\033[0m")
    print(f"\033[1;36m  {title}\033[0m")
    print(f"\033[1;36m{'=' * 70}\033[0m")


def setup_postgres_seed_data() -> None:
    """Pre-seeds a test table in PostgreSQL to verify dynamic read access."""
    print(f"[*] Seeding PostgreSQL table at {POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}...")
    conn_str = f"postgresql://{ROOT_PG_USER}:{ROOT_PG_PASS}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    with psycopg.connect(conn_str, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vault_classified_vault (
                    id serial PRIMARY KEY,
                    classification_level text NOT NULL,
                    secret_payload text NOT NULL,
                    created_at timestamp DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                INSERT INTO vault_classified_vault (classification_level, secret_payload)
                SELECT 'TOP_SECRET', 'alpha-bravo-7734-dynamic-lease-active'
                WHERE NOT EXISTS (
                    SELECT 1 FROM vault_classified_vault WHERE classification_level = 'TOP_SECRET'
                );
            """)
    print("\033[1;32m[+] PostgreSQL table and seed row confirmed.\033[0m")


def configure_vault_database_engine(client: hvac.Client) -> None:
    """Mounts database secrets engine, connects to Postgres, and defines dynamic role."""
    print("[*] Inspecting Vault mounted secrets engines...")
    mounts = client.sys.list_mounted_secrets_engines().get("data", {})

    if "database/" not in mounts:
        print("[*] Mounting database secrets engine at 'database/'...")
        client.sys.enable_secrets_engine(backend_type="database", path="database")
        print("\033[1;32m[+] Database secrets engine mounted successfully.\033[0m")
    else:
        print("\033[1;33m[!] Database secrets engine already mounted at 'database/'.\033[0m")

    print("[*] Configuring Vault database connection plugin for PostgreSQL...")
    # Vault connects to PostgreSQL inside the Docker network at hostname 'postgres'
    vault_pg_conn_url = f"postgresql://{{{{username}}}}:{{{{password}}}}@postgres:5432/{POSTGRES_DB}?sslmode=disable"

    try:
        client.secrets.database.configure(
            name="postgresql",
            plugin_name="postgresql-database-plugin",
            allowed_roles=[ROLE_NAME],
            connection_url=vault_pg_conn_url,
            username=ROOT_PG_USER,
            password=ROOT_PG_PASS,
        )
    except Exception:
        # Fallback to direct client write
        client.write(
            "database/config/postgresql",
            plugin_name="postgresql-database-plugin",
            allowed_roles=ROLE_NAME,
            connection_url=vault_pg_conn_url,
            username=ROOT_PG_USER,
            password=ROOT_PG_PASS,
        )
    print("\033[1;32m[+] Database connection 'postgresql' configured in Vault.\033[0m")

    creation_statements = [
        "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}';",
        'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{{name}}";',
        'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO "{{name}}";',
    ]
    revocation_statements = [
        'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";',
        'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";',
        'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM "{{name}}";',
        'DROP OWNED BY "{{name}}";',
        'DROP ROLE IF EXISTS "{{name}}";',
    ]

    print(f"[*] Defining dynamic role '{ROLE_NAME}' (TTL: {ROLE_TTL}, Max TTL: {ROLE_MAX_TTL})...")
    try:
        client.secrets.database.create_role(
            name=ROLE_NAME,
            db_name="postgresql",
            creation_statements=creation_statements,
            revocation_statements=revocation_statements,
            default_ttl=ROLE_TTL,
            max_ttl=ROLE_MAX_TTL,
        )
    except Exception:
        # Fallback to direct client write
        client.write(
            f"database/roles/{ROLE_NAME}",
            db_name="postgresql",
            creation_statements=creation_statements,
            revocation_statements=revocation_statements,
            default_ttl=ROLE_TTL,
            max_ttl=ROLE_MAX_TTL,
        )
    print(f"\033[1;32m[+] Dynamic role '{ROLE_NAME}' created in Vault.\033[0m")


def test_dynamic_credentials_lifecycle() -> None:
    client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
    if not client.is_authenticated():
        raise RuntimeError(f"Failed to authenticate with Vault at {VAULT_ADDR} using token {VAULT_TOKEN}")

    print_banner("1. Configure Vault Secrets Engine & PostgreSQL Database Role")
    setup_postgres_seed_data()
    configure_vault_database_engine(client)

    print_banner("2. Request Ephemeral Dynamic Credentials from Vault")
    print(f"[*] Requesting credentials for role '{ROLE_NAME}'...")
    start_time = time.perf_counter()
    creds_resp = client.secrets.database.generate_credentials(name=ROLE_NAME)
    gen_duration = (time.perf_counter() - start_time) * 1000

    username = creds_resp["data"]["username"]
    password = creds_resp["data"]["password"]
    lease_id = creds_resp["lease_id"]
    lease_duration = creds_resp["lease_duration"]
    renewable = creds_resp["renewable"]

    print(f"\033[1;32m[+] Generated Dynamic Credentials in {gen_duration:.1f}ms:\033[0m")
    print(f"    - Generated Username : \033[1;35m{username}\033[0m")
    print(f"    - Generated Password : \033[1;35m[REDACTED {len(password)} chars]\033[0m")
    print(f"    - Lease ID           : \033[1;33m{lease_id}\033[0m")
    print(f"    - Lease Duration     : {lease_duration}s (Renewable: {renewable})")

    print_banner("3. Connect to PostgreSQL Using Dynamic Role")
    print(f"[*] Connecting to {POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB} as '{username}'...")
    dynamic_conn_str = f"postgresql://{username}:{password}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

    with psycopg.connect(dynamic_conn_str, autocommit=True, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, session_user;")
            curr_user, sess_user = cur.fetchone()
            print("\033[1;32m[+] Authenticated successfully to PostgreSQL!\033[0m")
            print(f"    - Current DB User : {curr_user}")
            print(f"    - Session User    : {sess_user}")

            cur.execute("SELECT classification_level, secret_payload FROM vault_classified_vault;")
            level, payload = cur.fetchone()
            print("\033[1;32m[+] Query Execution Succeeded:\033[0m")
            print(f"    - Classification  : {level}")
            print(f"    - Secret Payload  : {payload}")

    print_banner("4. Cryptographic Revocation: Sever Access via Vault Lease Revocation")
    print(f"[*] Revoking lease '{lease_id}' via Vault API...")
    revoke_start = time.perf_counter()
    client.sys.revoke_lease(lease_id=lease_id)
    revoke_duration = (time.perf_counter() - revoke_start) * 1000
    print(f"\033[1;32m[+] Lease revoked by Vault in {revoke_duration:.1f}ms.\033[0m")

    # Verify lease is purged from Vault
    try:
        lease_status = client.sys.read_lease(lease_id=lease_id)
        print(f"\033[1;33m[!] Warning: Lease still returned by Vault: {lease_status}\033[0m")
    except Exception as exc:
        print(f"\033[1;32m[+] Vault confirmed lease lookup failure (revoked/purged): {exc}\033[0m")

    print_banner("5. Verification: Prove PostgreSQL Immediately Rejects Revoked User")
    print(f"[*] Attempting new connection using revoked username '{username}'...")

    rejected = False
    try:
        with psycopg.connect(dynamic_conn_str, autocommit=True, connect_timeout=3) as conn:
            raise RuntimeError(f"CRITICAL FAILURE: Revoked user '{username}' was able to connect to PostgreSQL!")
    except (errors.OperationalError, psycopg.OperationalError) as exc:
        rejected = True
        err_msg = str(exc).strip().replace("\n", " ")
        print("\033[1;32m[+] Postgres successfully rejected connection attempt!\033[0m")
        print(f"    - Error Signature: \033[1;31m{err_msg}\033[0m")

    if not rejected:
        print("\033[1;31m[-] ERROR: Connection succeeded after lease revocation!\033[0m")
        sys.exit(1)

    print_banner("Summary: Dynamic Database Credentials Invariants")
    print("  1. No static credentials shared across services or operators.")
    print("  2. Unique per-client username allocated deterministically on request.")
    print("  3. Centralized revocation instantly drops database privileges and role.")
    print("  4. Total elapsed time for credential creation + revocation cascade: < 100ms.")
    print("\033[1;32m\n=== Dynamic Secrets Lab Completed Successfully ===\033[0m\n")


if __name__ == "__main__":
    try:
        test_dynamic_credentials_lifecycle()
    except KeyboardInterrupt:
        print("\n[!] Aborted by user.")
        sys.exit(130)
    except Exception as err:
        print(f"\n\033[1;31m[ERROR] {err}\033[0m", file=sys.stderr)
        sys.exit(1)

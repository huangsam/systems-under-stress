# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "hvac>=2.1.0",
# ]
# ///
"""
HashiCorp Vault Crash Course: KV v2 Secrets, Versioning, Token Leases & Diagnostics

Demonstrates:
1. Vault healthcheck, seal status & engine version.
2. KV v2 versioned secrets (writing v1, updating to v2, rollback inspection).
3. Secret metadata and soft delete vs destroy.
4. Child token generation with explicit TTL.
5. System diagnostics (mounts, sys/health).
"""

from __future__ import annotations

import os
import sys

import hvac
from rich.console import Console

VAULT_ADDR = os.getenv("VAULT_ADDR", "http://127.0.0.1:8200")
VAULT_TOKEN = os.getenv("VAULT_TOKEN", "root")
SECRET_PATH = "crash-course-app"


console = Console()


def main() -> None:
    console.print("\n[bold cyan]" + "=" * 70 + "[/]")
    console.print("[bold green]HashiCorp Vault Crash Course: Secrets Engines, Leases & Diagnostics[/]")
    console.print("[bold cyan]" + "=" * 70 + "[/]\n")

    client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)

    try:
        is_init = client.sys.is_initialized()
        is_sealed = client.sys.is_sealed()
        health = client.sys.read_health_status(method="GET").json()
    except Exception as e:
        console.print(f"[bold red][!] Connection failed: {e}[/]")
        print("Start the lab container first: cd vault && make up EXP=dynamic-db")
        sys.exit(1)

    # 1. Health & Server Info
    console.print("[bold blue][*] 1. Server Health & Seal Status[/]")
    print(f"    Vault Version: {health.get('version')}, Cluster: {health.get('cluster_name')}")
    print(f"    Initialized: {is_init}, Sealed: {is_sealed}, Performance Standby: {health.get('performance_standby', False)}")

    # Ensure KV v2 engine is enabled at 'secret'
    mounts = client.sys.list_mounted_secrets_engines().get("data", {})
    if "secret/" not in mounts:
        client.sys.enable_secrets_engine(backend_type="kv", path="secret", options={"version": "2"})

    # 2. KV v2 Secrets & Versioning
    console.print("\n[bold blue][*] 2. Versioned Secrets Engine (KV v2 Write & Read)[/]")
    client.secrets.kv.v2.create_or_update_secret(
        path=SECRET_PATH,
        secret={"api_key": "prod_key_alpha_99", "env": "production", "max_conns": 50},
    )
    read_v1 = client.secrets.kv.v2.read_secret_version(path=SECRET_PATH)
    v1_data = read_v1["data"]["data"]
    v1_meta = read_v1["data"]["metadata"]
    print(f"    Written Secret v{v1_meta['version']}: {v1_data}")

    # Write Version 2
    console.print("\n[bold blue][*] 3. Secret Rotation (Creating Version 2)[/]")
    client.secrets.kv.v2.create_or_update_secret(
        path=SECRET_PATH,
        secret={"api_key": "rotated_key_beta_100", "env": "production", "max_conns": 100},
    )
    read_v2 = client.secrets.kv.v2.read_secret_version(path=SECRET_PATH)
    v2_data = read_v2["data"]["data"]
    v2_meta = read_v2["data"]["metadata"]
    print(f"    Rotated to Secret v{v2_meta['version']}: {v2_data}")

    # Read historical Version 1
    hist_v1 = client.secrets.kv.v2.read_secret_version(path=SECRET_PATH, version=1)
    print(f"    Historical query (v1 api_key): '{hist_v1['data']['data']['api_key']}'")

    # 4. Child Token Creation with TTL
    console.print("\n[bold blue][*] 4. Ephemeral Child Tokens & Lease Metadata[/]")
    new_token = client.auth.token.create(ttl="10m", renewable=True, display_name="crash-course-worker")
    auth_info = new_token["auth"]
    client_token = auth_info["client_token"]
    print(f"    Created child token: {client_token[:15]}... (TTL: {auth_info['lease_duration']}s, Renewable: {auth_info['renewable']})")

    # Lookup token self info
    worker_client = hvac.Client(url=VAULT_ADDR, token=client_token)
    token_meta = worker_client.auth.token.lookup_self()["data"]
    print(f"    Token accessor: {token_meta.get('accessor')}, Policies: {token_meta.get('policies')}")

    # Revoke child token
    client.auth.token.revoke(token=client_token)
    print("    Revoked child token.")

    # 5. Diagnostics
    console.print("\n[bold blue][*] 5. Mounted Secrets Engines & Auth Methods[/]")
    for path, cfg in mounts.items():
        print(f"    - Mount: {path:<15} Type: {cfg.get('type'):<10} Desc: {cfg.get('description', '')}")

    # Cleanup
    client.secrets.kv.v2.delete_metadata_and_all_versions(path=SECRET_PATH)
    print(f"    Cleaned up demo secret '{SECRET_PATH}' and all metadata.")

    console.print("\n[bold green][✓] HashiCorp Vault Crash Course completed successfully![/]\n")


if __name__ == "__main__":
    main()

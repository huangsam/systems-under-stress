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
from rich.markup import escape

VAULT_ADDR = os.getenv("VAULT_ADDR", "http://127.0.0.1:8200")
VAULT_TOKEN = os.getenv("VAULT_TOKEN", "root")
SECRET_PATH = "crash-course-app"


console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def main() -> None:
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("[bold green]HashiCorp Vault Crash Course: Secrets Engines, Leases & Diagnostics[/]")
    console.print("=" * 70 + "\n", style="bold cyan")

    client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)

    try:
        is_init = client.sys.is_initialized()
        is_sealed = client.sys.is_sealed()
        health_res = client.sys.read_health_status(method="GET")
        health = health_res.json() if hasattr(health_res, "json") else health_res
    except Exception as e:
        console.print(f"[bold red][!] Connection failed: {escape(str(e))}[/]")
        plain_console.print("Start the lab container first: cd vault && make up EXP=dynamic-creds")
        sys.exit(1)

    # 1. Health & Server Info
    console.print("[bold blue][*] 1. Server Health & Seal Status[/]")
    verify_phase(is_init and not is_sealed, "Phase 1 Health", f"expected initialized & unsealed Vault, got init={is_init}, sealed={is_sealed}")
    plain_console.print(f"    Vault Version: {health.get('version')}, Cluster: {health.get('cluster_name')}")
    plain_console.print(f"    Initialized: {is_init}, Sealed: {is_sealed}, Performance Standby: {health.get('performance_standby', False)}")

    # Ensure KV v2 engine is enabled at 'secret'
    mounts = client.sys.list_mounted_secrets_engines().get("data", {})
    if "secret/" not in mounts:
        client.sys.enable_secrets_engine(backend_type="kv", path="secret", options={"version": "2"})
        mounts = client.sys.list_mounted_secrets_engines().get("data", {})

    # 2. KV v2 Secrets & Versioning
    console.print("\n[bold blue][*] 2. Versioned Secrets Engine (KV v2 Write & Read)[/]")
    client.secrets.kv.v2.create_or_update_secret(
        path=SECRET_PATH,
        secret={"api_key": "prod_key_alpha_99", "env": "production", "max_conns": 50},
    )
    read_v1 = client.secrets.kv.v2.read_secret_version(path=SECRET_PATH)
    v1_data = read_v1["data"]["data"]
    v1_meta = read_v1["data"]["metadata"]
    verify_phase(
        v1_meta["version"] >= 1 and v1_data.get("api_key") == "prod_key_alpha_99", "Phase 2 KV v1", f"unexpected v1 payload/meta: {v1_meta}, {v1_data}"
    )
    plain_console.print(f"    Written Secret v{v1_meta['version']}: {v1_data}")

    # Write Version 2
    console.print("\n[bold blue][*] 3. Secret Rotation (Creating Version 2)[/]")
    client.secrets.kv.v2.create_or_update_secret(
        path=SECRET_PATH,
        secret={"api_key": "rotated_key_beta_100", "env": "production", "max_conns": 100},
    )
    read_v2 = client.secrets.kv.v2.read_secret_version(path=SECRET_PATH)
    v2_data = read_v2["data"]["data"]
    v2_meta = read_v2["data"]["metadata"]
    plain_console.print(f"    Rotated to Secret v{v2_meta['version']}: {v2_data}")

    # Read historical Version 1
    hist_v1 = client.secrets.kv.v2.read_secret_version(path=SECRET_PATH, version=v1_meta["version"])
    verify_phase(
        v2_meta["version"] == v1_meta["version"] + 1
        and v2_data.get("api_key") == "rotated_key_beta_100"
        and hist_v1["data"]["data"].get("api_key") == "prod_key_alpha_99",
        "Phase 3 Secret Rotation",
        f"unexpected v2/historical state: v1={v1_meta['version']}, v2={v2_meta['version']}, hist={hist_v1['data']['data']}",
    )
    plain_console.print(f"    Historical query (v{v1_meta['version']} api_key): '{hist_v1['data']['data']['api_key']}'")

    # 4. Child Token Creation with TTL
    console.print("\n[bold blue][*] 4. Ephemeral Child Tokens & Lease Metadata[/]")
    new_token = client.auth.token.create(ttl="10m", renewable=True, display_name="crash-course-worker")
    auth_info = new_token["auth"]
    client_token = auth_info["client_token"]
    plain_console.print(f"    Created child token: {client_token[:15]}... (TTL: {auth_info['lease_duration']}s, Renewable: {auth_info['renewable']})")

    # Lookup token self info
    worker_client = hvac.Client(url=VAULT_ADDR, token=client_token)
    token_meta = worker_client.auth.token.lookup_self()["data"]
    verify_phase(bool(client_token) and token_meta.get("accessor") is not None, "Phase 4 Child Token", "child token self-lookup missing accessor")
    plain_console.print(f"    Token accessor: {token_meta.get('accessor')}, Policies: {token_meta.get('policies')}")

    # Revoke child token
    client.auth.token.revoke(token=client_token)
    token_revoked = False
    try:
        worker_client.auth.token.lookup_self()
    except Exception:
        token_revoked = True
    verify_phase(token_revoked, "Phase 4 Revoke", "revoked child token still accepted on lookup_self()")
    plain_console.print("    Revoked child token.")

    # 5. Diagnostics
    console.print("\n[bold blue][*] 5. Mounted Secrets Engines & Auth Methods[/]")
    verify_phase("secret/" in mounts, "Phase 5 Mounts", f"expected 'secret/' in mounts, got {list(mounts.keys())}")
    for path, cfg in mounts.items():
        plain_console.print(f"    - Mount: {path:<15} Type: {cfg.get('type'):<10} Desc: {cfg.get('description', '')}")

    # Cleanup
    client.secrets.kv.v2.delete_metadata_and_all_versions(path=SECRET_PATH)
    secret_deleted = False
    try:
        client.secrets.kv.v2.read_secret_version(path=SECRET_PATH)
    except Exception:
        secret_deleted = True
    verify_phase(secret_deleted, "Phase 5 Cleanup", f"expected '{SECRET_PATH}' to be deleted after cleanup")
    plain_console.print(f"    Cleaned up demo secret '{SECRET_PATH}' and all metadata.")

    console.print("\n[bold green][✓] HashiCorp Vault Crash Course completed successfully![/]\n")


if __name__ == "__main__":
    main()

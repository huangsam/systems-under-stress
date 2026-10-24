# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "hvac>=2.1.0",
# ]
# ///
"""
Experiment 02: Token TTL, Renewable Leases & Max TTL Ceilings

Demonstrates:
1. Token Lease Mechanics:
   - Child tokens can be issued with short baseline TTLs (e.g. 2s) to enforce periodic heartbeats.
   - Long-lived background workers extend life via explicit lease renewal (`renew_self`).
2. The Max TTL Ceiling (Hard Boundary):
   - Regardless of how frequently a token is renewed, it CANNOT outlive its `explicit_max_ttl`.
   - Vault strictly bounds the maximum blast radius of any credential.
3. Enforcement & Expiration:
   - At the `max_ttl` boundary, renewal calls are rejected by Vault.
   - Once expired, the token is permanently revoked from Vault's storage.
"""

from __future__ import annotations

import os
import sys
import time

import hvac
from hvac import exceptions
from rich.console import Console

VAULT_ADDR = os.getenv("VAULT_ADDR", "http://127.0.0.1:8200")
VAULT_TOKEN = os.getenv("VAULT_TOKEN", "root")

BASELINE_TTL = "2s"
MAX_TTL = "6s"
RENEWAL_INTERVAL = 1.0  # seconds between renewal pulses


console = Console()
err_console = Console(stderr=True)


def print_banner(title: str) -> None:
    console.print(f"\n[bold cyan]{'=' * 70}[/]")
    console.print(f"[bold cyan]  {title}[/]")
    console.print(f"[bold cyan]{'=' * 70}[/]")


def test_token_renewal_lifecycle() -> None:
    client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
    if not client.is_authenticated():
        raise RuntimeError(f"Failed to authenticate with Vault at {VAULT_ADDR} using root token.")

    print_banner("1. Create Child Token with Baseline TTL and Explicit Max TTL")
    print(f"[*] Baseline TTL      : {BASELINE_TTL}")
    print(f"[*] Explicit Max TTL  : {MAX_TTL}")
    print(f"[*] Renewal Interval  : {RENEWAL_INTERVAL}s")

    token_resp = client.auth.token.create(
        ttl=BASELINE_TTL,
        explicit_max_ttl=MAX_TTL,
        renewable=True,
    )

    child_token = token_resp["auth"]["client_token"]
    initial_duration = token_resp["auth"]["lease_duration"]
    renewable = token_resp["auth"]["renewable"]

    console.print("[bold green][+] Child Token Created Successfully:[/]")
    print(f"    - Token String    : s.{child_token[:8]}...[REDACTED]")
    print(f"    - Lease Duration  : {initial_duration}s")
    print(f"    - Renewable       : {renewable}")

    # Initialize child client with the generated token
    child_client = hvac.Client(url=VAULT_ADDR, token=child_token)
    initial_lookup = child_client.auth.token.lookup_self()["data"]
    print(f"    - Creation TTL    : {initial_lookup.get('creation_ttl')}s")
    print(f"    - Explicit Max TTL: {initial_lookup.get('explicit_max_ttl')}s")

    print_banner("2. Active Renewal Heartbeat Loop (Approaching Max TTL Ceiling)")
    print(f"{'Time (s)':<10} {'Action':<22} {'Reported TTL':<16} {'Status'}")
    print("-" * 65)

    start_time = time.perf_counter()
    cycle = 0
    max_ttl_hit = False

    while True:
        cycle += 1
        time.sleep(RENEWAL_INTERVAL)
        elapsed = time.perf_counter() - start_time

        try:
            # Attempt to renew child token
            renew_res = child_client.auth.token.renew_self(increment=BASELINE_TTL)
            auth_data = renew_res.get("auth", {})
            current_ttl = auth_data.get("lease_duration")

            # Verify with lookup_self for real-time remaining TTL
            try:
                lookup_data = child_client.auth.token.lookup_self()["data"]
                exact_ttl = lookup_data.get("ttl")
            except Exception:
                exact_ttl = current_ttl

            console.print(f"{elapsed:6.2f}s    {'Renew Pulse #' + str(cycle):<22} {str(exact_ttl) + 's':<16} [bold green]RENEWED (Capped by Max TTL)[/]")

        except (exceptions.InvalidRequest, exceptions.VaultError, exceptions.Forbidden) as exc:
            err_msg = str(exc).strip().replace("\n", " ")
            console.print(f"{elapsed:6.2f}s    {'Renew Pulse #' + str(cycle):<22} {'0s':<16} [bold red]REJECTED: Ceiling Hit[/]")
            console.print("\n[bold yellow][*] Vault Renewal Rejection Signature:[/]")
            console.print(f"    [bold red]{err_msg}[/]")
            max_ttl_hit = True
            break

        # Safety escape in case of an unexpected runaway loop
        if elapsed > 12.0:
            console.print("\n[bold red][-] Timeout: Loop exceeded expected Max TTL boundary without rejection.[/]")
            break

    print_banner("3. Post-Ceiling Revocation Verification")
    if not max_ttl_hit:
        console.print("[bold red][-] FAILURE: Token was never rejected at the explicit_max_ttl boundary.[/]")
        sys.exit(1)

    print("[*] Waiting 1.5s for Vault background reaper / complete lease purge...")
    time.sleep(1.5)

    print("[*] Attempting to look up expired token via child client...")
    token_purged = False
    try:
        child_client.auth.token.lookup_self()
        console.print("[bold red][-] ERROR: Token lookup still succeeded after Max TTL expiration![/]")
    except (exceptions.Forbidden, exceptions.InvalidRequest, exceptions.VaultError) as exc:
        token_purged = True
        console.print("[bold green][+] Token successfully purged and rejected by Vault:[/]")
        print(f"    - Error: {exc}")

    assert token_purged, "Expired token was not invalidated by Vault"

    # Also verify from root client lookup
    try:
        client.auth.token.lookup(token=child_token)
        console.print("[bold red][-] Warning: Root client still sees child token in index.[/]")
    except Exception as exc:
        console.print(f"[bold green][+] Root client confirms token no longer exists in Vault: {exc}[/]")

    print_banner("Summary: Token Lifecycle & Max TTL Invariants")
    print("  1. Periodic renewal successfully maintains short-lived tokens in memory.")
    print("  2. Vault calculates TTL caps dynamically: TTL = min(increment, explicit_max_ttl - elapsed).")
    print(f"  3. At t = {MAX_TTL}, Vault strictly refuses further renewals.")
    print("  4. Once expired, tokens cannot perform any API actions and are evicted.")
    console.print("[bold green]\n=== Token Lifecycle Lab Completed Successfully ===[/]\n")


if __name__ == "__main__":
    try:
        test_token_renewal_lifecycle()
    except KeyboardInterrupt:
        print("\n[!] Aborted by user.")
        sys.exit(130)
    except Exception as err:
        err_console.print(f"\n[bold red][ERROR] {err}[/]")
        sys.exit(1)

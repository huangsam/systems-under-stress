# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "hvac>=2.1.0",
# ]
# ///
"""
Experiment 03: Vault Transit Encryption-as-a-Service (EaaS) Throughput & Key Rotation

Demonstrates:
1. Cryptographic offloading via Vault's Transit Secrets Engine (AES256-GCM96).
   The application encrypts/decrypts sensitive PII without ever holding raw keys in memory.
2. Network Roundtrip Bottleneck vs Batching:
   - Sequential single-item HTTP encryption vs Vault's 'batch_input' bulk API.
3. Zero-Downtime Key Rotation & Ciphertext Rewrapping:
   - Rotating the keyring from v1 -> v2 while v1 ciphertexts remain decryptable.
   - Upgrading legacy `vault:v1:...` ciphertexts to `vault:v2:...` via `rewrap_data`
     on the server without ever decrypting plaintext back to the application!
"""

from __future__ import annotations

import base64
import sys
import time

import hvac

VAULT_URL = "http://127.0.0.1:8200"
VAULT_TOKEN = "root"
KEY_NAME = "pii-customer-ssn"


def b64e(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("utf-8")


def b64d(s: str) -> str:
    return base64.b64decode(s.encode("utf-8")).decode("utf-8")


def main() -> None:
    client = hvac.Client(url=VAULT_URL, token=VAULT_TOKEN)
    try:
        if not client.sys.is_initialized():
            raise RuntimeError("Vault not initialized")
    except Exception as e:
        print(f"\033[1;31mError: Cannot connect to Vault on {VAULT_URL} ({e})\033[0m")
        print("Start the container first: make up EXP=transit-crypto")
        sys.exit(1)

    print("\033[1;36m======================================================================\033[0m")
    print("\033[1;36mExperiment 03: Vault Transit Encryption-as-a-Service & Key Rotation\033[0m")
    print("Benchmarking HTTP crypto overhead, batching, and zero-downtime key rotation.")
    print("\033[1;36m======================================================================\033[0m\n")

    # 1. Enable Transit secrets engine
    mounts = client.sys.list_mounted_secrets_engines()["data"]
    if "transit/" not in mounts:
        client.sys.enable_secrets_engine(backend_type="transit", path="transit")

    # Delete key if left over from previous soft run
    try:
        client.secrets.transit.update_key_configuration(name=KEY_NAME, deletion_allowed=True)
        client.secrets.transit.delete_key(name=KEY_NAME)
    except Exception:
        pass

    client.secrets.transit.create_key(name=KEY_NAME, key_type="aes256-gcm96")
    print(f"[*] Created AES256-GCM96 transit key '{KEY_NAME}' (Version 1).")

    # 2. Part 1: Sequential HTTP Encryption vs Batched Encryption (200 records)
    NUM_RECORDS = 200
    payloads = [b64e(f"SSN-999-00-{i:04d}") for i in range(NUM_RECORDS)]

    print(f"\n\033[1;34m[*] Part 1: Encrypting {NUM_RECORDS} PII records (Sequential vs Batched)...\033[0m")

    t0 = time.perf_counter()
    seq_ciphertexts: list[str] = []
    for p in payloads:
        resp = client.secrets.transit.encrypt_data(name=KEY_NAME, plaintext=p)
        seq_ciphertexts.append(resp["data"]["ciphertext"])
    seq_ms = (time.perf_counter() - t0) * 1000
    seq_ops = NUM_RECORDS / (seq_ms / 1000)

    t1 = time.perf_counter()
    batch_input = [{"plaintext": p} for p in payloads]
    batch_resp = client.secrets.transit.encrypt_data(name=KEY_NAME, batch_input=batch_input)
    batch_ciphertexts = [item["ciphertext"] for item in batch_resp["data"]["batch_results"]]
    batch_ms = (time.perf_counter() - t1) * 1000
    batch_ops = NUM_RECORDS / (batch_ms / 1000)

    speedup = seq_ms / batch_ms if batch_ms > 0 else 1.0
    print(f"    Sequential HTTP (200 calls) : {seq_ms:>7.1f}ms ({seq_ops:>7,.0f} records/sec)")
    print(f"    Batched API (1 HTTP call)   : {batch_ms:>7.1f}ms ({batch_ops:>7,.0f} records/sec)")
    print(f"    Batch Throughput Gain       : \033[1;32m{speedup:.1f}x Faster\033[0m")

    # 3. Part 2: Zero-Downtime Key Rotation & Server-Side Rewrap
    print("\n\033[1;34m[*] Part 2: Zero-Downtime Key Rotation (v1 -> v2) & Rewrapping...\033[0m")
    sample_v1 = batch_ciphertexts[0]
    print(f"    Original v1 Ciphertext : \033[1;33m{sample_v1[:45]}...\033[0m")

    # Rotate key to Version 2
    client.secrets.transit.rotate_key(name=KEY_NAME)
    key_info = client.secrets.transit.read_key(name=KEY_NAME)["data"]
    print(f"    Rotated '{KEY_NAME}' -> Latest Version: v{key_info['latest_version']}")

    # Rewrap v1 ciphertext to v2 without exposing plaintext
    rewrap_resp = client.secrets.transit.rewrap_data(name=KEY_NAME, ciphertext=sample_v1)
    sample_v2 = rewrap_resp["data"]["ciphertext"]
    print(f"    Rewrapped v2 Ciphertext: \033[1;32m{sample_v2[:45]}...\033[0m")

    # Enforce minimum decryption version = 2 (deprecating v1 keys after migration)
    client.secrets.transit.update_key_configuration(name=KEY_NAME, min_decryption_version=2)
    print("    Enforced policy: min_decryption_version = 2 (v1 ciphertexts now blocked).")

    # Verify v1 is rejected while v2 succeeds
    try:
        client.secrets.transit.decrypt_data(name=KEY_NAME, ciphertext=sample_v1)
        v1_blocked = False
    except Exception as err:
        v1_blocked = True
        print(f"    [✓] Legacy v1 decrypt blocked : {err}")

    dec_v2 = client.secrets.transit.decrypt_data(name=KEY_NAME, ciphertext=sample_v2)
    plaintext_recovered = b64d(dec_v2["data"]["plaintext"])
    print(f"    [✓] Upgraded v2 decrypt valid : '{plaintext_recovered}'")

    assert v1_blocked and plaintext_recovered == "SSN-999-00-0000"

    print("\n\033[1;32m======================================================================")
    print("SYSTEMS TAKEAWAYS")
    print("======================================================================\033[0m")
    print("1. Transit EaaS is CPU-fast (AES-NI) but HTTP-latency bound; always use")
    print(f"   'batch_input' ({speedup:.1f}x speedup) or Envelope Encryption (data keys) for bulk data.")
    print("2. Version-prefixed ciphertexts ('vault:v1:...', 'vault:v2:...') allow instant")
    print("   key rotation without breaking reads of existing database rows.")
    print("3. 'rewrap' upgrades old ciphertexts to the newest key inside Vault without")
    print("   ever transmitting plaintext over the network.")
    print("======================================================================\n")


if __name__ == "__main__":
    main()

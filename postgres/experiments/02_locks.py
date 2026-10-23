# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "psycopg[binary]>=3.1.0",
# ]
# ///
"""
Experiment 02: Lock Contention, Hot-Row Queues & Deadlock Detection in PostgreSQL

Demonstrates:
1. Hot Row Contention: 50 concurrent threads updating the same row.
   Row-level exclusive locks (XLock) serialize access, preserving balance integrity.
2. Controlled Deadlock Generation: Two transactions acquiring locks in opposite orders.
   PostgreSQL's background deadlock detector fires (deadlock_timeout=100ms)
   and terminates one transaction with SQLSTATE 40P01 (deadlock_detected).
3. The Global Lock Ordering Fix: Acquiring locks in consistent primary key order
   eliminates circular wait cycles completely under heavy bidirectional concurrency.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time

import psycopg
from psycopg import errors

DB_CONN = "postgresql://postgres:postgres@127.0.0.1:5432/lab_db"


def reset_accounts() -> None:
    with psycopg.connect(DB_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE accounts RESTART IDENTITY;")
            cur.execute("INSERT INTO accounts (owner, balance) VALUES ('Alice', 1000.00), ('Bob', 1000.00);")


def get_balance(account_id: int) -> float:
    with psycopg.connect(DB_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM accounts WHERE id = %s;", (account_id,))
            return float(cur.fetchone()[0])


# ----------------------------------------------------------------------
# Part 1: Hot Row Contention & Row Lock Queuing
# ----------------------------------------------------------------------
def test_hot_row_contention() -> None:
    print("\n\033[1;34m[*] Part 1: 50 Concurrent Updates to Single Hot Row (Alice, id=1)...\033[0m")
    reset_accounts()

    NUM_WORKERS = 50
    start_time = time.perf_counter()

    def increment_balance() -> None:
        with psycopg.connect(DB_CONN) as conn:
            with conn.cursor() as cur:
                # In READ COMMITTED, each UPDATE waits for preceding XLock and re-evaluates
                cur.execute("UPDATE accounts SET balance = balance + 1.00 WHERE id = 1;")
                conn.commit()

    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = [executor.submit(increment_balance) for _ in range(NUM_WORKERS)]
        concurrent.futures.wait(futures)

    duration = time.perf_counter() - start_time
    final_balance = get_balance(1)

    print("    Initial Balance  : $1,000.00")
    print("    Expected Balance : $1,050.00")
    print(f"    Final Balance    : \033[1;32m${final_balance:,.2f}\033[0m")
    print(f"    Total Time       : {duration * 1000:.1f}ms ({NUM_WORKERS} updates serialized)")

    if final_balance == 1050.00:
        print("  [✓] PostgreSQL row-level locks successfully queued all 50 concurrent updates.")
        print("      Zero lost updates occurred.")


# ----------------------------------------------------------------------
# Part 2: Controlled Deadlock Generation (Opposite Lock Ordering)
# ----------------------------------------------------------------------
def test_deadlock() -> None:
    print("\n\033[1;34m[*] Part 2: Triggering Controlled Deadlock (Opposite Lock Ordering)...\033[0m")
    reset_accounts()

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def tx_alice_to_bob() -> None:
        """Locks Alice (1), then Bob (2)."""
        try:
            with psycopg.connect(DB_CONN) as conn:
                with conn.cursor() as cur:
                    # Lock Account 1 (Alice)
                    cur.execute("SELECT id, balance FROM accounts WHERE id = 1 FOR UPDATE;")
                    barrier.wait()  # Wait for Tx 2 to lock Account 2

                    time.sleep(0.05)
                    # Now attempt to lock Account 2 (Bob) - will block waiting for Tx 2
                    cur.execute("SELECT id, balance FROM accounts WHERE id = 2 FOR UPDATE;")
                    conn.commit()
                    results["Tx1 (Alice->Bob)"] = "COMMITTED"
        except errors.DeadlockDetected as err:
            results["Tx1 (Alice->Bob)"] = f"DEADLOCK_DETECTED ({err.sqlstate})"
        except Exception as err:
            results["Tx1 (Alice->Bob)"] = f"ERROR: {err}"

    def tx_bob_to_alice() -> None:
        """Locks Bob (2), then Alice (1)."""
        try:
            with psycopg.connect(DB_CONN) as conn:
                with conn.cursor() as cur:
                    # Lock Account 2 (Bob)
                    cur.execute("SELECT id, balance FROM accounts WHERE id = 2 FOR UPDATE;")
                    barrier.wait()  # Wait for Tx 1 to lock Account 1

                    time.sleep(0.05)
                    # Now attempt to lock Account 1 (Alice) - will block waiting for Tx 1
                    cur.execute("SELECT id, balance FROM accounts WHERE id = 1 FOR UPDATE;")
                    conn.commit()
                    results["Tx2 (Bob->Alice)"] = "COMMITTED"
        except errors.DeadlockDetected as err:
            results["Tx2 (Bob->Alice)"] = f"DEADLOCK_DETECTED ({err.sqlstate})"
        except Exception as err:
            results["Tx2 (Bob->Alice)"] = f"ERROR: {err}"

    t1 = threading.Thread(target=tx_alice_to_bob)
    t2 = threading.Thread(target=tx_bob_to_alice)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print(f"    Transaction 1 Result : {results.get('Tx1 (Alice->Bob)')}")
    print(f"    Transaction 2 Result : {results.get('Tx2 (Bob->Alice)')}")

    has_deadlock = any("DEADLOCK_DETECTED" in r for r in results.values())
    has_commit = any("COMMITTED" in r for r in results.values())

    if has_deadlock and has_commit:
        print("  \033[1;32m[✓] Deadlock successfully caught & resolved by PostgreSQL!\033[0m")
        print("      PostgreSQL's deadlock detector identified the wait-for cycle, aborted the")
        print("      younger transaction (SQLSTATE 40P01), allowing the other transaction to commit.")


# ----------------------------------------------------------------------
# Part 3: The Fix - Consistent Global Lock Ordering
# ----------------------------------------------------------------------
def test_ordered_locking() -> None:
    print("\n\033[1;34m[*] Part 3: Testing 40 Concurrent Transfers with Consistent Lock Ordering...\033[0m")
    reset_accounts()

    deadlock_count = 0
    success_count = 0

    def transfer(from_id: int, to_id: int, amount: float) -> None:
        nonlocal deadlock_count, success_count
        # KEY MITIGATION: Always lock smaller account ID first, then larger account ID
        first_id, second_id = min(from_id, to_id), max(from_id, to_id)

        try:
            with psycopg.connect(DB_CONN) as conn:
                with conn.cursor() as cur:
                    # Deterministic lock acquisition order
                    cur.execute("SELECT id, balance FROM accounts WHERE id = %s FOR UPDATE;", (first_id,))
                    cur.execute("SELECT id, balance FROM accounts WHERE id = %s FOR UPDATE;", (second_id,))

                    cur.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s;", (amount, from_id))
                    cur.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s;", (amount, to_id))
                    conn.commit()
                    success_count += 1
        except errors.DeadlockDetected:
            deadlock_count += 1

    # Launch 40 concurrent transfers in opposite directions (20 Alice->Bob, 20 Bob->Alice)
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = []
        for i in range(40):
            if i % 2 == 0:
                futures.append(executor.submit(transfer, 1, 2, 10.0))
            else:
                futures.append(executor.submit(transfer, 2, 1, 10.0))
        concurrent.futures.wait(futures)

    alice_bal = get_balance(1)
    bob_bal = get_balance(2)

    print(f"    Successful Transfers : \033[1;32m{success_count} / 40\033[0m")
    print(f"    Deadlocks Caught     : \033[1;32m{deadlock_count}\033[0m (0 expected)")
    print(f"    Alice Final Balance  : ${alice_bal:,.2f}")
    print(f"    Bob Final Balance    : ${bob_bal:,.2f}")
    print(f"    Total System Wealth  : ${alice_bal + bob_bal:,.2f} ($2,000.00 expected)")

    if deadlock_count == 0 and (alice_bal + bob_bal) == 2000.00:
        print("  [✓] Zero deadlocks! Consistent lock ordering broke the circular wait condition.")


def main() -> None:
    try:
        with psycopg.connect(DB_CONN, connect_timeout=3):
            pass
    except Exception:
        print("\033[1;31mError: Cannot connect to PostgreSQL on 127.0.0.1:5432/lab_db.\033[0m")
        print("Please start the locks container first:")
        print("  cd postgres && make up EXP=locks")
        return

    print("\033[1;36m=================================================================\033[0m")
    print("\033[1;36mExperiment 02: PostgreSQL Lock Contention & Deadlock Detection\033[0m")
    print("Testing row-level exclusive locks and deadlock detection dynamics.")
    print("\033[1;36m=================================================================\033[0m")

    # Part 1: Hot row queue
    test_hot_row_contention()

    # Part 2: Deadlock creation
    test_deadlock()

    # Part 3: Ordered lock fix
    test_ordered_locking()

    print("\n\033[1;32m=================================================================")
    print("SYSTEMS TAKEAWAYS")
    print("=================================================================\033[0m")
    print("1. Hot rows serialize through exclusive row locks; no lost updates occur.")
    print("2. Deadlocks occur when concurrent transactions acquire locks out of order.")
    print("3. Deadlock detection is reactive (costly search in lock wait-graph).")
    print("4. Preventive fix: Always acquire locks in ascending primary key order.")
    print("=================================================================\n")


if __name__ == "__main__":
    main()

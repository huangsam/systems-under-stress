# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "psycopg[binary]>=3.1.0",
# ]
# ///
"""
Experiment 01: Transaction Isolation Anomalies & Write Skew in PostgreSQL

Demonstrates:
1. Dirty Read Immunity: PostgreSQL treats READ UNCOMMITTED as READ COMMITTED.
2. The Write Skew Anomaly (Doctors on Call):
   - Constraint: At least 1 doctor must remain on call at all times.
   - Under REPEATABLE READ: Two concurrent transactions read overlapping state,
     both commit, leaving 0 doctors on call (INVARIANT BROKEN).
   - Under SERIALIZABLE: PostgreSQL Serializable Snapshot Isolation (SSI) detects
     the read/write dependency cycle and aborts one transaction with SQLSTATE 40001.
"""

from __future__ import annotations

import threading
import time

import psycopg
from psycopg import errors

DB_CONN = "postgresql://postgres:postgres@127.0.0.1:5432/lab_db"


def reset_doctors() -> None:
    with psycopg.connect(DB_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE doctors RESTART IDENTITY;")
            cur.execute("INSERT INTO doctors (name, on_call) VALUES ('Dr. Alice', TRUE), ('Dr. Bob', TRUE);")


def count_on_call() -> int:
    with psycopg.connect(DB_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM doctors WHERE on_call = TRUE;")
            return cur.fetchone()[0]


# ----------------------------------------------------------------------
# Part 1: Verify Dirty Read Immunity
# ----------------------------------------------------------------------
def test_dirty_read() -> None:
    print("\n\033[1;34m[*] Part 1: Testing READ UNCOMMITTED (Dirty Read check)...\033[0m")
    reset_doctors()

    barrier = threading.Barrier(2)
    dirty_read_detected = False

    def session_a() -> None:
        # Session A updates a doctor but DOES NOT commit yet
        with psycopg.connect(DB_CONN) as conn:
            conn.set_isolation_level(psycopg.IsolationLevel.READ_UNCOMMITTED)
            with conn.cursor() as cur:
                cur.execute("UPDATE doctors SET name = 'Dr. Impostor' WHERE id = 1;")
                barrier.wait()  # Signal Session B to read
                time.sleep(0.3)
                conn.rollback()

    def session_b() -> None:
        nonlocal dirty_read_detected
        with psycopg.connect(DB_CONN) as conn:
            conn.set_isolation_level(psycopg.IsolationLevel.READ_UNCOMMITTED)
            with conn.cursor() as cur:
                barrier.wait()  # Wait for Session A to perform uncommitted update
                cur.execute("SELECT name FROM doctors WHERE id = 1;")
                name = cur.fetchone()[0]
                if name == "Dr. Impostor":
                    dirty_read_detected = True

    t_a = threading.Thread(target=session_a)
    t_b = threading.Thread(target=session_b)
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    if dirty_read_detected:
        print("  \033[1;31m[!] Dirty read occurred!\033[0m")
    else:
        print("  \033[1;32m[✓] Dirty read prevented:\033[0m Session B only saw committed data.")
        print("      PostgreSQL treats READ UNCOMMITTED as READ COMMITTED under its MVCC engine.")


# ----------------------------------------------------------------------
# Part 2: Write Skew Under REPEATABLE READ vs SERIALIZABLE
# ----------------------------------------------------------------------
def run_write_skew_simulation(isolation_level: psycopg.IsolationLevel, label: str) -> None:
    print(f"\n\033[1;34m[*] Part 2: Simulating Write Skew under '{label}'...\033[0m")
    reset_doctors()

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def step_down(doctor_name: str) -> None:
        try:
            with psycopg.connect(DB_CONN) as conn:
                conn.set_isolation_level(isolation_level)
                with conn.cursor() as cur:
                    # 1. Check if safe to step down (requires >= 2 on call)
                    cur.execute("SELECT count(*) FROM doctors WHERE on_call = TRUE;")
                    on_call_count = cur.fetchone()[0]

                    # Synchronize both threads so both read count=2 before either updates
                    barrier.wait()

                    if on_call_count >= 2:
                        time.sleep(0.05)  # Simulate small application compute latency
                        cur.execute("UPDATE doctors SET on_call = FALSE WHERE name = %s;", (doctor_name,))
                        conn.commit()
                        results[doctor_name] = "COMMITTED"
                    else:
                        results[doctor_name] = "ABORTED_BY_APP"
        except errors.SerializationFailure as err:
            results[doctor_name] = f"SERIALIZATION_FAILURE ({err.sqlstate})"
        except Exception as err:
            results[doctor_name] = f"ERROR: {err}"

    t_alice = threading.Thread(target=step_down, args=("Dr. Alice",))
    t_bob = threading.Thread(target=step_down, args=("Dr. Bob",))

    t_alice.start()
    t_bob.start()
    t_alice.join()
    t_bob.join()

    remaining = count_on_call()

    print(f"    Dr. Alice Result : {results.get('Dr. Alice')}")
    print(f"    Dr. Bob Result   : {results.get('Dr. Bob')}")
    print(f"    Doctors Still On Call : \033[1;{'31' if remaining == 0 else '32'}m{remaining}\033[0m (Required: >= 1)")

    if remaining == 0:
        print("  \033[1;31m[!] WRITE SKEW ANOMALY OCCURRED!\033[0m")
        print("      Both transactions read overlapping snapshots, updated non-overlapping rows,")
        print("      and committed successfully—violating the global system invariant.")
    else:
        print("  \033[1;32m[✓] INVARIANT PRESERVED BY SERIALIZABLE SNAPSHOT ISOLATION (SSI)!\033[0m")
        print("      PostgreSQL detected the rw-antidependency cycle and aborted one transaction.")


def main() -> None:
    try:
        with psycopg.connect(DB_CONN, connect_timeout=3):
            pass
    except Exception:
        print("\033[1;31mError: Cannot connect to PostgreSQL on 127.0.0.1:5432/lab_db.\033[0m")
        print("Please start the container first:")
        print("  cd postgres && make up EXP=isolation")
        return

    print("\033[1;36m=================================================================\033[0m")
    print("\033[1;36mExperiment 01: PostgreSQL Transaction Isolation & Write Skew\033[0m")
    print("Testing concurrency anomalies across MVCC isolation levels.")
    print("\033[1;36m=================================================================\033[0m")

    # 1. Dirty Read test
    test_dirty_read()

    # 2. Write Skew under Repeatable Read
    run_write_skew_simulation(psycopg.IsolationLevel.REPEATABLE_READ, "REPEATABLE READ")

    # 3. Write Skew under Serializable
    run_write_skew_simulation(psycopg.IsolationLevel.SERIALIZABLE, "SERIALIZABLE")

    print("\n\033[1;32m=================================================================")
    print("SYSTEMS TAKEAWAYS")
    print("=================================================================\033[0m")
    print("1. PostgreSQL does not implement Dirty Reads; READ UNCOMMITTED = READ COMMITTED.")
    print("2. REPEATABLE READ protects against Phantom Reads but FAILS on Write Skew.")
    print("3. Write Skew can only be solved by: SELECT ... FOR UPDATE (explicit locking)")
    print("   or true SERIALIZABLE isolation (with automatic client retry on 40001).")
    print("=================================================================\n")


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "psycopg[binary]>=3.1.0",
# ]
# ///
"""
PostgreSQL Crash Course: Core Relational Concepts & Diagnostics

Demonstrates:
1. Connectivity & engine version check.
2. DDL & table schema creation.
3. Parameterized CRUD operations (single & batch insert, SELECT, UPDATE).
4. Transaction semantics (COMMIT vs ROLLBACK).
5. Diagnostic inspection (pg_stat_activity, table disk footprint).
"""

from __future__ import annotations

import sys

import psycopg

DB_CONN = "postgresql://postgres:postgres@127.0.0.1:5432/lab_db"
TABLE_NAME = "crash_course_items"


def main() -> None:
    print("\n\033[1;36m" + "=" * 70 + "\033[0m")
    print("\033[1;32mPostgreSQL Crash Course: Relational Fundamentals & Diagnostics\033[0m")
    print("\033[1;36m" + "=" * 70 + "\033[0m\n")

    try:
        conn = psycopg.connect(DB_CONN, autocommit=True)
    except Exception as e:
        print(f"\033[1;31m[!] Connection failed: {e}\033[0m")
        print("Start the lab container first: cd postgres && make up EXP=isolation")
        sys.exit(1)

    with conn:
        with conn.cursor() as cur:
            # 1. Connectivity & Version
            print("\033[1;34m[*] 1. Server Version & Connectivity\033[0m")
            cur.execute("SELECT version();")
            version_str = cur.fetchone()[0]
            print(f"    Version: {version_str.split(',')[0]}")

            # 2. Schema DDL
            print("\n\033[1;34m[*] 2. Creating Demo Table\033[0m")
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_NAME};")
            cur.execute(f"""
                CREATE TABLE {TABLE_NAME} (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    sku TEXT UNIQUE NOT NULL,
                    price NUMERIC(10, 2) NOT NULL,
                    stock INT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
            print(f"    Created table '{TABLE_NAME}'.")

            # 3. Parameterized Insert & Batch Insert
            print("\n\033[1;34m[*] 3. Parameterized Inserts & Reads\033[0m")
            cur.execute(
                f"INSERT INTO {TABLE_NAME} (name, sku, price, stock) VALUES (%s, %s, %s, %s) RETURNING id;",
                ("Mechanical Keyboard", "KEY-001", 129.99, 15),
            )
            inserted_id = cur.fetchone()[0]
            print(f"    Inserted single row with ID: {inserted_id}")

            batch_data = [
                ("Gaming Mouse", "MOU-002", 59.50, 40),
                ("UltraWide Monitor", "MON-003", 399.00, 8),
                ("Desk Mat", "MAT-004", 25.00, 100),
            ]
            cur.executemany(
                f"INSERT INTO {TABLE_NAME} (name, sku, price, stock) VALUES (%s, %s, %s, %s);",
                batch_data,
            )
            print(f"    Batch-inserted {len(batch_data)} additional rows.")

            # Read back
            cur.execute(f"SELECT id, name, sku, price, stock FROM {TABLE_NAME} ORDER BY id;")
            rows = cur.fetchall()
            print(f"    Total rows in {TABLE_NAME}: {len(rows)}")
            for r in rows:
                print(f"      - ID {r[0]}: {r[1]} ({r[2]}) -> ${r[3]} [Stock: {r[4]}]")

            # Update
            cur.execute(f"UPDATE {TABLE_NAME} SET stock = stock - 2 WHERE sku = %s;", ("KEY-001",))
            print("    Decremented stock for 'KEY-001'.")

    # 4. Transactions: Commit vs Rollback
    print("\n\033[1;34m[*] 4. Transaction Semantics (Atomic Rollback)\033[0m")
    with psycopg.connect(DB_CONN, autocommit=False) as tx_conn:
        with tx_conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {TABLE_NAME};")
            initial_count = cur.fetchone()[0]

            print("    Beginning transaction, inserting candidate row...")
            cur.execute(
                f"INSERT INTO {TABLE_NAME} (name, sku, price, stock) VALUES (%s, %s, %s, %s);",
                ("Aborted Product", "ABT-999", 9.99, 1),
            )
            print("    Explicitly rolling back transaction...")
            tx_conn.rollback()

    with psycopg.connect(DB_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {TABLE_NAME};")
            post_count = cur.fetchone()[0]
            assert post_count == initial_count, "Rollback invariant violated!"
            print(f"    Verified row count unchanged ({post_count} rows). Rollback succeeded.")

            # 5. Diagnostics: pg_stat_activity & Table Footprint
            print("\n\033[1;34m[*] 5. Diagnostic Queries & Catalog Metadata\033[0m")
            cur.execute("""
                SELECT pid, usename, client_addr, state, wait_event_type, wait_event
                FROM pg_stat_activity
                WHERE datname = current_database() AND pid != pg_backend_pid();
            """)
            backends = cur.fetchall()
            print(f"    Concurrent backends on '{cur.connection.info.dbname}': {len(backends)}")

            cur.execute(f"""
                SELECT
                    pg_size_pretty(pg_total_relation_size('{TABLE_NAME}')) AS total_size,
                    pg_size_pretty(pg_relation_size('{TABLE_NAME}')) AS table_size,
                    pg_size_pretty(pg_indexes_size('{TABLE_NAME}')) AS index_size;
            """)
            sizes = cur.fetchone()
            print(f"    Table '{TABLE_NAME}' Footprint: Total: {sizes[0]}, Heap: {sizes[1]}, Indexes: {sizes[2]}")

            # Cleanup
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_NAME};")
            print(f"    Cleaned up demo table '{TABLE_NAME}'.")

    print("\n\033[1;32m[✓] PostgreSQL Crash Course completed successfully!\033[0m\n")


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
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
from rich.console import Console
from rich.markup import escape

DB_CONN = "postgresql://postgres:postgres@127.0.0.1:5432/lab_db"
TABLE_NAME = "crash_course_items"


console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def main() -> None:
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("[bold green]PostgreSQL Crash Course: Relational Fundamentals & Diagnostics[/]")
    console.print("=" * 70 + "\n", style="bold cyan")

    try:
        conn = psycopg.connect(DB_CONN, autocommit=True)
    except Exception as e:
        console.print(f"[bold red][!] Connection failed: {escape(str(e))}[/]")
        plain_console.print("Start the lab container first: cd postgres && make up EXP=isolation")
        sys.exit(1)

    with conn:
        with conn.cursor() as cur:
            # 1. Connectivity & Version
            console.print("[bold blue][*] 1. Server Version & Connectivity[/]")
            cur.execute("SELECT version();")
            version_str = cur.fetchone()[0]
            verify_phase("PostgreSQL" in version_str, "Phase 1: Connectivity", f"Unexpected version string: {version_str}")
            plain_console.print(f"    Version: {version_str.split(',')[0]}")

            # 2. Schema DDL
            console.print("\n[bold blue][*] 2. Creating Demo Table[/]")
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
            cur.execute("SELECT to_regclass(%s);", (TABLE_NAME,))
            verify_phase(cur.fetchone()[0] is not None, "Phase 2: DDL", f"Table '{TABLE_NAME}' was not created")
            plain_console.print(f"    Created table '{TABLE_NAME}'.")

            # 3. Parameterized Insert & Batch Insert
            console.print("\n[bold blue][*] 3. Parameterized Inserts & Reads[/]")
            cur.execute(
                f"INSERT INTO {TABLE_NAME} (name, sku, price, stock) VALUES (%s, %s, %s, %s) RETURNING id;",
                ("Mechanical Keyboard", "KEY-001", 129.99, 15),
            )
            inserted_id = cur.fetchone()[0]
            verify_phase(inserted_id == 1, "Phase 3: Insert", f"Expected inserted_id=1, got {inserted_id}")
            plain_console.print(f"    Inserted single row with ID: {inserted_id}")

            batch_data = [
                ("Gaming Mouse", "MOU-002", 59.50, 40),
                ("UltraWide Monitor", "MON-003", 399.00, 8),
                ("Desk Mat", "MAT-004", 25.00, 100),
            ]
            cur.executemany(
                f"INSERT INTO {TABLE_NAME} (name, sku, price, stock) VALUES (%s, %s, %s, %s);",
                batch_data,
            )
            plain_console.print(f"    Batch-inserted {len(batch_data)} additional rows.")

            # Read back
            cur.execute(f"SELECT id, name, sku, price, stock FROM {TABLE_NAME} ORDER BY id;")
            rows = cur.fetchall()
            verify_phase(len(rows) == 4, "Phase 3: Read Back", f"Expected 4 rows, got {len(rows)}")
            plain_console.print(f"    Total rows in {TABLE_NAME}: {len(rows)}")
            for r in rows:
                plain_console.print(f"      - ID {r[0]}: {r[1]} ({r[2]}) -> ${r[3]} [Stock: {r[4]}]")

            # Update
            cur.execute(f"UPDATE {TABLE_NAME} SET stock = stock - 2 WHERE sku = %s RETURNING stock;", ("KEY-001",))
            updated_stock = cur.fetchone()[0]
            verify_phase(updated_stock == 13, "Phase 3: Update", f"Expected KEY-001 stock=13, got {updated_stock}")
            plain_console.print("    Decremented stock for 'KEY-001'.")

    # 4. Transactions: Commit vs Rollback
    console.print("\n[bold blue][*] 4. Transaction Semantics (Atomic Rollback)[/]")
    with psycopg.connect(DB_CONN, autocommit=False) as tx_conn:
        with tx_conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {TABLE_NAME};")
            initial_count = cur.fetchone()[0]

            plain_console.print("    Beginning transaction, inserting candidate row...")
            cur.execute(
                f"INSERT INTO {TABLE_NAME} (name, sku, price, stock) VALUES (%s, %s, %s, %s);",
                ("Aborted Product", "ABT-999", 9.99, 1),
            )
            plain_console.print("    Explicitly rolling back transaction...")
            tx_conn.rollback()

    with psycopg.connect(DB_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {TABLE_NAME};")
            post_count = cur.fetchone()[0]
            verify_phase(post_count == initial_count, "Phase 4: Rollback", f"Rollback invariant violated: {post_count} != {initial_count}")
            plain_console.print(f"    Verified row count unchanged ({post_count} rows). Rollback succeeded.")

            # 5. Diagnostics: pg_stat_activity & Table Footprint
            console.print("\n[bold blue][*] 5. Diagnostic Queries & Catalog Metadata[/]")
            cur.execute("""
                SELECT pid, usename, client_addr, state, wait_event_type, wait_event
                FROM pg_stat_activity
                WHERE datname = current_database() AND pid != pg_backend_pid();
            """)
            backends = cur.fetchall()
            plain_console.print(f"    Concurrent backends on '{cur.connection.info.dbname}': {len(backends)}")

            cur.execute(f"""
                SELECT
                    pg_size_pretty(pg_total_relation_size('{TABLE_NAME}')) AS total_size,
                    pg_size_pretty(pg_relation_size('{TABLE_NAME}')) AS table_size,
                    pg_size_pretty(pg_indexes_size('{TABLE_NAME}')) AS index_size;
            """)
            sizes = cur.fetchone()
            verify_phase(sizes is not None and all(sizes), "Phase 5: Diagnostics", "Missing table footprint diagnostics")
            plain_console.print(f"    Table '{TABLE_NAME}' Footprint: Total: {sizes[0]}, Heap: {sizes[1]}, Indexes: {sizes[2]}")

            # Cleanup
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_NAME};")
            cur.execute("SELECT to_regclass(%s);", (TABLE_NAME,))
            verify_phase(cur.fetchone()[0] is None, "Phase 5: Cleanup", f"Demo table '{TABLE_NAME}' still exists after DROP")
            plain_console.print(f"    Cleaned up demo table '{TABLE_NAME}'.")

    console.print("\n[bold green][✓] PostgreSQL Crash Course completed successfully![/]\n")


if __name__ == "__main__":
    main()

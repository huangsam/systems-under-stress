# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7.0",
#     "cassandra-driver>=3.29.0",
# ]
# ///
"""
Apache Cassandra Crash Course: Wide-Column Storage, Partition Routing & CQL Mechanics

Demonstrates:
1. Cluster connectivity & node introspection via system.local.
2. Keyspace & table provisioning with composite primary keys ((partition_key), clustering_column).
3. Prepared statements & efficient partition-targeted inserts.
4. Partition-bounded reads (O(1) token hash lookup) vs token range scans.
5. Catalog introspection (system_schema.tables, token metadata).
"""

from __future__ import annotations

import sys
import uuid

from cassandra.cluster import Cluster
from cassandra.query import SimpleStatement
from rich.console import Console

HOST = "127.0.0.1"
PORT = 9042
KEYSPACE = "lab_crash_course"
TABLE_NAME = "users_by_country"


console = Console()


def main() -> None:
    console.print("\n[bold cyan]" + "=" * 70 + "[/]")
    console.print("[bold green]Apache Cassandra Crash Course: Partition Keys & CQL Mechanics[/]")
    console.print("[bold cyan]" + "=" * 70 + "[/]\n")

    try:
        cluster = Cluster([HOST], port=PORT)
        session = cluster.connect()
    except Exception as e:
        console.print(f"[bold red][!] Connection failed: {e}[/]")
        print("Start the lab container first: cd cassandra && make up EXP=tombstones")
        sys.exit(1)

    try:
        # 1. Cluster Metadata & Connectivity
        console.print("[bold blue][*] 1. Node Metadata & System Introspection[/]")
        row = session.execute("SELECT cluster_name, data_center, release_version FROM system.local").one()
        print(f"    Cluster Name: {row.cluster_name}")
        print(f"    Datacenter: {row.data_center}, Cassandra Version: {row.release_version}")

        # 2. Keyspace & Table Creation
        console.print("\n[bold blue][*] 2. Creating Keyspace & Table Schema[/]")
        session.execute(f"""
            CREATE KEYSPACE IF NOT EXISTS {KEYSPACE}
            WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': 1}};
        """)
        session.set_keyspace(KEYSPACE)

        session.execute(f"DROP TABLE IF EXISTS {TABLE_NAME};")
        session.execute(f"""
            CREATE TABLE {TABLE_NAME} (
                country text,
                user_id uuid,
                username text,
                email text,
                score int,
                PRIMARY KEY ((country), user_id)
            );
        """)
        print(f"    Created keyspace '{KEYSPACE}' and table '{TABLE_NAME}'.")
        print("    Schema Notes: 'country' is Partition Key (token hash); 'user_id' is Clustering Column.")

        # 3. Prepared Statements & Inserts
        console.print("\n[bold blue][*] 3. Prepared Statements & Partition-Targeted Writes[/]")
        insert_stmt = session.prepare(f"""
            INSERT INTO {TABLE_NAME} (country, user_id, username, email, score)
            VALUES (?, ?, ?, ?, ?);
        """)

        records = [
            ("US", uuid.uuid4(), "alice_us", "alice@example.com", 95),
            ("US", uuid.uuid4(), "bob_us", "bob@example.com", 82),
            ("CA", uuid.uuid4(), "charlie_ca", "charlie@example.ca", 88),
            ("DE", uuid.uuid4(), "david_de", "david@example.de", 91),
            ("US", uuid.uuid4(), "eve_us", "eve@example.com", 99),
        ]

        for country, uid, uname, email, score in records:
            session.execute(insert_stmt, (country, uid, uname, email, score))
        print(f"    Inserted {len(records)} users across 3 distinct country partitions (US, CA, DE).")

        # 4. Partition-bounded reads
        console.print("\n[bold blue][*] 4. Querying by Partition Key (O(1) Token Routing)[/]")
        query = SimpleStatement(f"SELECT country, username, email, score FROM {TABLE_NAME} WHERE country = 'US';")
        rows = session.execute(query)
        print("    Querying partition 'country = US':")
        for r in rows:
            print(f"      - [{r.country}] {r.username} ({r.email}) -> Score: {r.score}")

        # 5. Schema Introspection
        console.print("\n[bold blue][*] 5. Schema Introspection (system_schema.columns)[/]")
        schema_query = f"""
            SELECT column_name, kind, type
            FROM system_schema.columns
            WHERE keyspace_name = '{KEYSPACE}' AND table_name = '{TABLE_NAME}';
        """
        cols = session.execute(schema_query)
        print(f"    Column definitions for {KEYSPACE}.{TABLE_NAME}:")
        for col in cols:
            print(f"      - {col.column_name}: {col.type} (kind: {col.kind})")

        # Cleanup
        session.execute(f"DROP KEYSPACE IF EXISTS {KEYSPACE};")
        print(f"    Cleaned up demo keyspace '{KEYSPACE}'.")

    finally:
        cluster.shutdown()

    console.print("\n[bold green][✓] Apache Cassandra Crash Course completed successfully![/]\n")


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "psycopg[binary]>=3.1.0",
# ]
# ///
"""
Experiment 03: PostgreSQL Memory Limits (work_mem) & Sort Spill to Disk

Demonstrates:
1. In-Memory vs External Merge Sort:
   - With low work_mem (4MB), sorting 150,000 rows exceeds memory and spills to temporary disk files.
   - EXPLAIN (ANALYZE, BUFFERS) clearly displays: 'Sort Method: external merge  Disk: X kB'.
2. Session-Level Memory Tuning:
   - Bumping session-level work_mem (64MB) allows the exact same sort to execute entirely in RAM.
   - EXPLAIN (ANALYZE, BUFFERS) flips to: 'Sort Method: quicksort  Memory: X kB', yielding significant speedups.
3. Production Sizing Dilemma:
   - Why setting global work_mem too high causes catastrophic server-wide OOM kills.
"""
from __future__ import annotations

import re
import time
import psycopg

DB_CONN = "postgresql://postgres:postgres@127.0.0.1:5432/lab_db"


def seed_data() -> None:
    print("[*] Preparing 150,000 test rows in 'analytics_events' table...")
    with psycopg.connect(DB_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL,
                event_name VARCHAR(100) NOT NULL,
                payload TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """)

            cur.execute("SELECT count(*) FROM analytics_events;")
            count = cur.fetchone()[0]
            if count >= 150000:
                print(f"    Table already seeded with {count:,} rows.")
                return

            print("    Generating 150,000 synthetic rows (using generate_series)...")
            t0 = time.perf_counter()
            cur.execute("""
            TRUNCATE TABLE analytics_events;
            INSERT INTO analytics_events (user_id, event_name, payload, created_at)
            SELECT 
                (random() * 50000)::INT,
                'page_view_' || (random() * 10)::INT,
                md5(g::TEXT) || repeat('DATA_PAYLOAD_PADDING_', 2),
                NOW() - (random() * interval '30 days')
            FROM generate_series(1, 150000) AS g;
            """)
            print(f"    Seeded 150,000 rows in {time.perf_counter() - t0:.2f}s.")


def run_explain_sort(work_mem_setting: str | None = None) -> dict:
    with psycopg.connect(DB_CONN) as conn:
        with conn.cursor() as cur:
            if work_mem_setting:
                cur.execute(f"SET work_mem = '{work_mem_setting}';")

            cur.execute("SHOW work_mem;")
            effective_mem = cur.fetchone()[0]

            t0 = time.perf_counter()
            # Sort 150,000 rows by text payload
            cur.execute("""
            EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
            SELECT * FROM analytics_events ORDER BY payload;
            """)
            lines = [row[0] for row in cur.fetchall()]
            elapsed_ms = (time.perf_counter() - t0) * 1000

            full_plan = "\n".join(lines)

            # Extract Sort Method line
            sort_match = re.search(r"Sort Method:\s*([^\n]+)", full_plan)
            sort_method = sort_match.group(1) if sort_match else "Unknown"

            # Extract execution time from plan
            exec_match = re.search(r"Execution Time:\s*([\d\.]+)\s*ms", full_plan)
            plan_time_ms = float(exec_match.group(1)) if exec_match else elapsed_ms

            return {
                "work_mem": effective_mem,
                "sort_method": sort_method,
                "plan_time_ms": plan_time_ms,
                "wall_time_ms": elapsed_ms,
            }


def main() -> None:
    try:
        with psycopg.connect(DB_CONN, connect_timeout=3):
            pass
    except Exception:
        print("\033[1;31mError: Cannot connect to PostgreSQL on 127.0.0.1:5432/lab_db.\033[0m")
        print("Please start the work-mem container first:")
        print("  cd postgres && make up EXP=work-mem")
        return

    print("\033[1;36m=================================================================\033[0m")
    print("\033[1;36mExperiment 03: PostgreSQL Memory Limits (work_mem) & Disk Spill\033[0m")
    print("Measuring sort performance across in-memory vs disk-spill boundaries.")
    print("\033[1;36m=================================================================\033[0m\n")

    seed_data()

    # 1. Run with container default work_mem (4MB)
    print("\n\033[1;34m[*] Test 1: Sorting with default work_mem (4MB)...\033[0m")
    res1 = run_explain_sort(None)
    print(f"    Effective work_mem  : {res1['work_mem']}")
    print(f"    Sort Method         : \033[1;31m{res1['sort_method']}\033[0m")
    print(f"    Engine Execution    : {res1['plan_time_ms']:.1f}ms")

    # 2. Run with tuned session work_mem (64MB)
    print("\n\033[1;34m[*] Test 2: Sorting with tuned session work_mem (64MB)...\033[0m")
    res2 = run_explain_sort("64MB")
    print(f"    Effective work_mem  : {res2['work_mem']}")
    print(f"    Sort Method         : \033[1;32m{res2['sort_method']}\033[0m")
    print(f"    Engine Execution    : {res2['plan_time_ms']:.1f}ms")

    speedup = res1["plan_time_ms"] / res2["plan_time_ms"] if res2["plan_time_ms"] > 0 else 1.0

    print("\n\033[1;32m" + "=" * 65)
    print("EXPERIMENT 03 SUMMARY REPORT")
    print("=" * 65 + "\033[0m")
    print(f"With work_mem = 4MB  : {res1['sort_method']} ({res1['plan_time_ms']:.1f}ms)")
    print(f"With work_mem = 64MB : {res2['sort_method']} ({res2['plan_time_ms']:.1f}ms)")
    print(f"Performance Gain     : \033[1;32m{speedup:.1f}x Faster\033[0m in pure RAM")
    print("=" * 65)

    print("\n\033[1;33m[Systems Takeaway - The work_mem Multiplier Dilemma]\033[0m")
    print("Why not set work_mem = 1GB globally on your database server?")
    print("work_mem is NOT a server-wide cap. It is allocated PER SORT/HASH NODE per query.")
    print("If 100 concurrent queries each contain 3 joins/sorts:")
    print("  100 connections * 3 nodes * 1GB work_mem = 300GB RAM!")
    print("The Linux kernel OOM Killer would immediately terminate PostgreSQL.")
    print("Best Practice: Keep global work_mem modest (4MB-16MB) and elevate it only")
    print("for specific analytical queries via 'SET LOCAL work_mem = ...'.")


if __name__ == "__main__":
    main()

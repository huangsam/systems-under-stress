# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "confluent-kafka>=2.6.0",
# ]
# ///
"""
Experiment 02: Offset Commit Pitfalls (Auto-Commit Data Loss vs. Poison-Pill DLQ)

Demonstrates:
1. Phase A (enable.auto.commit=True Crash Mid-Batch -> Silent Message Loss):
   - Consumer fetches a 10-message batch with `auto.commit.interval.ms=200`.
   - Processes 3 messages, stalls past the commit interval, and crashes before
     completing the remaining 7 messages in the batch.
   - Auto-commit advances the group offset past the entire fetched batch; the
     replacement consumer silently skips the 7 unprocessed messages.
2. Phase B (Manual Commit Without DLQ -> Poison-Pill Head-of-Line Blocking):
   - Consumer uses `enable.auto.commit=False` and commits only after success.
   - A malformed poison-pill record at offset 4 triggers repeated redeliveries
     and permanently blocks offsets 5..9.
3. Phase C (Manual Commit + Bounded Retry + Dead Letter Queue):
   - Retries transient failures up to `MAX_RETRIES=2`, then routes the poison
     pill to a DLQ topic with diagnostic headers, commits, and unblocks the
     partition.
"""

from __future__ import annotations

import json
import sys
import time
import uuid

from confluent_kafka import Consumer, Producer
from confluent_kafka.admin import AdminClient, NewTopic
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

BOOTSTRAP_SERVERS = "127.0.0.1:9092"

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def create_topic(admin: AdminClient, topic: str, partitions: int = 1) -> None:
    futures = admin.create_topics([NewTopic(topic, num_partitions=partitions, replication_factor=1)])
    for fut in futures.values():
        fut.result(timeout=10.0)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        meta = admin.list_topics(topic=topic, timeout=2.0)
        t = meta.topics.get(topic)
        if t and t.error is None and len(t.partitions) == partitions:
            return
        time.sleep(0.2)
    verify_phase(False, "Setup", f"Topic {topic} did not become ready")


def produce_records(topic: str, records: list[dict]) -> None:
    producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
    for rec in records:
        producer.produce(topic, value=json.dumps(rec).encode())
    remaining = producer.flush(timeout=10.0)
    verify_phase(remaining == 0, "Produce", f"Failed to flush {remaining} records")


def run_phase_a_autocommit_loss(admin: AdminClient) -> tuple[int, int, int]:
    console.print("\n[bold blue][*] Phase A: enable.auto.commit=True Crash Mid-Batch[/]")
    topic = f"exp02-autocommit-{uuid.uuid4().hex[:8]}"
    group_id = f"exp02-auto-grp-{uuid.uuid4().hex[:8]}"
    create_topic(admin, topic, partitions=1)

    records = [{"id": i, "payload": f"order-{i:02d}"} for i in range(20)]
    produce_records(topic, records)

    c1 = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
            "auto.commit.interval.ms": 200,
        }
    )
    c1.subscribe([topic])

    # Fetch first batch of 10 messages
    batch: list = []
    deadline = time.monotonic() + 15.0
    while len(batch) < 10 and time.monotonic() < deadline:
        polled = c1.consume(num_messages=10 - len(batch), timeout=1.0)
        batch.extend(m for m in polled if m is not None and not m.error())

    verify_phase(len(batch) == 10, "Phase A: Batch Fetch", f"Expected 10 messages in batch, got {len(batch)}")

    processed_ids: list[int] = []
    for idx, msg in enumerate(batch):
        data = json.loads(msg.value().decode())
        if idx < 3:
            processed_ids.append(data["id"])
            time.sleep(0.12)  # Total > 360ms > auto.commit.interval.ms=200
        else:
            # Simulate crash mid-batch; closing consumer triggers final auto-commit of polled offset (10)
            plain_console.print(f"    [!] C1 crashed while processing id={data['id']} (offset {msg.offset()}) after batch poll...")
            break
    c1.close()

    # Replacement consumer joins same group
    c2 = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
        }
    )
    c2.subscribe([topic])

    c2_ids: list[int] = []
    deadline = time.monotonic() + 10.0
    idle_polls = 0
    while time.monotonic() < deadline and idle_polls < 6:
        msg = c2.poll(0.3)
        if msg is None:
            idle_polls += 1
            continue
        if msg.error():
            continue
        idle_polls = 0
        c2_ids.append(json.loads(msg.value().decode())["id"])
    c2.close()

    all_processed = set(processed_ids) | set(c2_ids)
    missing = sorted(set(range(20)) - all_processed)
    plain_console.print(f"    C1 processed={processed_ids}, C2 resumed at={c2_ids[:3]}... (total {len(c2_ids)})")
    plain_console.print(f"    [!] Silently lost IDs (skipped by auto-commit): {missing}")

    verify_phase(len(missing) >= 1, "Phase A: Silent Data Loss", "Expected >= 1 silently skipped message under auto-commit crash")
    admin.delete_topics([topic], operation_timeout=5.0)
    return len(processed_ids) + len(c2_ids), len(missing), 0


def run_phase_b_poison_pill_loop(admin: AdminClient) -> tuple[int, int]:
    console.print("\n[bold blue][*] Phase B: Manual Commit Without DLQ (Poison-Pill Redelivery Loop)[/]")
    topic = f"exp02-poison-{uuid.uuid4().hex[:8]}"
    group_id = f"exp02-poison-grp-{uuid.uuid4().hex[:8]}"
    create_topic(admin, topic, partitions=1)

    records = [{"id": i, "corrupt": (i == 4)} for i in range(10)]
    produce_records(topic, records)

    poison_deliveries = 0
    processed_before_block: set[int] = set()
    blocked_tail_count = 0

    for attempt in range(1, 4):
        c = Consumer(
            {
                "bootstrap.servers": BOOTSTRAP_SERVERS,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            }
        )
        c.subscribe([topic])
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            msg = c.poll(0.3)
            if msg is None or msg.error():
                continue
            data = json.loads(msg.value().decode())
            if data.get("corrupt"):
                poison_deliveries += 1
                plain_console.print(f"    [!] Attempt {attempt}: Poison pill id={data['id']} raised ValueError -> offset NOT committed")
                break
            processed_before_block.add(data["id"])
            if data["id"] > 4:
                blocked_tail_count += 1
            c.commit(message=msg, asynchronous=False)
        c.close()

    plain_console.print(
        f"    Phase B summary: poison_deliveries={poison_deliveries}, processed={sorted(processed_before_block)}, tail_processed={blocked_tail_count}"
    )
    verify_phase(poison_deliveries == 3, "Phase B: Redelivery Loop", f"Expected 3 poison redeliveries, got {poison_deliveries}")
    verify_phase(blocked_tail_count == 0, "Phase B: Head-of-Line Block", f"Expected 0 tail messages processed, got {blocked_tail_count}")

    admin.delete_topics([topic], operation_timeout=5.0)
    return poison_deliveries, len(processed_before_block)


def run_phase_c_dlq_recovery(admin: AdminClient) -> tuple[int, int]:
    console.print("\n[bold blue][*] Phase C: Manual Commit + Bounded Retry + Dead Letter Queue (DLQ)[/]")
    topic = f"exp02-dlq-src-{uuid.uuid4().hex[:8]}"
    dlq_topic = f"exp02-dlq-sink-{uuid.uuid4().hex[:8]}"
    group_id = f"exp02-dlq-grp-{uuid.uuid4().hex[:8]}"
    create_topic(admin, topic, partitions=1)
    create_topic(admin, dlq_topic, partitions=1)

    records = [{"id": i, "corrupt": (i == 4)} for i in range(10)]
    produce_records(topic, records)

    dlq_producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([topic])

    processed_valid: list[int] = []
    routed_to_dlq = 0
    max_retries = 2

    deadline = time.monotonic() + 15.0
    while len(processed_valid) + routed_to_dlq < 10 and time.monotonic() < deadline:
        msg = consumer.poll(0.3)
        if msg is None or msg.error():
            continue
        data = json.loads(msg.value().decode())
        success = False
        last_err = ""
        for _retry in range(max_retries + 1):
            try:
                if data.get("corrupt"):
                    raise ValueError("Malformed schema: corrupt=true")
                processed_valid.append(data["id"])
                success = True
                break
            except ValueError as exc:
                last_err = str(exc)

        if not success:
            dlq_producer.produce(
                dlq_topic,
                value=msg.value(),
                headers=[
                    ("original_topic", topic),
                    ("original_offset", str(msg.offset())),
                    ("error", last_err),
                ],
            )
            dlq_producer.flush(timeout=5.0)
            routed_to_dlq += 1
            plain_console.print(f"    [+] Routed poison pill id={data['id']} (offset {msg.offset()}) to DLQ '{dlq_topic}'")

        consumer.commit(message=msg, asynchronous=False)

    consumer.close()

    # Verify DLQ contains the 1 routed message
    dlq_consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": f"dlq-verify-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "earliest",
        }
    )
    dlq_consumer.subscribe([dlq_topic])
    dlq_msgs = []
    dlq_deadline = time.monotonic() + 8.0
    while len(dlq_msgs) < 1 and time.monotonic() < dlq_deadline:
        m = dlq_consumer.poll(0.3)
        if m is not None and not m.error():
            dlq_msgs.append(m)
    dlq_consumer.close()

    verify_phase(len(processed_valid) == 9, "Phase C: Valid Messages", f"Expected 9 valid messages, got {len(processed_valid)}")
    verify_phase(routed_to_dlq == 1 and len(dlq_msgs) == 1, "Phase C: DLQ Delivery", f"Expected 1 DLQ message, got {len(dlq_msgs)}")

    admin.delete_topics([topic, dlq_topic], operation_timeout=5.0)
    return len(processed_valid), len(dlq_msgs)


def main() -> None:
    console.print("\n" + "=" * 72, style="bold cyan")
    console.print("[bold green]Experiment 02: Offset Commit Pitfalls & Poison-Pill DLQ[/]")
    console.print("=" * 72, style="bold cyan")

    admin = AdminClient({"bootstrap.servers": BOOTSTRAP_SERVERS})
    ok_a, lost_a, _ = run_phase_a_autocommit_loss(admin)
    redeliv_b, ok_b = run_phase_b_poison_pill_loop(admin)
    ok_c, dlq_c = run_phase_c_dlq_recovery(admin)

    table = Table(title="Offset Commit & Error Handling Comparison", box=box.ROUNDED, header_style="bold cyan")
    table.add_column("Mode")
    table.add_column("Valid Processed", justify="right")
    table.add_column("Lost / Redelivered", justify="right")
    table.add_column("DLQ Routed", justify="right")
    table.add_column("Outcome")

    table.add_row("Phase A: enable.auto.commit=True", f"{ok_a}/20", f"[bold red]{lost_a} lost[/]", "0", "[bold red]SILENT DATA LOSS[/]")
    table.add_row("Phase B: Manual commit (no DLQ)", f"{ok_b}/9", f"[bold yellow]{redeliv_b}x poison loop[/]", "0", "[bold red]HEAD-OF-LINE BLOCK[/]")
    table.add_row("Phase C: Manual commit + DLQ", f"{ok_c}/9", "0 lost", str(dlq_c), "[bold green]ZERO LOSS + UNBLOCKED[/]")
    console.print("\n", table)
    console.print("\n[bold green][✔] Experiment 02 completed and verified.[/]\n")


if __name__ == "__main__":
    main()

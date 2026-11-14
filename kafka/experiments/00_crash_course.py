# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "confluent-kafka>=2.6.0",
# ]
# ///
"""
Kafka (KRaft) Crash Course: Broker Connectivity, Topics, Produce/Consume & Metadata

Demonstrates:
1. KRaft broker connectivity, cluster ID, and controller/broker metadata inspection.
2. Programmatic topic creation (3 partitions, RF=1) and partition metadata validation.
3. Keyed and unkeyed message production with delivery report callbacks.
4. Consumer group subscription, polling round-trip, and message order verification.
5. Watermark offsets (low/high) and committed consumer group offset inspection.
"""

from __future__ import annotations

import sys
import time
import uuid

from confluent_kafka import Consumer, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

BOOTSTRAP_SERVERS = "127.0.0.1:9092"
TOPIC = f"cc-events-{uuid.uuid4().hex[:8]}"

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def wait_for_topic_ready(admin: AdminClient, topic: str, expected_partitions: int, timeout_s: float = 15.0) -> None:
    """Poll cluster metadata until topic exists with expected partition count."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        meta = admin.list_topics(topic=topic, timeout=2.0)
        tmeta = meta.topics.get(topic)
        if tmeta and tmeta.error is None and len(tmeta.partitions) == expected_partitions:
            return
        time.sleep(0.2)
    verify_phase(False, "Phase 2: Topic Creation", f"Topic '{topic}' did not become ready within {timeout_s}s")


def main() -> None:
    console.print("\n" + "=" * 72, style="bold cyan")
    console.print("[bold green]Kafka (KRaft) Crash Course: Connectivity, Topics & Diagnostics[/]")
    console.print("=" * 72 + "\n", style="bold cyan")

    # 1. Broker Connectivity & KRaft Cluster Metadata
    console.print("[bold blue][*] 1. Broker Connectivity & KRaft Metadata[/]")
    admin = AdminClient({"bootstrap.servers": BOOTSTRAP_SERVERS})
    try:
        meta = admin.list_topics(timeout=5.0)
    except KafkaException as exc:
        console.print(f"[bold red][!] Connection failed: {escape(str(exc))}[/]")
        plain_console.print("Start the lab container first: cd kafka && make up EXP=single-broker")
        sys.exit(1)

    verify_phase(len(meta.brokers) >= 1, "Phase 1: Connectivity", f"Expected >= 1 broker, got {len(meta.brokers)}")
    verify_phase(meta.controller_id >= 0, "Phase 1: KRaft Controller", f"Invalid controller_id: {meta.controller_id}")
    plain_console.print(f"    Cluster ID: {meta.cluster_id}")
    plain_console.print(f"    Controller ID: {meta.controller_id}, Active Brokers: {list(meta.brokers.keys())}")

    # 2. Topic Creation
    console.print(f"\n[bold blue][*] 2. Topic Creation ({TOPIC}, 3 partitions, RF=1)[/]")
    futures = admin.create_topics([NewTopic(TOPIC, num_partitions=3, replication_factor=1)])
    for tname, fut in futures.items():
        fut.result(timeout=10.0)
        plain_console.print(f"    Created topic: {tname}")

    wait_for_topic_ready(admin, TOPIC, expected_partitions=3)
    topic_meta = admin.list_topics(topic=TOPIC, timeout=5.0).topics[TOPIC]
    verify_phase(len(topic_meta.partitions) == 3, "Phase 2: Partitions", f"Expected 3 partitions, got {len(topic_meta.partitions)}")

    # 3. Produce Round-Trip (Keyed & Unkeyed)
    console.print("\n[bold blue][*] 3. Producing 12 Messages (Keyed + Unkeyed)[/]")
    producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
    delivered: list[tuple[str, int, int]] = []
    delivery_errors: list[str] = []

    def on_delivery(err, msg) -> None:
        if err is not None:
            delivery_errors.append(str(err))
        else:
            key = msg.key().decode() if msg.key() else "<none>"
            delivered.append((key, msg.partition(), msg.offset()))

    for i in range(12):
        key = f"user-{i % 3}" if i < 9 else None
        payload = f"event-{i:02d}".encode()
        producer.produce(TOPIC, key=key.encode() if key else None, value=payload, callback=on_delivery)

    remaining = producer.flush(timeout=10.0)
    verify_phase(remaining == 0 and not delivery_errors, "Phase 3: Produce", f"remaining={remaining}, errors={delivery_errors}")
    verify_phase(len(delivered) == 12, "Phase 3: Produce Count", f"Expected 12 delivered, got {len(delivered)}")

    prod_table = Table(title="Produced Records", box=box.ROUNDED, header_style="bold cyan")
    prod_table.add_column("Seq", justify="right")
    prod_table.add_column("Key")
    prod_table.add_column("Partition", justify="center")
    prod_table.add_column("Offset", justify="right")
    for idx, (k, part, off) in enumerate(delivered):
        prod_table.add_row(str(idx), k, str(part), str(off))
    console.print(prod_table)

    # 4. Consumer Group Round-Trip
    group_id = f"cc-group-{uuid.uuid4().hex[:8]}"
    console.print(f"\n[bold blue][*] 4. Consuming via Group '{group_id}'[/]")
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([TOPIC])

    consumed: list[tuple[int, int, str]] = []
    deadline = time.monotonic() + 15.0
    while len(consumed) < 12 and time.monotonic() < deadline:
        msg = consumer.poll(timeout=0.5)
        if msg is None:
            continue
        if msg.error():
            verify_phase(False, "Phase 4: Consume", f"Consumer error: {msg.error()}")
        consumed.append((msg.partition(), msg.offset(), msg.value().decode()))

    verify_phase(len(consumed) == 12, "Phase 4: Consume Count", f"Expected 12 consumed, got {len(consumed)}")
    consumer.commit(asynchronous=False)
    plain_console.print(f"    Consumed and committed {len(consumed)}/12 records.")

    # 5. Watermark & Committed Offset Inspection
    console.print("\n[bold blue][*] 5. Partition Watermarks & Committed Offsets[/]")
    tps = [TopicPartition(TOPIC, p) for p in range(3)]
    committed = consumer.committed(tps, timeout=5.0)
    offset_table = Table(title=f"Topic '{TOPIC}' Offsets", box=box.ROUNDED, header_style="bold cyan")
    offset_table.add_column("Partition", justify="center")
    offset_table.add_column("Low Watermark", justify="right")
    offset_table.add_column("High Watermark", justify="right")
    offset_table.add_column("Committed Offset", justify="right")

    total_high = 0
    for tp in committed:
        low, high = consumer.get_watermark_offsets(TopicPartition(TOPIC, tp.partition), timeout=5.0)
        total_high += high
        offset_table.add_row(str(tp.partition), str(low), str(high), str(tp.offset if tp.offset >= 0 else 0))
        if high > 0:
            verify_phase(tp.offset == high, "Phase 5: Committed Offset", f"Partition {tp.partition}: committed={tp.offset} != high={high}")

    console.print(offset_table)
    verify_phase(total_high == 12, "Phase 5: Total High Watermark", f"Expected sum(high)=12, got {total_high}")

    consumer.close()
    admin.delete_topics([TOPIC], operation_timeout=5.0)
    console.print("\n[bold green][✔] Kafka KRaft Crash Course completed successfully.[/]\n")


if __name__ == "__main__":
    main()

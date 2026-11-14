# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "confluent-kafka>=2.6.0",
# ]
# ///
"""
Experiment 03: Hot-Key Partition Skew & Consumer Group Lag

Demonstrates:
1. Phase A (Hot-Key Skew):
   - Produces 200 messages where 90% (180) share a single hot key (`tenant:vip-1`).
   - Murmur2 hashing pins all 180 hot-key messages to 1 partition.
   - 4 consumers in a group (1 per partition) drain with 20ms/msg work:
     1 consumer saturates and builds lag while 3 sibling consumers sit idle.
2. Phase B (Key Salting Mitigation):
   - Produces 200 messages with salted keys (`tenant:vip-1#<salt>`).
   - Traffic spreads across all 4 partitions, engaging all 4 consumers in
     parallel and cutting total drain time by ~3x.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from collections import Counter

from confluent_kafka import Consumer, Producer
from confluent_kafka.admin import AdminClient, NewTopic
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

BOOTSTRAP_SERVERS = "127.0.0.1:9092"
TOTAL_MESSAGES = 200
WORK_PER_MSG_S = 0.02

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def create_topic(admin: AdminClient, topic: str, partitions: int = 4) -> None:
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


def run_workload(admin: AdminClient, label: str, hot_key_mode: bool) -> dict:
    console.print(f"\n[bold blue][*] {label}[/]")
    topic = f"exp03-{'skew' if hot_key_mode else 'salt'}-{uuid.uuid4().hex[:8]}"
    group_id = f"exp03-grp-{uuid.uuid4().hex[:8]}"
    create_topic(admin, topic, partitions=4)

    # Start 4 consumers in the same group (1 per partition) BEFORE producing so no messages are polled during join
    consumers: list[Consumer] = []
    assigned_flags = [threading.Event() for _ in range(4)]
    consumer_counts = [0, 0, 0, 0]
    count_lock = threading.Lock()
    stop_event = threading.Event()

    for idx in range(4):
        c = Consumer(
            {
                "bootstrap.servers": BOOTSTRAP_SERVERS,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            }
        )

        def make_on_assign(ev: threading.Event):
            def on_assign(_c, parts) -> None:
                if parts:
                    ev.set()

            return on_assign

        c.subscribe([topic], on_assign=make_on_assign(assigned_flags[idx]))
        consumers.append(c)

    # Wait for all 4 consumers to receive a partition assignment
    assign_deadline = time.monotonic() + 15.0
    while not all(ev.is_set() for ev in assigned_flags) and time.monotonic() < assign_deadline:
        for c in consumers:
            c.poll(0.05)
    verify_phase(all(ev.is_set() for ev in assigned_flags), label, "Not all 4 consumers received partition assignments")

    # Produce 200 messages and record partition distribution
    producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
    part_counts: Counter[int] = Counter()

    def on_delivery(err, msg) -> None:
        if err is None:
            part_counts[msg.partition()] += 1

    for i in range(TOTAL_MESSAGES):
        if hot_key_mode:
            key = "tenant:vip-1" if i < 180 else f"tenant:other-{i}"
        else:
            key = f"tenant:vip-1#{i}"
        producer.produce(topic, key=key.encode(), value=f"event-{i:03d}".encode(), callback=on_delivery)

    remaining = producer.flush(timeout=10.0)
    verify_phase(remaining == 0, label, f"Failed to flush {remaining} messages")
    plain_console.print(f"    Partition distribution: {dict(sorted(part_counts.items()))}")

    def worker(idx: int, consumer: Consumer) -> None:
        while not stop_event.is_set():
            msg = consumer.poll(0.1)
            if msg is None or msg.error():
                continue
            time.sleep(WORK_PER_MSG_S)
            with count_lock:
                consumer_counts[idx] += 1

    start_t = time.monotonic()
    threads = [threading.Thread(target=worker, args=(i, consumers[i]), daemon=True) for i in range(4)]
    for t in threads:
        t.start()

    drain_deadline = time.monotonic() + 20.0
    while time.monotonic() < drain_deadline:
        with count_lock:
            done = sum(consumer_counts)
        if done >= TOTAL_MESSAGES:
            break
        time.sleep(0.05)

    elapsed_s = time.monotonic() - start_t
    stop_event.set()
    for t in threads:
        t.join(timeout=5.0)
    for c in consumers:
        c.close()

    with count_lock:
        total_done = sum(consumer_counts)

    verify_phase(total_done == TOTAL_MESSAGES, label, f"Expected {TOTAL_MESSAGES} drained, got {total_done}")
    plain_console.print(f"    Consumer counts: {consumer_counts}, Drain time: {elapsed_s:.2f}s")

    admin.delete_topics([topic], operation_timeout=5.0)
    return {
        "part_counts": dict(sorted(part_counts.items())),
        "max_part": max(part_counts.values()),
        "consumer_counts": consumer_counts,
        "elapsed_s": elapsed_s,
    }


def main() -> None:
    console.print("\n" + "=" * 72, style="bold cyan")
    console.print("[bold green]Experiment 03: Hot-Key Partition Skew & Consumer Group Lag[/]")
    console.print("=" * 72, style="bold cyan")

    admin = AdminClient({"bootstrap.servers": BOOTSTRAP_SERVERS})
    skew_res = run_workload(admin, "Phase A: Hot-Key Skew (90% tenant:vip-1)", hot_key_mode=True)
    salt_res = run_workload(admin, "Phase B: Salted Keys (tenant:vip-1#<salt>)", hot_key_mode=False)

    verify_phase(
        skew_res["max_part"] >= 180,
        "Phase A: Skew Assertion",
        f"Expected hot partition >= 180 messages, got {skew_res['max_part']}",
    )
    idle_consumers = sum(1 for c in skew_res["consumer_counts"] if c <= 15)
    verify_phase(
        idle_consumers >= 2,
        "Phase A: Idle Siblings",
        f"Expected >= 2 near-idle consumers, got counts {skew_res['consumer_counts']}",
    )
    verify_phase(
        salt_res["max_part"] <= 90,
        "Phase B: Balanced Distribution",
        f"Expected max partition <= 90 with salting, got {salt_res['max_part']}",
    )
    verify_phase(
        salt_res["elapsed_s"] < skew_res["elapsed_s"] * 0.65,
        "Phase B: Parallel Speedup",
        f"Expected salted drain ({salt_res['elapsed_s']:.2f}s) significantly faster than skew ({skew_res['elapsed_s']:.2f}s)",
    )

    table = Table(title="Partition Skew vs. Salted Key Parallelism", box=box.ROUNDED, header_style="bold cyan")
    table.add_column("Strategy")
    table.add_column("Max Partition Load", justify="right")
    table.add_column("Consumer Counts (4 workers)")
    table.add_column("Drain Time", justify="right")
    table.add_column("Speedup", justify="right")

    speedup = skew_res["elapsed_s"] / max(salt_res["elapsed_s"], 0.001)
    table.add_row(
        "Phase A: Hot Key (tenant:vip-1)",
        f"{skew_res['max_part']}/{TOTAL_MESSAGES} ({skew_res['max_part'] / TOTAL_MESSAGES:.0%})",
        str(skew_res["consumer_counts"]),
        f"{skew_res['elapsed_s']:.2f}s",
        "1.00x",
    )
    table.add_row(
        "Phase B: Salted Key (#salt)",
        f"{salt_res['max_part']}/{TOTAL_MESSAGES} ({salt_res['max_part'] / TOTAL_MESSAGES:.0%})",
        str(salt_res["consumer_counts"]),
        f"{salt_res['elapsed_s']:.2f}s",
        f"[bold green]{speedup:.2f}x[/]",
    )
    console.print("\n", table)
    console.print("\n[bold green][✔] Experiment 03 completed and verified.[/]\n")


if __name__ == "__main__":
    main()

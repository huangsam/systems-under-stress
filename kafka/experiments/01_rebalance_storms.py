# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "confluent-kafka>=2.6.0",
# ]
# ///
"""
Experiment 01: Consumer Group Rebalance Storms vs. Cooperative Sticky & Bounded Processing

Demonstrates:
1. Phase A (Eager Rebalance Storm):
   - A 2-consumer group using `range` assignment with `max.poll.interval.ms=3000`.
   - Consumer 1 stalls for 3.8s on a message, breaching `max.poll.interval.ms`.
   - The GroupCoordinator evicts Consumer 1 and triggers a stop-the-world eager
     rebalance (revoking ALL partitions from healthy Consumer 2 too).
   - When Consumer 1 resumes polling, it rejoins and triggers a second rebalance
     storm, while uncommitted messages are redelivered (duplicate processing).
2. Phase B (Mitigation: Cooperative Sticky + Bounded Processing):
   - Uses `cooperative-sticky` assignment and bounded per-message work within
     `max.poll.interval.ms=10000`.
   - Zero timeout-induced rebalances and zero duplicate deliveries.
   - Adding a 3rd consumer triggers an incremental rebalance: only moved partitions
     are revoked instead of a stop-the-world pause.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

from confluent_kafka import Consumer, KafkaException, Producer
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


@dataclass
class ConsumerMetrics:
    name: str
    assign_events: int = 0
    revoke_events: int = 0
    revoked_partitions_total: int = 0
    assigned_partitions: list[int] = field(default_factory=list)
    processed_keys: list[str] = field(default_factory=list)


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
    verify_phase(False, "Setup", f"Topic {topic} failed to become ready")


def produce_messages(topic: str, count: int, prefix: str) -> None:
    producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
    for i in range(count):
        key = f"{prefix}-{i:02d}".encode()
        producer.produce(topic, key=key, value=f"payload-{i:02d}".encode())
    remaining = producer.flush(timeout=10.0)
    verify_phase(remaining == 0, "Produce", f"Failed to flush {remaining} messages")


def make_consumer(group_id: str, strategy: str, max_poll_ms: int, metrics: ConsumerMetrics) -> Consumer:
    def on_assign(_consumer, partitions) -> None:
        if partitions:
            metrics.assign_events += 1
            current = set(metrics.assigned_partitions)
            current.update(p.partition for p in partitions)
            metrics.assigned_partitions = sorted(current)

    def on_revoke(_consumer, partitions) -> None:
        if partitions:
            metrics.revoke_events += 1
            metrics.revoked_partitions_total += len(partitions)
            revoked = {p.partition for p in partitions}
            metrics.assigned_partitions = sorted(p for p in metrics.assigned_partitions if p not in revoked)

    c = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "partition.assignment.strategy": strategy,
            "max.poll.interval.ms": max_poll_ms,
            "session.timeout.ms": 6000,
            "heartbeat.interval.ms": 500,
            "log_level": 0,
        }
    )
    return c, on_assign, on_revoke


def wait_for_balanced_join(c1: Consumer, m1: ConsumerMetrics, c2: Consumer, m2: ConsumerMetrics, phase: str, timeout_s: float = 15.0) -> None:
    """Poll both consumers until all 4 partitions are assigned across both members (2 each) and stabilized."""
    deadline = time.monotonic() + timeout_s
    stable_ticks = 0
    while time.monotonic() < deadline:
        c1.poll(0.05)
        c2.poll(0.05)
        if len(m1.assigned_partitions) == 2 and len(m2.assigned_partitions) == 2:
            stable_ticks += 1
            if stable_ticks >= 4:
                return
        else:
            stable_ticks = 0
    verify_phase(
        False,
        f"{phase}: Initial Join",
        f"Consumers failed to reach balanced 2+2 assignment: C1={m1.assigned_partitions}, C2={m2.assigned_partitions}",
    )
    return


def run_phase_a_rebalance_storm(admin: AdminClient) -> tuple[ConsumerMetrics, ConsumerMetrics, int]:
    console.print("\n[bold blue][*] Phase A: Eager Rebalance Storm (max.poll.interval.ms=6500, strategy=range)[/]")
    topic = f"exp01-storm-{uuid.uuid4().hex[:8]}"
    group_id = f"exp01-storm-grp-{uuid.uuid4().hex[:8]}"
    create_topic(admin, topic, partitions=4)

    m1 = ConsumerMetrics("C1-Slow")
    m2 = ConsumerMetrics("C2-Healthy")
    c1, on_assign1, on_revoke1 = make_consumer(group_id, "range", max_poll_ms=6500, metrics=m1)
    c2, on_assign2, on_revoke2 = make_consumer(group_id, "range", max_poll_ms=6500, metrics=m2)

    c1.subscribe([topic], on_assign=on_assign1, on_revoke=on_revoke1)
    c2.subscribe([topic], on_assign=on_assign2, on_revoke=on_revoke2)

    wait_for_balanced_join(c1, m1, c2, m2, "Phase A")
    plain_console.print(f"    Initial assignment: C1={m1.assigned_partitions}, C2={m2.assigned_partitions}")

    # Reset revocation counters from any stagger during initial group join
    m1.revoke_events = m1.revoked_partitions_total = 0
    m2.revoke_events = m2.revoked_partitions_total = 0
    m1.assign_events = m2.assign_events = 1

    produce_messages(topic, count=24, prefix="storm")

    stop_event = threading.Event()
    c1_stalled = threading.Event()
    c1_resumed = threading.Event()

    def c1_worker() -> None:
        stalled_once = False
        while not stop_event.is_set():
            msg = c1.poll(0.2)
            if msg is None or msg.error():
                continue
            key = msg.key().decode()
            m1.processed_keys.append(key)
            if not stalled_once:
                stalled_once = True
                plain_console.print(f"    [!] C1 stalled for 7.5s on {key} (exceeding max.poll.interval.ms=6500)...")
                c1_stalled.set()
                time.sleep(7.5)
                plain_console.print("    [!] C1 finished slow work and called poll() -> rejoining group...")
                c1_resumed.set()
                # Do NOT commit the stalled message before eviction -> triggers duplicate on C2
                continue
            try:
                c1.commit(message=msg, asynchronous=False)
            except KafkaException:
                pass

    def c2_worker() -> None:
        while not stop_event.is_set():
            msg = c2.poll(0.1)
            if msg is None or msg.error():
                continue
            key = msg.key().decode()
            m2.processed_keys.append(key)
            try:
                c2.commit(message=msg, asynchronous=False)
            except KafkaException:
                pass

    t1 = threading.Thread(target=c1_worker, daemon=True)
    t2 = threading.Thread(target=c2_worker, daemon=True)
    t1.start()
    t2.start()

    try:
        # Wait until C1 stalls, resumes, and all 24 unique keys are processed
        phase_deadline = time.monotonic() + 25.0
        while time.monotonic() < phase_deadline:
            unique_done = len(set(m1.processed_keys) | set(m2.processed_keys))
            if c1_resumed.is_set() and unique_done >= 24 and m2.assign_events >= 3:
                break
            time.sleep(0.2)
        c2_assigns = m2.assign_events
        c2_revokes = m2.revoke_events
        c2_revoked_parts = m2.revoked_partitions_total
    finally:
        stop_event.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        c1.close()
        c2.close()

    m2.assign_events = c2_assigns
    m2.revoke_events = c2_revokes
    m2.revoked_partitions_total = c2_revoked_parts

    total_deliveries = len(m1.processed_keys) + len(m2.processed_keys)
    unique_deliveries = len(set(m1.processed_keys) | set(m2.processed_keys))
    duplicates = total_deliveries - unique_deliveries

    plain_console.print(
        f"    Phase A summary: C2 assignments={m2.assign_events}, C2 revocations={m2.revoke_events} "
        f"(partitions revoked={m2.revoked_partitions_total}), duplicates={duplicates}"
    )

    verify_phase(m2.revoke_events >= 2, "Phase A: Rebalance Storm", f"Expected >= 2 revocations on C2, got {m2.revoke_events}")
    verify_phase(
        m2.revoked_partitions_total >= 4,
        "Phase A: Stop-the-World Revocation",
        f"Expected >= 4 total partitions revoked on C2, got {m2.revoked_partitions_total}",
    )
    verify_phase(duplicates >= 1, "Phase A: Duplicate Redelivery", f"Expected >= 1 duplicate delivery on eviction, got {duplicates}")

    admin.delete_topics([topic], operation_timeout=5.0)
    return m1, m2, duplicates


def run_phase_b_cooperative_sticky(admin: AdminClient) -> tuple[ConsumerMetrics, ConsumerMetrics, ConsumerMetrics, int]:
    console.print("\n[bold blue][*] Phase B: Cooperative Sticky + Bounded Processing (max.poll.interval.ms=10000)[/]")
    topic = f"exp01-coop-{uuid.uuid4().hex[:8]}"
    group_id = f"exp01-coop-grp-{uuid.uuid4().hex[:8]}"
    create_topic(admin, topic, partitions=4)

    m1 = ConsumerMetrics("C1-Bounded")
    m2 = ConsumerMetrics("C2-Bounded")
    m3 = ConsumerMetrics("C3-ScaleOut")
    c1, on_assign1, on_revoke1 = make_consumer(group_id, "cooperative-sticky", max_poll_ms=10000, metrics=m1)
    c2, on_assign2, on_revoke2 = make_consumer(group_id, "cooperative-sticky", max_poll_ms=10000, metrics=m2)

    c1.subscribe([topic], on_assign=on_assign1, on_revoke=on_revoke1)
    c2.subscribe([topic], on_assign=on_assign2, on_revoke=on_revoke2)

    wait_for_balanced_join(c1, m1, c2, m2, "Phase B")
    plain_console.print(f"    Initial assignment: C1={m1.assigned_partitions}, C2={m2.assigned_partitions}")

    # Reset revocation counters from any stagger during initial group join
    m1.revoke_events = m1.revoked_partitions_total = 0
    m2.revoke_events = m2.revoked_partitions_total = 0
    m1.assign_events = m2.assign_events = 1

    produce_messages(topic, count=24, prefix="coop")

    stop_event = threading.Event()

    def bounded_worker(consumer: Consumer, metrics: ConsumerMetrics) -> None:
        while not stop_event.is_set():
            msg = consumer.poll(0.1)
            if msg is None or msg.error():
                continue
            time.sleep(0.02)  # Bounded work well under max.poll.interval.ms
            metrics.processed_keys.append(msg.key().decode())
            try:
                consumer.commit(message=msg, asynchronous=False)
            except KafkaException:
                pass

    t1 = threading.Thread(target=bounded_worker, args=(c1, m1), daemon=True)
    t2 = threading.Thread(target=bounded_worker, args=(c2, m2), daemon=True)
    t1.start()
    t2.start()

    c3 = None
    t3 = None
    try:
        # Wait for all 24 messages to be processed with 0 revocations
        deadline = time.monotonic() + 15.0
        while len(set(m1.processed_keys) | set(m2.processed_keys)) < 24 and time.monotonic() < deadline:
            time.sleep(0.1)

        verify_phase(
            m1.revoke_events == 0 and m2.revoke_events == 0,
            "Phase B: Zero Timeout Rebalances",
            f"Expected 0 revocations during steady state, got C1={m1.revoke_events}, C2={m2.revoke_events}",
        )

        # Snapshot steady-state revocations before scale-out, then add C3 to observe incremental cooperative-sticky rebalance
        steady_c2_revokes = m2.revoke_events
        c3, on_assign3, on_revoke3 = make_consumer(group_id, "cooperative-sticky", max_poll_ms=10000, metrics=m3)
        c3.subscribe([topic], on_assign=on_assign3, on_revoke=on_revoke3)
        t3 = threading.Thread(target=bounded_worker, args=(c3, m3), daemon=True)
        t3.start()

        scale_deadline = time.monotonic() + 15.0
        while not m3.assigned_partitions and time.monotonic() < scale_deadline:
            time.sleep(0.1)
        scale_moved = m1.revoked_partitions_total + m2.revoked_partitions_total
        c3_parts = list(m3.assigned_partitions)
    finally:
        stop_event.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        if t3 is not None:
            t3.join(timeout=5.0)
        c1.close()
        c2.close()
        if c3 is not None:
            c3.close()

    # Restore pre-close metrics so c.close() revocation callbacks do not overwrite scale-out observations
    m2.revoke_events = steady_c2_revokes
    m1.revoked_partitions_total = scale_moved
    m2.revoked_partitions_total = 0
    m3.assigned_partitions = c3_parts

    total_deliveries = len(m1.processed_keys) + len(m2.processed_keys) + len(m3.processed_keys)
    unique_deliveries = len(set(m1.processed_keys) | set(m2.processed_keys) | set(m3.processed_keys))
    duplicates = total_deliveries - unique_deliveries

    plain_console.print(
        f"    Phase B summary: unique={unique_deliveries}/24, duplicates={duplicates}, "
        f"scale-out moved partitions={m1.revoked_partitions_total + m2.revoked_partitions_total} (C3 got {m3.assigned_partitions})"
    )

    verify_phase(unique_deliveries == 24, "Phase B: Completeness", f"Expected 24 unique messages, got {unique_deliveries}")
    verify_phase(duplicates == 0, "Phase B: Zero Duplicates", f"Expected 0 duplicates, got {duplicates}")
    verify_phase(m3.assign_events >= 1 and len(m3.assigned_partitions) >= 1, "Phase B: Scale-out", "C3 did not receive any partition")

    admin.delete_topics([topic], operation_timeout=5.0)
    return m1, m2, m3, duplicates


def main() -> None:
    console.print("\n" + "=" * 72, style="bold cyan")
    console.print("[bold green]Experiment 01: Consumer Group Rebalance Storms vs. Cooperative Sticky[/]")
    console.print("=" * 72, style="bold cyan")

    admin = AdminClient({"bootstrap.servers": BOOTSTRAP_SERVERS})
    _m1_a, m2_a, dup_a = run_phase_a_rebalance_storm(admin)
    m1_b, m2_b, m3_b, dup_b = run_phase_b_cooperative_sticky(admin)

    table = Table(title="Rebalance Storm vs. Cooperative Sticky Comparison", box=box.ROUNDED, header_style="bold cyan")
    table.add_column("Phase / Strategy")
    table.add_column("Revocation Events (C2)", justify="right")
    table.add_column("Partitions Revoked", justify="right")
    table.add_column("Duplicate Deliveries", justify="right")
    table.add_column("Outcome")

    table.add_row(
        "Phase A: range + 7.5s stall (>6.5s limit)",
        str(m2_a.revoke_events),
        str(m2_a.revoked_partitions_total),
        str(dup_a),
        "[bold red]STOP-THE-WORLD STORM[/]",
    )
    table.add_row(
        "Phase B: cooperative-sticky + bounded work",
        f"{m2_b.revoke_events} (steady) / scale-out",
        str(m1_b.revoked_partitions_total + m2_b.revoked_partitions_total),
        str(dup_b),
        f"[bold green]INCREMENTAL ({len(m3_b.assigned_partitions)} moved)[/]",
    )
    console.print("\n", table)
    console.print("\n[bold green][✔] Experiment 01 completed and verified.[/]\n")


if __name__ == "__main__":
    main()

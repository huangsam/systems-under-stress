# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.7.0",
#     "confluent-kafka>=2.6.0",
# ]
# ///
"""
Experiment 04: acks=all + min.insync.replicas vs. acks=1 Leader Failover Data Loss

Demonstrates on a 3-node KRaft cluster (kafka-1, kafka-2, kafka-3):
1. Phase A (acks=all + min.insync.replicas=2 -> NotEnoughReplicasException):
   - Creates a topic with replication_factor=2, min.insync.replicas=2.
   - Baseline writes with acks="all" succeed while both replicas are in-sync.
   - SIGKILLs one replica broker (2/3 KRaft controllers remain, preserving quorum).
   - ISR shrinks to 1 (< min.insync.replicas=2); subsequent acks="all" writes are
     rejected with Broker: Not enough in-sync replicas (NOT_ENOUGH_REPLICAS).
   - Restarting the broker restores ISR=2 and resumes acks="all" writes with 0 loss.
2. Phase B (acks=1 + Leader SIGKILL -> Acknowledged Data Loss):
   - Creates a topic with replication_factor=3, min.insync.replicas=1,
     unclean.leader.election.enable=true.
   - Pauses both followers, produces 15 messages with acks="1" (all acknowledged
     by the leader's local log), then SIGKILLs the leader before replication.
   - Unpauses followers: a stale follower wins leader election, and all 15
     acknowledged messages are permanently lost.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

from confluent_kafka import Consumer, KafkaError, Producer
from confluent_kafka.admin import AdminClient, NewTopic
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

BOOTSTRAP_SERVERS = "127.0.0.1:19092,127.0.0.1:19093,127.0.0.1:19094"
LAB_DIR = Path(__file__).resolve().parent.parent

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)


def verify_phase(condition: bool, phase: str, detail: str) -> None:
    """Validate a phase invariant, exiting non-zero if it fails."""
    if not condition:
        console.print(f"[bold red][✗] Phase validation failed ({phase}):[/] {escape(detail)}")
        sys.exit(1)


def docker_compose(*args: str) -> None:
    """Run a docker compose command scoped to the multi-broker profile."""
    subprocess.run(
        ["docker", "compose", "--profile", "multi-broker", *args],
        cwd=LAB_DIR,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def service_for_broker(broker_id: int) -> str:
    return f"kafka-{broker_id}"


def wait_for_cluster_brokers(admin: AdminClient, min_brokers: int = 3, timeout_s: float = 35.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            meta = admin.list_topics(timeout=2.0)
            if len(meta.brokers) >= min_brokers:
                return
        except Exception:
            pass
        time.sleep(0.5)
    verify_phase(False, "Cluster Readiness", f"Expected {min_brokers} brokers within {timeout_s}s")


def create_topic_and_wait_isr(
    admin: AdminClient,
    topic: str,
    replication_factor: int,
    config: dict[str, str],
    timeout_s: float = 20.0,
) -> tuple[int, list[int], list[int]]:
    futures = admin.create_topics(
        [
            NewTopic(
                topic,
                num_partitions=1,
                replication_factor=replication_factor,
                config=config,
            )
        ]
    )
    for fut in futures.values():
        fut.result(timeout=10.0)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        meta = admin.list_topics(topic=topic, timeout=2.0)
        tmeta = meta.topics.get(topic)
        if tmeta and tmeta.error is None and 0 in tmeta.partitions:
            pmeta = tmeta.partitions[0]
            if pmeta.leader >= 0 and len(pmeta.isrs) >= replication_factor:
                return pmeta.leader, list(pmeta.replicas), list(pmeta.isrs)
        time.sleep(0.3)
    verify_phase(False, "Topic Setup", f"Topic {topic} did not reach ISR={replication_factor} within {timeout_s}s")
    return -1, [], []


def wait_for_isr_count(admin: AdminClient, topic: str, expected_isr: int, timeout_s: float = 25.0) -> tuple[int, list[int]]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            meta = admin.list_topics(topic=topic, timeout=2.0)
            tmeta = meta.topics.get(topic)
            if tmeta and tmeta.error is None and 0 in tmeta.partitions:
                pmeta = tmeta.partitions[0]
                if pmeta.leader >= 0 and len(pmeta.isrs) == expected_isr:
                    return pmeta.leader, list(pmeta.isrs)
        except Exception:
            pass
        time.sleep(0.3)
    verify_phase(False, "ISR Wait", f"Topic {topic} did not reach ISR={expected_isr} within {timeout_s}s")
    return -1, []


def produce_batch(topic: str, count: int, prefix: str, acks: str, timeout_ms: int = 4000) -> tuple[int, list[tuple[int, str]]]:
    producer = Producer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "acks": acks,
            "message.timeout.ms": timeout_ms,
            "retries": 0,
        }
    )
    acked = 0
    errors: list[tuple[int, str]] = []

    def on_delivery(err, _msg) -> None:
        nonlocal acked
        if err is not None:
            errors.append((err.code(), err.str()))
        else:
            acked += 1

    for i in range(count):
        producer.produce(topic, value=f"{prefix}-{i:02d}".encode(), callback=on_delivery)
    producer.flush(timeout=max(timeout_ms / 1000.0 + 3.0, 5.0))
    return acked, errors


def consume_all(topic: str, timeout_s: float = 8.0) -> list[str]:
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": f"exp04-read-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([topic])
    values: list[str] = []
    idle = 0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and idle < 6:
        msg = consumer.poll(0.4)
        if msg is None:
            idle += 1
            continue
        if msg.error():
            continue
        idle = 0
        values.append(msg.value().decode())
    consumer.close()
    return values


def run_phase_a_min_isr(admin: AdminClient) -> tuple[int, str]:
    console.print("\n[bold blue][*] Phase A: acks=all + min.insync.replicas=2 (NotEnoughReplicasException)[/]")
    topic = f"exp04-strict-{uuid.uuid4().hex[:8]}"
    leader, replicas, isrs = create_topic_and_wait_isr(
        admin,
        topic,
        replication_factor=2,
        config={"min.insync.replicas": "2"},
    )
    follower = next(r for r in replicas if r != leader)
    plain_console.print(f"    Topic '{topic}': leader={leader}, replicas={replicas}, isr={isrs}")

    acked_ok, errs_ok = produce_batch(topic, count=10, prefix="strict-ok", acks="all")
    verify_phase(acked_ok == 10 and not errs_ok, "Phase A: Baseline", f"Expected 10 acked, got {acked_ok}, errors={errs_ok}")
    plain_console.print(f"    Baseline acks=all (ISR=2): {acked_ok}/10 acknowledged.")

    # Stop the follower replica (SIGKILL without auto-restart); 2/3 KRaft nodes remain alive so quorum stays intact
    follower_svc = service_for_broker(follower)
    plain_console.print(f"    [!] Stopping follower '{follower_svc}' (stop -t 0 / SIGKILL) to shrink ISR below min.insync.replicas=2...")
    docker_compose("stop", "-t", "0", follower_svc)

    try:
        _, shrunk_isr = wait_for_isr_count(admin, topic, expected_isr=1)
        plain_console.print(f"    ISR shrunk to {shrunk_isr} (< min.insync.replicas=2)")
        time.sleep(1.5)  # Allow leader MetadataImage to apply ISR shrink after controller update

        acked_fail, errs_fail = produce_batch(topic, count=5, prefix="strict-rej", acks="all", timeout_ms=4000)
        not_enough = [msg for code, msg in errs_fail if code == KafkaError.NOT_ENOUGH_REPLICAS or "not enough in-sync" in msg.lower()]
        if not not_enough and acked_fail == 0:
            time.sleep(2.0)
            acked_fail, errs_fail = produce_batch(topic, count=5, prefix="strict-rej", acks="all", timeout_ms=4000)
            not_enough = [msg for code, msg in errs_fail if code == KafkaError.NOT_ENOUGH_REPLICAS or "not enough in-sync" in msg.lower()]
        plain_console.print(f"    Write attempt under ISR=1: acked={acked_fail}/5, errors={len(errs_fail)}")
        if errs_fail:
            plain_console.print(f"    Captured failure signature: {errs_fail[0][1]}")

        verify_phase(acked_fail == 0, "Phase A: Reject Under-Replicated", f"Expected 0 acked under ISR=1, got {acked_fail}")
        verify_phase(len(not_enough) >= 1, "Phase A: NotEnoughReplicas", f"Expected NOT_ENOUGH_REPLICAS error, got {errs_fail}")
    finally:
        plain_console.print(f"    [+] Restarting '{follower_svc}' and waiting for ISR=2 recovery...")
        docker_compose("start", follower_svc)
        wait_for_cluster_brokers(admin, min_brokers=3)

    _, recovered_isr = wait_for_isr_count(admin, topic, expected_isr=2)
    acked_rec, errs_rec = produce_batch(topic, count=5, prefix="strict-rec", acks="all")
    verify_phase(acked_rec == 5 and not errs_rec, "Phase A: Post-Recovery", f"Expected 5 acked after recovery, got {acked_rec}")

    stored = consume_all(topic)
    verify_phase(len(stored) == 15, "Phase A: Zero Data Loss", f"Expected 15 durable records, found {len(stored)}")
    plain_console.print(f"    Recovered ISR={recovered_isr}; all {len(stored)}/15 acknowledged messages intact.")

    admin.delete_topics([topic], operation_timeout=5.0)
    return len(stored), errs_fail[0][1]


def run_phase_b_acks1_loss(admin: AdminClient) -> tuple[int, int]:
    console.print("\n[bold blue][*] Phase B: acks=1 + Abrupt Leader SIGKILL (Acknowledged Data Loss)[/]")
    topic = f"exp04-acks1-{uuid.uuid4().hex[:8]}"
    leader, replicas, isrs = create_topic_and_wait_isr(
        admin,
        topic,
        replication_factor=3,
        config={"min.insync.replicas": "1", "unclean.leader.election.enable": "true"},
    )
    followers = [r for r in replicas if r != leader]
    plain_console.print(f"    Topic '{topic}': leader={leader}, followers={followers}, isr={isrs}")

    # Baseline 5 messages replicated to all 3 nodes
    acked_base, errs_base = produce_batch(topic, count=5, prefix="base", acks="all")
    verify_phase(acked_base == 5 and not errs_base, "Phase B: Baseline", f"Expected 5 baseline acks, got {acked_base}")

    follower_svcs = [service_for_broker(f) for f in followers]
    leader_svc = service_for_broker(leader)

    plain_console.print(f"    [!] Stopping followers {follower_svcs} so leader '{leader_svc}' cannot replicate...")
    docker_compose("stop", "-t", "0", *follower_svcs)
    followers_restarted = False
    leader_restarted = False

    try:
        # Produce 15 messages with acks=1: leader writes to its local log and ACKs immediately
        acked_unrep, errs_unrep = produce_batch(topic, count=15, prefix="acks1-lost", acks="1", timeout_ms=5000)
        verify_phase(acked_unrep == 15 and not errs_unrep, "Phase B: acks=1 Produce", f"Expected 15 acks=1, got {acked_unrep}, errs={errs_unrep}")
        plain_console.print(f"    Producer received {acked_unrep}/15 ACKs from leader '{leader_svc}' (unreplicated!).")

        plain_console.print(f"    [!] Stopping leader '{leader_svc}' immediately (stop -t 0 / SIGKILL) before replication...")
        docker_compose("stop", "-t", "0", leader_svc)

        plain_console.print(f"    [+] Starting followers {follower_svcs} -> failover to stale follower...")
        docker_compose("start", *follower_svcs)
        followers_restarted = True

        # Wait for one of the surviving followers to become leader
        deadline = time.monotonic() + 30.0
        new_leader = -1
        while time.monotonic() < deadline:
            try:
                meta = admin.list_topics(topic=topic, timeout=2.0)
                tmeta = meta.topics.get(topic)
                if tmeta and tmeta.error is None and 0 in tmeta.partitions:
                    cand = tmeta.partitions[0].leader
                    if cand >= 0 and cand != leader:
                        new_leader = cand
                        break
            except Exception:
                pass
            time.sleep(0.4)

        verify_phase(new_leader >= 0 and new_leader != leader, "Phase B: Failover", "Surviving follower did not take leadership")
        plain_console.print(f"    New leader elected: broker {new_leader} (old leader was {leader})")

        surviving = consume_all(topic)
        lost_count = 20 - len(surviving)
        plain_console.print(f"    [!] Surviving records after failover: {len(surviving)}/20 ({lost_count} acknowledged writes LOST!)")
        verify_phase(lost_count >= 15, "Phase B: Data Loss Verification", f"Expected >= 15 lost records, got {lost_count}")
    finally:
        if not followers_restarted:
            docker_compose("start", *follower_svcs)
        if not leader_restarted:
            plain_console.print(f"    [+] Restarting '{leader_svc}' to restore 3-node cluster...")
            docker_compose("start", leader_svc)
            wait_for_cluster_brokers(admin, min_brokers=3)

    admin.delete_topics([topic], operation_timeout=5.0)
    return acked_unrep, lost_count


def main() -> None:
    console.print("\n" + "=" * 72, style="bold cyan")
    console.print("[bold green]Experiment 04: acks=all + min.insync.replicas vs. acks=1 Failover Loss[/]")
    console.print("=" * 72, style="bold cyan")

    admin = AdminClient({"bootstrap.servers": BOOTSTRAP_SERVERS})
    wait_for_cluster_brokers(admin, min_brokers=3)

    durable_a, err_sig = run_phase_a_min_isr(admin)
    acked_b, lost_b = run_phase_b_acks1_loss(admin)

    table = Table(title="Durability vs. Availability Under Broker Failure", box=box.ROUNDED, header_style="bold cyan")
    table.add_column("Configuration")
    table.add_column("Failure Injected")
    table.add_column("Write Behavior")
    table.add_column("Acknowledged Loss", justify="right")

    table.add_row(
        "acks=all, min.insync.replicas=2",
        "Replica SIGKILL (ISR=1 < 2)",
        f"[bold yellow]REJECTED[/] ({escape(err_sig)})",
        f"[bold green]0/{durable_a} lost[/]",
    )
    table.add_row(
        "acks=1, unclean.leader.election=true",
        "Leader SIGKILL before replication",
        f"[bold red]ACCEPTED[/] ({acked_b}/{acked_b} acked)",
        f"[bold red]{lost_b} acked writes LOST[/]",
    )
    console.print("\n", table)
    console.print("\n[bold green][✔] Experiment 04 completed and verified.[/]\n")


if __name__ == "__main__":
    main()

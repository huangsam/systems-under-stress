# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0.1",
#     "rich>=13.7.0",
# ]
# ///
"""
Validates all docker-compose.yml files against repository safety invariants (AGENTS.md):
1. Mandatory resource limits: Every service must define deploy.resources.limits.cpus and memory.
2. Loopback-only port binding: All port mappings must explicitly bind to 127.0.0.1.
3. Explicit healthchecks: Every service must define a deterministic healthcheck.
4. No sensitive host mounts: Volume mounts must be named volumes or relative paths within the lab folder.
5. Isolated named volumes: Stateful volumes must not be shared across distinct profiles.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rich import box
from rich.console import Console
from rich.table import Table

console = Console()
plain_console = Console(markup=False, highlight=False, soft_wrap=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = sorted(REPO_ROOT.glob("*/docker-compose.yml"))
SENSITIVE_PREFIXES = ("/", "~", "/var/run/docker.sock")


@dataclass
class AuditResult:
    lab: str
    service_count: int = 0
    errors: list[str] = field(default_factory=list)


def audit_compose_file(path: Path) -> AuditResult:
    lab_name = path.parent.name
    result = AuditResult(lab=lab_name)

    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        result.errors.append(f"Failed to parse YAML: {exc}")
        return result

    services = data.get("services", {})
    result.service_count = len(services)
    if not services:
        result.errors.append("No services defined")
        return result

    named_volumes = set(data.get("volumes", {}).keys()) if data.get("volumes") else set()
    vol_to_profiles: dict[str, set[tuple[str, ...]]] = defaultdict(set)

    for sname, sdef in services.items():
        if not isinstance(sdef, dict):
            continue

        # Invariant 1: Mandatory resource limits
        deploy = sdef.get("deploy", {})
        limits = deploy.get("resources", {}).get("limits", {})
        if not limits.get("cpus") or not limits.get("memory"):
            result.errors.append(f"[{sname}] Missing deploy.resources.limits (cpus & memory required)")

        # Invariant 2: Loopback-only port binding
        ports = sdef.get("ports", [])
        for p in ports:
            if isinstance(p, str):
                if not p.startswith("127.0.0.1:"):
                    result.errors.append(f"[{sname}] Port '{p}' must explicitly bind to 127.0.0.1")
            elif isinstance(p, dict):
                host_ip = p.get("host_ip", "")
                if host_ip != "127.0.0.1":
                    result.errors.append(f"[{sname}] Port host_ip '{host_ip}' must be 127.0.0.1")

        # Invariant 3: Deterministic readiness healthcheck
        hc = sdef.get("healthcheck", {})
        if not hc.get("test"):
            result.errors.append(f"[{sname}] Missing healthcheck.test")

        # Invariant 4 & 5: Volume safety and profile isolation
        profs = tuple(sorted(sdef.get("profiles", ["default"])))
        for v in sdef.get("volumes", []):
            vol_target = v.split(":")[0] if isinstance(v, str) else v.get("source", "")
            if vol_target.startswith(SENSITIVE_PREFIXES):
                result.errors.append(f"[{sname}] Sensitive host mount '{vol_target}' is forbidden")
            elif not vol_target.startswith("."):
                # Named volume
                if vol_target not in named_volumes:
                    result.errors.append(f"[{sname}] Mounts undefined named volume '{vol_target}'")
                vol_to_profiles[vol_target].add(profs)

    # Invariant 5: Verify no named volumes shared across different profile sets
    for vol, prof_sets in vol_to_profiles.items():
        if len(prof_sets) > 1:
            result.errors.append(f"Named volume '{vol}' shared across distinct profiles: {[list(p) for p in prof_sets]}")

    return result


def main() -> int:
    if not COMPOSE_FILES:
        console.print("[bold red]ERROR: No docker-compose.yml files found to audit.[/]")
        return 1

    results = [audit_compose_file(cf) for cf in COMPOSE_FILES]
    total_services = sum(r.service_count for r in results)
    total_errors = sum(len(r.errors) for r in results)

    table = Table(
        title="Docker Compose Safety & Host Invariant Audit",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("Lab", style="bold", no_wrap=True)
    table.add_column("Svcs", justify="center")
    table.add_column("Limits", justify="center")
    table.add_column("Loopback", justify="center")
    table.add_column("Health", justify="center")
    table.add_column("Volumes", justify="center")
    table.add_column("Status", justify="center")

    for r in results:
        status = "[bold green]PASS[/]" if not r.errors else "[bold red]FAIL[/]"
        has_limits_err = any("limits" in e.lower() for e in r.errors)
        has_port_err = any("port" in e.lower() for e in r.errors)
        has_hc_err = any("healthcheck" in e.lower() for e in r.errors)
        has_vol_err = any("volume" in e.lower() or "mount" in e.lower() for e in r.errors)

        c_lim = "[red]✖[/]" if has_limits_err else "[green]✔[/]"
        c_port = "[red]✖[/]" if has_port_err else "[green]✔[/]"
        c_hc = "[red]✖[/]" if has_hc_err else "[green]✔[/]"
        c_vol = "[red]✖[/]" if has_vol_err else "[green]✔[/]"

        table.add_row(
            r.lab,
            str(r.service_count),
            c_lim,
            c_port,
            c_hc,
            c_vol,
            status,
        )

    console.print()
    console.print(table)
    console.print()

    if total_errors > 0:
        console.print(f"[bold red]Found {total_errors} Invariant Violation(s):[/]\n")
        for r in results:
            if r.errors:
                console.print(f"[bold yellow]{r.lab}:[/]")
                for err in r.errors:
                    console.print(f"  [red]✖[/] {err}")
        return 1

    console.print(f"[bold green]✔ All {len(results)} Compose files and {total_services} services strictly comply with AGENTS.md safety invariants.[/]\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

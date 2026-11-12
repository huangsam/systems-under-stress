# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0.1",
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
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = sorted(REPO_ROOT.glob("*/docker-compose.yml"))

SENSITIVE_PREFIXES = ("/", "~", "/var/run/docker.sock")


def audit_compose_file(path: Path) -> list[str]:
    rel_path = path.relative_to(REPO_ROOT)
    errors: list[str] = []

    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        return [f"{rel_path}: Failed to parse YAML: {exc}"]

    services = data.get("services", {})
    if not services:
        return [f"{rel_path}: No services defined"]

    named_volumes = set(data.get("volumes", {}).keys()) if data.get("volumes") else set()
    vol_to_profiles: dict[str, set[tuple[str, ...]]] = defaultdict(set)

    for sname, sdef in services.items():
        if not isinstance(sdef, dict):
            continue

        # Invariant 1: Mandatory resource limits
        deploy = sdef.get("deploy", {})
        limits = deploy.get("resources", {}).get("limits", {})
        if not limits.get("cpus") or not limits.get("memory"):
            errors.append(f"{rel_path} [{sname}]: Missing deploy.resources.limits (both cpus and memory required)")

        # Invariant 2: Loopback-only port binding
        ports = sdef.get("ports", [])
        for p in ports:
            if isinstance(p, str):
                if not p.startswith("127.0.0.1:"):
                    errors.append(f"{rel_path} [{sname}]: Port '{p}' must explicitly bind to 127.0.0.1")
            elif isinstance(p, dict):
                host_ip = p.get("host_ip", "")
                if host_ip != "127.0.0.1":
                    errors.append(f"{rel_path} [{sname}]: Port host_ip '{host_ip}' must be 127.0.0.1")

        # Invariant 3: Deterministic readiness healthcheck
        hc = sdef.get("healthcheck", {})
        if not hc.get("test"):
            errors.append(f"{rel_path} [{sname}]: Missing healthcheck.test")

        # Invariant 4 & 5: Volume safety and profile isolation
        profs = tuple(sorted(sdef.get("profiles", ["default"])))
        for v in sdef.get("volumes", []):
            vol_target = v.split(":")[0] if isinstance(v, str) else v.get("source", "")
            if vol_target.startswith(SENSITIVE_PREFIXES):
                errors.append(f"{rel_path} [{sname}]: Sensitive host mount '{vol_target}' is forbidden")
            elif not vol_target.startswith("."):
                # Named volume
                if vol_target not in named_volumes:
                    errors.append(f"{rel_path} [{sname}]: Mounts undefined named volume '{vol_target}'")
                vol_to_profiles[vol_target].add(profs)

    # Invariant 5: Verify no named volumes shared across different profile sets
    for vol, prof_sets in vol_to_profiles.items():
        if len(prof_sets) > 1:
            errors.append(f"{rel_path}: Named volume '{vol}' is shared across distinct profiles: {[list(p) for p in prof_sets]}")

    return errors


def main() -> int:
    if not COMPOSE_FILES:
        print("ERROR: No docker-compose.yml files found to audit.", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    for cf in COMPOSE_FILES:
        errs = audit_compose_file(cf)
        all_errors.extend(errs)

    if all_errors:
        print("Safety & Compose Invariant Violations Found:", file=sys.stderr)
        for err in all_errors:
            print(f"  ❌ {err}", file=sys.stderr)
        return 1

    print(f"✅ Verified {len(COMPOSE_FILES)} compose files against all repository safety invariants.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

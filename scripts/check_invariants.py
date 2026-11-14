# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0.1",
#     "rich>=13.7.0",
# ]
# ///
"""
Validates all labs against repository safety, architecture, and documentation invariants (AGENTS.md):
1. Mandatory resource limits: Every service must define deploy.resources.limits.cpus and memory.
2. Loopback-only port binding: All port mappings must explicitly bind to 127.0.0.1.
3. Explicit healthchecks: Every service must define a deterministic healthcheck.
4. No sensitive host mounts: Volume mounts must be named volumes or relative paths within the lab folder.
5. Isolated named volumes: Stateful volumes must not be shared across distinct profiles.
6. Self-contained PEP 723 metadata on all experiments/[0-9][0-9]_*.py scripts, plus 00_crash_course.py presence.
7. Required Makefile targets (help, up, run, verify, down, clean, logs, plus soft-reset where applicable).
8. Canonical lab README.md structure, complete profile & script coverage, no hardcoded image tags, and accurate root README counts.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rich import box
from rich.console import Console
from rich.table import Table

console = Console()

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = sorted(REPO_ROOT.glob("*/docker-compose.yml"))
SENSITIVE_PREFIXES = ("/", "~", "/var/run/docker.sock")

STATELESS_NO_SOFT_RESET_LABS = {"linux", "vault"}
REQUIRED_MAKE_TARGETS = {"help", "up", "run", "verify", "down", "clean", "logs"}

REQUIRED_EXPERIMENT_SUBSECTIONS = [
    "### 1. Hypothesis & Objective",
    "### 2. Configuration & Limits",
    "### 3. How to Run",
    "### 4. Observed Metrics & Failure Signatures",
    "### 5. Production & Systems Takeaways",
]

HARDCODED_IMAGE_TAG_RE = re.compile(
    r"\b(?:redis|postgres|clickhouse/clickhouse-server|mongo|cassandra|hashicorp/vault|debian|ghcr\.io/shopify/toxiproxy):[0-9a-zA-Z._-]+"
)
EXPERIMENT_SCRIPT_RE = re.compile(r"^\d{2}_[a-z0-9_]+\.py$")


@dataclass
class AuditResult:
    lab: str
    service_count: int = 0
    compose_errors: list[str] = field(default_factory=list)
    script_errors: list[str] = field(default_factory=list)
    make_errors: list[str] = field(default_factory=list)
    doc_errors: list[str] = field(default_factory=list)

    @property
    def all_errors(self) -> list[str]:
        return self.compose_errors + self.script_errors + self.make_errors + self.doc_errors


def audit_lab(compose_path: Path) -> AuditResult:
    lab_dir = compose_path.parent
    lab_name = lab_dir.name
    result = AuditResult(lab=lab_name)

    try:
        with open(compose_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        result.compose_errors.append(f"Failed to parse YAML: {exc}")
        return result

    services = data.get("services", {})
    result.service_count = len(services)
    if not services:
        result.compose_errors.append("No services defined")
        return result

    named_volumes = set(data.get("volumes", {}).keys()) if data.get("volumes") else set()
    vol_to_profiles: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    declared_profiles: set[str] = set()

    for sname, sdef in services.items():
        if not isinstance(sdef, dict):
            continue

        # Invariant 1: Mandatory resource limits
        deploy = sdef.get("deploy", {})
        limits = deploy.get("resources", {}).get("limits", {})
        if not limits.get("cpus") or not limits.get("memory"):
            result.compose_errors.append(f"[{sname}] Missing deploy.resources.limits (cpus & memory required)")

        # Invariant 2: Loopback-only port binding
        ports = sdef.get("ports", [])
        for p in ports:
            if isinstance(p, str):
                if not p.startswith("127.0.0.1:"):
                    result.compose_errors.append(f"[{sname}] Port '{p}' must explicitly bind to 127.0.0.1")
            elif isinstance(p, dict):
                host_ip = p.get("host_ip", "")
                if host_ip != "127.0.0.1":
                    result.compose_errors.append(f"[{sname}] Port host_ip '{host_ip}' must be 127.0.0.1")

        # Invariant 3: Deterministic readiness healthcheck
        hc = sdef.get("healthcheck", {})
        if not hc.get("test"):
            result.compose_errors.append(f"[{sname}] Missing healthcheck.test")

        # Invariant 4 & 5: Volume safety and profile isolation
        raw_profs = sdef.get("profiles", ["default"])
        for p in raw_profs:
            if p != "default":
                declared_profiles.add(p)
        profs = tuple(sorted(raw_profs))
        for v in sdef.get("volumes", []):
            vol_target = v.split(":")[0] if isinstance(v, str) else v.get("source", "")
            if vol_target.startswith(SENSITIVE_PREFIXES):
                result.compose_errors.append(f"[{sname}] Sensitive host mount '{vol_target}' is forbidden")
            elif not vol_target.startswith("."):
                if vol_target not in named_volumes:
                    result.compose_errors.append(f"[{sname}] Mounts undefined named volume '{vol_target}'")
                vol_to_profiles[vol_target].add(profs)

    # Invariant 5: Verify no named volumes shared across different profile sets
    for vol, prof_sets in vol_to_profiles.items():
        if len(prof_sets) > 1:
            result.compose_errors.append(f"Named volume '{vol}' shared across distinct profiles: {[list(p) for p in prof_sets]}")

    # Invariant 6: Experiment scripts & PEP 723 metadata
    exp_dir = lab_dir / "experiments"
    exp_scripts: list[Path] = []
    if not exp_dir.is_dir():
        result.script_errors.append("Missing experiments/ directory")
    else:
        exp_scripts = sorted(p for p in exp_dir.glob("*.py") if EXPERIMENT_SCRIPT_RE.match(p.name))
        if not (exp_dir / "00_crash_course.py").is_file():
            result.script_errors.append("Missing experiments/00_crash_course.py")
        for sp in exp_scripts:
            text = sp.read_text()
            if "# /// script" not in text or "# requires-python" not in text:
                result.script_errors.append(f"[{sp.name}] Missing inline PEP 723 script metadata block")

    # Invariant 7: Makefile targets
    makefile_path = lab_dir / "Makefile"
    if not makefile_path.is_file():
        result.make_errors.append("Missing Makefile")
    else:
        mk_text = makefile_path.read_text()
        found_targets = set(re.findall(r"^([a-z][a-z0-9_-]*):", mk_text, flags=re.M))
        expected_targets = set(REQUIRED_MAKE_TARGETS)
        if lab_name not in STATELESS_NO_SOFT_RESET_LABS:
            expected_targets.add("soft-reset")
        missing_targets = sorted(expected_targets - found_targets)
        if missing_targets:
            result.make_errors.append(f"Makefile missing required targets: {', '.join(missing_targets)}")

    # Invariant 8: Canonical README structure, profile/script coverage, and no hardcoded image tags
    readme_path = lab_dir / "README.md"
    if not readme_path.is_file():
        result.doc_errors.append("Missing README.md")
    else:
        readme_text = readme_path.read_text()
        if "./docker-compose.yml" not in readme_text and "docker-compose.yml" not in readme_text:
            result.doc_errors.append("README.md must link to docker-compose.yml")

        # Check required H2 sections and ordering
        h2_headings = re.findall(r"^## .+$", readme_text, flags=re.M)
        required_h2_prefixes = [
            "## Lab Architecture",
            "## Quickstart & Execution Standard",
            "## Experiment 00: Crash Course",
        ]
        numbered_scripts = [p for p in exp_scripts if not p.name.startswith("00_")]
        for idx, _ in enumerate(numbered_scripts, start=1):
            required_h2_prefixes.append(f"## Experiment {idx:02d}:")
        required_h2_prefixes.extend(
            [
                "## Primary Sources & Deep-Dive References",
                "## Clean Reset & Teardown",
            ]
        )

        cursor = 0
        for req_prefix in required_h2_prefixes:
            matched_idx = None
            for i in range(cursor, len(h2_headings)):
                if h2_headings[i].startswith(req_prefix):
                    matched_idx = i
                    break
            if matched_idx is None:
                result.doc_errors.append(f"README.md missing or out-of-order H2 section starting with '{req_prefix}'")
            else:
                cursor = matched_idx + 1

        # Verify each numbered experiment has the 5 canonical H3 subsections
        exp_blocks = re.split(r"(?=^## )", readme_text, flags=re.M)
        for block in exp_blocks:
            first_line = block.splitlines()[0] if block.splitlines() else ""
            if re.match(r"^## Experiment (?!00)\d{2}:", first_line):
                for sub in REQUIRED_EXPERIMENT_SUBSECTIONS:
                    if sub not in block:
                        result.doc_errors.append(f"'{first_line}' is missing required subsection '{sub}'")

        # Verify every Compose profile is documented in README.md
        for prof in sorted(declared_profiles):
            if prof not in readme_text:
                result.doc_errors.append(f"Compose profile '{prof}' is not documented in README.md")

        # Verify every experiment script stem is documented in README.md
        for sp in exp_scripts:
            if sp.stem not in readme_text:
                result.doc_errors.append(f"Experiment script '{sp.name}' is not referenced in README.md")

        # Verify no hardcoded image tags are duplicated in README.md
        tag_matches = HARDCODED_IMAGE_TAG_RE.findall(readme_text)
        if tag_matches:
            result.doc_errors.append(f"README.md contains hardcoded image tags (link to docker-compose.yml instead): {sorted(set(tag_matches))}")

    return result


def audit_root_docs(results: list[AuditResult]) -> list[str]:
    errors: list[str] = []
    root_readme = REPO_ROOT / "README.md"
    root_makefile = REPO_ROOT / "Makefile"

    if not root_readme.is_file():
        return ["Missing root README.md"]

    readme_text = root_readme.read_text()
    lab_names = [r.lab for r in results]
    total_scripts = sum(
        len([p for p in (REPO_ROOT / r.lab / "experiments").glob("*.py") if EXPERIMENT_SCRIPT_RE.match(p.name)])
        for r in results
        if (REPO_ROOT / r.lab / "experiments").is_dir()
    )

    # Verify root README Lab Matrix heading count matches actual total scripts and lab count
    matrix_match = re.search(
        r"## Lab Matrix \((\d+) Reproducible [A-Za-z ]+ Across (\d+) Labs",
        readme_text,
    )
    if not matrix_match:
        errors.append("Root README.md missing canonical '## Lab Matrix (<N> Reproducible ... Across <M> Labs)' heading")
    else:
        doc_scripts = int(matrix_match.group(1))
        doc_labs = int(matrix_match.group(2))
        if doc_scripts != total_scripts:
            errors.append(f"Root README.md Lab Matrix count ({doc_scripts}) does not match actual experiment scripts ({total_scripts})")
        if doc_labs != len(lab_names):
            errors.append(f"Root README.md lab count ({doc_labs}) does not match actual labs ({len(lab_names)})")

    for lab in lab_names:
        if f"(./{lab}/)" not in readme_text:
            errors.append(f"Root README.md Lab Matrix is missing link to './{lab}/'")

    if root_makefile.is_file():
        mk_text = root_makefile.read_text()
        for lab in lab_names:
            if not re.search(rf"\b{re.escape(lab)}\b", mk_text):
                errors.append(f"Root Makefile is missing lab '{lab}' in workspace loops")

    return errors


def main() -> int:
    if not COMPOSE_FILES:
        console.print("[bold red]ERROR: No docker-compose.yml files found to audit.[/]")
        return 1

    results = [audit_lab(cf) for cf in COMPOSE_FILES]
    root_errors = audit_root_docs(results)
    total_services = sum(r.service_count for r in results)
    total_errors = sum(len(r.all_errors) for r in results) + len(root_errors)

    table = Table(
        title="Repository Safety, Architecture & Documentation Invariant Audit",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("Lab", style="bold", no_wrap=True)
    table.add_column("Svcs", justify="center")
    table.add_column("Compose", justify="center")
    table.add_column("PEP 723", justify="center")
    table.add_column("Makefile", justify="center")
    table.add_column("README", justify="center")
    table.add_column("Status", justify="center")

    for r in results:
        status = "[bold green]PASS[/]" if not r.all_errors else "[bold red]FAIL[/]"
        c_comp = "[red]✖[/]" if r.compose_errors else "[green]✔[/]"
        c_scr = "[red]✖[/]" if r.script_errors else "[green]✔[/]"
        c_mk = "[red]✖[/]" if r.make_errors else "[green]✔[/]"
        c_doc = "[red]✖[/]" if r.doc_errors else "[green]✔[/]"

        table.add_row(
            r.lab,
            str(r.service_count),
            c_comp,
            c_scr,
            c_mk,
            c_doc,
            status,
        )

    console.print()
    console.print(table)
    console.print()

    if total_errors > 0:
        console.print(f"[bold red]Found {total_errors} Invariant Violation(s):[/]\n")
        for r in results:
            if r.all_errors:
                console.print(f"[bold yellow]{r.lab}:[/]")
                for err in r.all_errors:
                    console.print(f"  [red]✖[/] {err}")
        if root_errors:
            console.print("[bold yellow]root:[/]")
            for err in root_errors:
                console.print(f"  [red]✖[/] {err}")
        return 1

    console.print(f"[bold green]✔ All {len(results)} labs ({total_services} services) and root docs strictly comply with AGENTS.md invariants.[/]\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

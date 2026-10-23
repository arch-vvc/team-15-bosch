"""Read-only prerequisite checks. No pulls, restarts, or system changes."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence
from incident_zero.config import model_settings


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def command_check(name: str, command: Sequence[str]) -> Check:
    if shutil.which(command[0]) is None:
        return Check(name, "fail", f"{command[0]} is not installed or not on PATH")
    try:
        result = subprocess.run(
            list(command), capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name, "fail", str(exc))
    detail = (result.stdout.strip() or result.stderr.strip())[:1500]
    return Check(name, "pass" if result.returncode == 0 else "fail", detail)


def run_checks(root: Path) -> list[Check]:
    version = platform.python_version()
    free_gib = shutil.disk_usage(root).free / 1024**3
    settings = model_settings(root / ".env")
    return [
        Check("python", "pass" if sys.version_info >= (3, 11) else "fail", version),
        Check("disk", "pass" if free_gib >= 14 else "fail", f"{free_gib:.1f} GiB free; brief estimates 14 GiB for demo"),
        command_check("git", ["git", "--version"]),
        command_check("compose", ["docker", "compose", "version"]),
        command_check("docker_engine", ["docker", "info", "--format", "{{.ServerVersion}}"]),
        Check("demo_checkout", "pass" if (root / "opentelemetry-demo" / ".git").exists() else "pending", "Expected upstream checkout at opentelemetry-demo/"),
        Check("model_credentials", "pass" if settings.get("OPENAI_API_KEY") else "pending", "Credential present (account access unverified)" if settings.get("OPENAI_API_KEY") else "Set OPENAI_API_KEY in the local .env; never paste it into chat"),
        Check("organizer_template", "pending", "Need the official template repository and pinned demo version"),
        Check("action_api", "pending", "Need the official allowlist, API contract, approval and rollback semantics"),
        Check("incident_schema", "pending", "Need the official incident JSON schema; no scoring output is generated yet"),
        Check("practice_data", "pending", "Need official practice incidents or offline telemetry bundles"),
    ]


def as_report(checks: list[Check]) -> dict:
    return {
        "report_type": "local_environment_check",
        "ready_for_integration": all(check.status == "pass" for check in checks),
        "checks": [asdict(check) for check in checks],
    }

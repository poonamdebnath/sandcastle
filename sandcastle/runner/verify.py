from __future__ import annotations

import subprocess
from pathlib import Path

from .models import GatesConfig
from .state_store import utc_now_iso, write_json_file


def run_verification(
    *,
    worktree_path: Path,
    verification_dir: Path,
    issue_number: int,
    gates: GatesConfig,
) -> Path:
    steps: list[dict[str, object]] = []

    for step_name in gates.verification_order:
        if step_name == "format" and gates.format.enabled:
            steps.append(_run_step("format", list(gates.format.command), worktree_path))
        elif step_name == "tests":
            steps.append(_run_step("tests", list(gates.tests.default_command), worktree_path))

    overall_status = "passed" if all(step["returncode"] == 0 for step in steps) else "failed"
    payload = {
        "issue_number": issue_number,
        "worktree_path": str(worktree_path),
        "status": overall_status,
        "steps": steps,
        "verified_at_utc": utc_now_iso(),
    }
    return write_json_file(verification_dir / f"{issue_number}.json", payload)


def _run_step(name: str, command: list[str], cwd: Path) -> dict[str, object]:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "name": name,
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }

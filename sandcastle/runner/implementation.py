from __future__ import annotations

import subprocess
from pathlib import Path

from .context import fetch_issue_context
from .models import ImplementationPolicy
from .state_store import read_json_file, utc_now_iso, write_json_file
from .worktree import (
    current_head_sha,
    diff_stat_between,
    list_changed_files_between,
    list_worktree_changed_files,
    list_worktree_changes,
)


def run_implementation_command(
    *,
    repo_root: Path,
    implementation_dir: Path,
    policy: ImplementationPolicy,
    issue_number: int,
    branch: str,
    worktree_path: str,
    issue_title: str,
    context_path: str | None,
    implementation_mode: str,
    findings_path: str | None,
    revision_path: str | None,
) -> Path:
    if not policy.enabled:
        payload = {
            "issue_number": issue_number,
            "branch": branch,
            "worktree_path": worktree_path,
            "issue_title": issue_title,
            "context_path": context_path,
            "implementation_mode": implementation_mode,
            "findings_path": findings_path,
            "revision_path": revision_path,
            "status": "disabled",
            "command_template": list(policy.command),
            "command": [],
            "returncode": None,
            "stdout": "",
            "stderr": "Implementation command is disabled in sandcastle/config/repo.json.",
            "started_at_utc": utc_now_iso(),
            "completed_at_utc": utc_now_iso(),
        }
        return write_json_file(implementation_dir / f"{issue_number}.json", payload)

    issue_context = _load_issue_context(repo_root, issue_number, context_path)
    resolved_command = _resolve_command(
        policy.command,
        issue_number=issue_number,
        branch=branch,
        worktree_path=worktree_path,
        issue_title=issue_title,
        context_path=context_path or "",
        issue_url=str(issue_context.get("url", "")),
        implementation_mode=implementation_mode,
        findings_path=findings_path or "",
        revision_path=revision_path or "",
    )
    started_at = utc_now_iso()
    pre_command_head_sha = current_head_sha(worktree_path)
    result = subprocess.run(
        resolved_command,
        cwd=Path(worktree_path),
        check=False,
        capture_output=True,
        text=True,
    )
    post_command_head_sha = current_head_sha(worktree_path)
    worktree_changes = list_worktree_changes(worktree_path)
    changed_files = sorted(
        {
            *list_changed_files_between(
                worktree_path, pre_command_head_sha, post_command_head_sha
            ),
            *list_worktree_changed_files(worktree_path),
        }
    )
    diff_summary = diff_stat_between(
        worktree_path, pre_command_head_sha, post_command_head_sha
    )
    if not diff_summary:
        diff_summary = "\n".join(worktree_changes[:20]).strip()
    payload = {
        "issue_number": issue_number,
        "branch": branch,
        "worktree_path": worktree_path,
        "issue_title": issue_title,
        "context_path": context_path,
        "implementation_mode": implementation_mode,
        "findings_path": findings_path,
        "revision_path": revision_path,
        "status": "succeeded" if result.returncode == 0 else "failed",
        "command_template": list(policy.command),
        "command": resolved_command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "pre_command_head_sha": pre_command_head_sha,
        "post_command_head_sha": post_command_head_sha,
        "worktree_changes": worktree_changes,
        "changed_files": changed_files,
        "diff_summary": diff_summary,
        "started_at_utc": started_at,
        "completed_at_utc": utc_now_iso(),
    }
    return write_json_file(implementation_dir / f"{issue_number}.json", payload)


def _load_issue_context(
    repo_root: Path, issue_number: int, context_path: str | None
) -> dict[str, object]:
    if context_path:
        path = Path(context_path)
        if path.exists():
            return read_json_file(path)
    return fetch_issue_context(repo_root, issue_number)


def _resolve_command(
    command_template: tuple[str, ...],
    **values: str | int,
) -> list[str]:
    substitutions = {key: str(value) for key, value in values.items()}
    return [part.format(**substitutions) for part in command_template]
